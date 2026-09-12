"""Owned subprocess support for the ACP stdio wire.

The JSON-RPC implementation deliberately knows nothing about processes.  This
module is the small, POSIX-specific bridge: it gives that wire deadline-aware
pipe objects while retaining the adapter launch ownership contract.
"""

from __future__ import annotations

import os
import select
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Self

from .acp_stream import (
    AcpCancelled,
    AcpDeadlineExpired,
    AcpEofError,
    AcpJsonRpcStream,
    AcpWriteError,
)
from .adapters import (
    _adapter_cgroup,
    _join_cgroup,
    _on_adapter_started,
    _remove_cgroup,
    sanitize_diagnostic,
)

# A cancellation predicate has no file descriptor to wake ``select``.  Keep
# its observation latency bounded even for an otherwise unbounded connection.
_CANCELLATION_POLL_S = 0.05


class AcpProcessError(RuntimeError):
    """Base class for local ACP process evidence (never provider evidence)."""


class AcpLaunchError(AcpProcessError):
    pass


class AcpUnexpectedExit(AcpProcessError):
    pass


class AcpTransportEof(AcpProcessError):
    pass


class AcpProcessTimeout(AcpProcessError):
    pass


class AcpIntentionalShutdown(AcpProcessError):
    """The supervisor's own bounded shutdown, never an adapter failure."""


class _SupervisedStream:
    """Translate wire failures into evidence from its owning process.

    Keeping this as a small proxy leaves the wire deliberately process-free,
    while making the process distinctions unavoidable for normal ACP calls.
    """

    def __init__(self, owner: AcpSubprocess, wire: AcpJsonRpcStream) -> None:
        self._owner, self._wire = owner, wire

    def __getattr__(self, name: str) -> object:
        member = getattr(self._wire, name)
        if not callable(member):
            return member

        def supervised(*args: object, **kwargs: object) -> object:
            self._owner._check_before_wire()
            try:
                return member(*args, **kwargs)
            except AcpEofError as exc:
                self._owner.transport_eof(exc)
            except AcpDeadlineExpired as exc:
                self._owner.deadline_expired(exc)
            except AcpWriteError as exc:
                self._owner.transport_write_error(exc)

        return supervised


class _PipeReader:
    def __init__(self, pipe: object, clock: Callable[[], float]) -> None:
        self._pipe = pipe
        self._fd = pipe.fileno()  # type: ignore[attr-defined]
        self._clock = clock

    def read_with_deadline(self, size: int, deadline: float | None,
                           cancelled: Callable[[], bool] | None) -> bytes:
        while True:
            if cancelled is not None and cancelled():
                raise AcpCancelled("ACP operation cancelled")
            remaining = None if deadline is None else deadline - self._clock()
            if remaining is not None and remaining <= 0:
                raise AcpDeadlineExpired("ACP process deadline expired")
            # A finite polling interval is required when cancellation changes
            # after this call has started (and also avoids an infinite select
            # when no deadline was supplied).
            wait = _CANCELLATION_POLL_S if remaining is None else min(
                remaining, _CANCELLATION_POLL_S,
            )
            readable, _, _ = select.select([self._fd], [], [], wait)
            if readable:
                return os.read(self._fd, size)


class _PipeWriter:
    def __init__(self, pipe: object, clock: Callable[[], float]) -> None:
        self._pipe = pipe
        self._fd = pipe.fileno()  # type: ignore[attr-defined]
        # select only promises that *some* pipe capacity was available.  A
        # blocking descriptor can still block in os.write for a larger frame.
        os.set_blocking(self._fd, False)
        self._clock = clock

    def write_with_deadline(self, data: bytes, deadline: float | None,
                            cancelled: Callable[[], bool] | None) -> int:
        while True:
            if cancelled is not None and cancelled():
                raise AcpCancelled("ACP operation cancelled")
            remaining = None if deadline is None else deadline - self._clock()
            if remaining is not None and remaining <= 0:
                raise AcpDeadlineExpired("ACP process deadline expired")
            wait = _CANCELLATION_POLL_S if remaining is None else min(
                remaining, _CANCELLATION_POLL_S,
            )
            _, writable, _ = select.select([], [self._fd], [], wait)
            if not writable:
                # This can be a cancellation poll rather than deadline expiry.
                # Recheck both on the next loop before continuing to wait.
                continue
            try:
                return os.write(self._fd, data)
            except BlockingIOError:
                # Another write or a readiness race filled the pipe.  Never
                # turn that into an unbounded blocking write.
                continue

    def flush_with_deadline(self, deadline: float | None,
                            cancelled: Callable[[], bool] | None) -> None:
        if cancelled is not None and cancelled():
            raise AcpCancelled("ACP operation cancelled")
        if deadline is not None and self._clock() >= deadline:
            raise AcpDeadlineExpired("ACP process deadline expired")


class AcpSubprocess:
    """An ACP child with bounded stderr capture and containment-aware cleanup.

    ``deadline`` is a single monotonic deadline for all wire activity.  The
    caller supplies it rather than allowing each operation a fresh timeout.
    ``close`` is required after a successful prompt too: ACP servers commonly
    remain alive after their reply.
    """

    def __init__(self, command: Sequence[str], *, cwd: Path, deadline: float,
                 log_path: Path, clock: Callable[[], float] = time.monotonic,
                 stderr_bytes: int = 32_768, grace_s: float = 5.0,
                 env: Mapping[str, str] | None = None) -> None:
        if not command or stderr_bytes < 0 or grace_s < 0:
            raise ValueError("invalid ACP subprocess bounds")
        self.deadline = deadline
        self._clock, self._grace = clock, grace_s
        self.log_path = log_path
        self._stderr: deque[bytes] = deque()
        self._stderr_size = 0
        self._stderr_limit = stderr_bytes
        self._stderr_done = threading.Event()
        self._closed = False
        self._intentional_shutdown = False
        # A nonzero exit observed before this client sends a containment
        # signal is process failure evidence, even when it happens while
        # orderly stdin-EOF cleanup is in progress.
        self.shutdown_failure: AcpUnexpectedExit | None = None
        self._shutdown_failure_during_cleanup = False
        # This is deliberately separate from a process exit code: closing a
        # server is cleanup, not a successful ACP result.
        self.shutdown_outcome: str | None = None
        # Ordered bounds that actually expired during local cleanup.  The
        # client combines these with expired cancellation/close wire bounds.
        self.timeout_phases: list[str] = []
        self.cleanup_uncertain = False
        self._cgroup = _adapter_cgroup()
        # ``env`` is an isolation boundary.  In particular, do not prepend a
        # runtime-home bin directory: a relative verified command must resolve
        # only through the explicitly supplied PATH, not a parent-controlled
        # same-named executable.
        launch_env = dict(os.environ if env is None else env)
        launched = False
        try:
            self.proc = subprocess.Popen(
                list(command), cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=launch_env, start_new_session=True,
                preexec_fn=(lambda: _join_cgroup(self._cgroup)) if self._cgroup else None,  # noqa: PLW1509 - containment before ACP exec
            )
            launched = True
            # Ownership is recorded immediately after launch, before even a
            # protocol-pipe object is made.  The failure path cannot use
            # ``close`` yet, so it invokes the same containment sequence.
            callback = _on_adapter_started.get()
            if callback is not None:
                try:
                    callback(self.proc.pid)
                except BaseException:
                    self._cleanup_process(close_pipes=True)
                    self.log_path.parent.mkdir(parents=True, exist_ok=True)
                    self.log_path.write_text("")
                    raise
        except (OSError, subprocess.SubprocessError) as exc:
            if not launched:
                _remove_cgroup(self._cgroup)
                raise AcpLaunchError(
                    f"ACP launcher failure: {sanitize_diagnostic(str(exc))}"
                ) from exc
            # An ownership callback may itself choose a subprocess exception;
            # it is not evidence that the launcher failed.  It has already
            # run its containment cleanup, so retain its cleanup semantics.
            if not self.cleanup_uncertain:
                _remove_cgroup(self._cgroup)
            raise
        except BaseException:
            if not self.cleanup_uncertain:
                _remove_cgroup(self._cgroup)
            raise
        assert self.proc.stdin and self.proc.stdout and self.proc.stderr
        self.reader = _PipeReader(self.proc.stdout, clock)
        self.writer = _PipeWriter(self.proc.stdin, clock)
        self._drainer = threading.Thread(target=self._drain_stderr, daemon=True)
        self._drainer.start()

    def _drain_stderr(self) -> None:
        assert self.proc.stderr
        try:
            while chunk := self.proc.stderr.read(4096):
                self._stderr.append(chunk)
                self._stderr_size += len(chunk)
                while self._stderr and self._stderr_size > self._stderr_limit:
                    self._stderr_size -= len(self._stderr.popleft())
        finally:
            self._stderr_done.set()

    @property
    def diagnostics(self) -> str:
        return sanitize_diagnostic(b"".join(self._stderr).decode(errors="replace"))

    def stream(self, **kwargs: object) -> _SupervisedStream:
        """Make the existing wire over this process's deadline-aware pipes."""
        wire = AcpJsonRpcStream(
            self.reader, self.writer, clock=self._clock,
            default_deadline=self.deadline, **kwargs,
        )
        return _SupervisedStream(self, wire)

    def check(self) -> None:
        self._raise_if_intentional_shutdown()
        if self._clock() >= self.deadline:
            raise AcpProcessTimeout("ACP total deadline expired")
        code = self.proc.poll()
        if code is not None:
            raise AcpUnexpectedExit(self._exit_message(code))

    def check_post_response_failure(self, observation_s: float = 0.05) -> None:
        """Reject a nonzero exit that races directly behind a prompt reply.

        A live server remains ours to close.  But a server which has already
        failed after replying must not acquire an "intentional shutdown"
        label merely because cleanup starts a few scheduler ticks later.
        """
        if observation_s < 0:
            raise ValueError("post-response observation must be nonnegative")
        try:
            self.proc.wait(timeout=observation_s)
        except subprocess.TimeoutExpired:
            return
        code = self.proc.returncode
        if isinstance(code, int) and code != 0:
            raise AcpUnexpectedExit(self._exit_message(code))

    def _raise_if_intentional_shutdown(self) -> None:
        if self._intentional_shutdown:
            raise AcpIntentionalShutdown(
                self.shutdown_outcome or "ACP intentional shutdown in progress"
            )

    def _check_before_wire(self) -> None:
        """Do not allow a known local death to look like a wire failure."""
        self.check()

    def deadline_expired(self, exc: AcpDeadlineExpired) -> None:
        """Translate expiration of this supervisor's total deadline."""
        self._raise_if_intentional_shutdown()
        if self._clock() >= self.deadline:
            raise AcpProcessTimeout("ACP total deadline expired") from exc
        raise exc

    def transport_eof(self, exc: AcpEofError) -> None:
        """Convert wire EOF to local transport evidence, preserving exit state."""
        self._raise_if_intentional_shutdown()
        code = self.proc.poll()
        if code is not None:
            raise AcpUnexpectedExit(self._exit_message(code)) from exc
        raise AcpTransportEof(f"ACP transport EOF: {sanitize_diagnostic(str(exc))}") from exc

    def transport_write_error(self, exc: AcpWriteError) -> None:
        """Prefer newly-established local death over a generic pipe error.

        The child can exit after the proxy's pre-operation poll but before its
        stdin write reaches the kernel.  In that race the stream correctly
        wraps ``BrokenPipeError`` as ``AcpWriteError``; a second poll preserves
        the stronger process evidence when it is now available.
        """
        self._raise_if_intentional_shutdown()
        code = self.proc.poll()
        if code is not None:
            raise AcpUnexpectedExit(self._exit_message(code)) from exc
        raise exc

    def _exit_message(self, code: int) -> str:
        detail = f"; stderr: {self.diagnostics}" if self.diagnostics else ""
        if code < 0:
            return f"ACP child terminated by signal {-code}{detail}"
        return f"ACP child exited unexpectedly with status {code}{detail}"

    def _terminate(self, *, force: bool = False) -> None:
        # The direct child can exit before a descendant in its session does.
        # Signal the process group even in that case: a dead parent is not
        # evidence that its ACP process tree has gone away.
        try:
            os.killpg(self.proc.pid, signal.SIGKILL if force else signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    def _wait_for_exit(self, timeout: float) -> bool:
        """Reap the direct child in a bounded cleanup phase.

        Cleanup has its own bound.  The protocol deadline must not turn a
        timeout into an unreaped zombie merely because it was already spent.
        """
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        return True

    def _process_group_alive(self) -> bool:
        """Whether the owned session group still has a member.

        A reaped leader is not proof that its descendants are gone.
        """
        try:
            os.killpg(self.proc.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _terminate_cgroup(self) -> None:
        """Kill all owned cgroup members when cgroup v2 exposes cgroup.kill.

        The session group is the no-cgroup fallback.  It cannot see a
        descendant which deliberately escaped with ``setsid``; a dedicated
        cgroup can, so prefer its kernel containment operation when present.
        ``cgroup.kill`` is optional on older or constrained cgroup v2 hosts,
        in which case membership inspection below remains the evidence.
        """
        if self._cgroup is None:
            return
        try:
            (self._cgroup / "cgroup.kill").write_text("1")
        except OSError:
            pass

    def _cgroup_empty(self) -> bool | None:
        """Return cgroup membership evidence, or ``None`` if it is unreadable."""
        if self._cgroup is None:
            return None
        try:
            return not (self._cgroup / "cgroup.procs").read_text().split()
        except OSError:
            return None

    def _cleanup_process(self, *, close_pipes: bool) -> None:
        """Run the containment cleanup sequence, including launch failures."""
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        exited_after_eof = self._wait_for_exit(self._grace)
        if (exited_after_eof and self._intentional_shutdown
                and isinstance(self.proc.returncode, int) and self.proc.returncode != 0):
            self.shutdown_failure = AcpUnexpectedExit(self._exit_message(self.proc.returncode))
            self._shutdown_failure_during_cleanup = True
        if not exited_after_eof:
            self.timeout_phases.append("term_grace")
        self._terminate()
        if not self._wait_for_exit(self._grace):
            self.timeout_phases.append("kill_grace")
        self._terminate(force=True)
        self._terminate_cgroup()
        reaped = self._wait_for_exit(self._grace)
        cgroup_empty = self._cgroup_empty()
        self.cleanup_uncertain = (
            not reaped
            or self._process_group_alive()
            # A session group is only a best-effort fallback: a descendant
            # can call setsid(2) and escape it.  Without cgroup membership
            # evidence, a reaped leader and empty original group cannot prove
            # that the whole owned process tree is gone.
            or self._cgroup is None
            or cgroup_empty is not True
        )
        if close_pipes:
            for pipe in (self.proc.stdout, self.proc.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass
        if not self.cleanup_uncertain:
            _remove_cgroup(self._cgroup)

    def close(self) -> None:
        """Boundedly close, terminate and reap; retained cgroup is survivor evidence."""
        if self._closed:
            return
        self._closed = True
        preexisting_exit = self.proc.poll()
        self._intentional_shutdown = preexisting_exit is None
        if isinstance(preexisting_exit, int) and preexisting_exit != 0:
            self.shutdown_failure = AcpUnexpectedExit(self._exit_message(preexisting_exit))
        self.shutdown_outcome = (
            "ACP intentional shutdown in progress" if preexisting_exit is None
            else f"ACP process exited before deliberate shutdown with status {preexisting_exit}"
        )
        # EOF is the ACP stdio closure/cancellation signal.  Give a compliant
        # server a small, independent grace period before containment signals.
        self._cleanup_process(close_pipes=False)
        if self.proc.stdout is not None:
            try:
                self.proc.stdout.close()
            except OSError:
                pass
        self._stderr_done.wait(timeout=self._grace)
        self._drainer.join(timeout=self._grace)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text(self.diagnostics + ("\n" if self.diagnostics else ""))
        code = self.proc.returncode
        if code is None:
            evidence = "child not reaped"
        elif code < 0:
            evidence = f"child terminated by signal {-code}"
        else:
            evidence = f"child exited with status {code}"
        uncertainty = "; containment uncertain" if self.cleanup_uncertain else ""
        prefix = "ACP process failure during deliberate shutdown" if (
            self.shutdown_failure and self._shutdown_failure_during_cleanup
        ) else (
            "ACP process exit observed before deliberate shutdown" if self.shutdown_failure else (
            "ACP intentional shutdown" if preexisting_exit is None else (
            "ACP process exit observed before deliberate shutdown"
            )
            )
        )
        self.shutdown_outcome = f"{prefix}: {evidence}{uncertainty}"

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


AcpWireProcess = AcpSubprocess
AcpProcessSupervisor = AcpSubprocess


def launch_acp(command: Sequence[str], *, cwd: Path, deadline: float, log_path: Path,
               clock: Callable[[], float] = time.monotonic,
               env: Mapping[str, str] | None = None) -> AcpSubprocess:
    """Launch an owned ACP subprocess; callers must close it in ``finally``."""
    return AcpSubprocess(command, cwd=cwd, deadline=deadline, log_path=log_path, clock=clock,
                         env=env)
