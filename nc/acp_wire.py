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
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Self

from .acp_stream import AcpDeadlineExpired, AcpEofError, AcpJsonRpcStream, AcpWriteError
from .adapters import (
    _adapter_cgroup,
    _join_cgroup,
    _on_adapter_started,
    _remove_cgroup,
    sanitize_diagnostic,
)


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


class _PipeReader:
    def __init__(self, pipe: object, clock: Callable[[], float]) -> None:
        self._pipe = pipe
        self._fd = pipe.fileno()  # type: ignore[attr-defined]
        self._clock = clock

    def read_with_deadline(self, size: int, deadline: float | None,
                           cancelled: Callable[[], bool] | None) -> bytes:
        while True:
            if cancelled is not None and cancelled():
                raise AcpDeadlineExpired("ACP operation cancelled")
            remaining = None if deadline is None else deadline - self._clock()
            if remaining is not None and remaining <= 0:
                raise AcpDeadlineExpired("ACP process deadline expired")
            readable, _, _ = select.select([self._fd], [], [], remaining)
            if readable:
                return os.read(self._fd, size)


class _PipeWriter:
    def __init__(self, pipe: object, clock: Callable[[], float]) -> None:
        self._pipe = pipe
        self._fd = pipe.fileno()  # type: ignore[attr-defined]
        self._clock = clock

    def write_with_deadline(self, data: bytes, deadline: float | None,
                            cancelled: Callable[[], bool] | None) -> int:
        if cancelled is not None and cancelled():
            raise AcpWriteError("ACP operation cancelled")
        remaining = None if deadline is None else deadline - self._clock()
        if remaining is not None and remaining <= 0:
            raise AcpDeadlineExpired("ACP process deadline expired")
        _, writable, _ = select.select([], [self._fd], [], remaining)
        if not writable:
            raise AcpDeadlineExpired("ACP process deadline expired")
        return os.write(self._fd, data)

    def flush_with_deadline(self, deadline: float | None,
                            cancelled: Callable[[], bool] | None) -> None:
        if cancelled is not None and cancelled():
            raise AcpWriteError("ACP operation cancelled")
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
                 stderr_bytes: int = 32_768, grace_s: float = 2.0) -> None:
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
        self._cgroup = _adapter_cgroup()
        env = dict(os.environ, PATH=f"{Path.home()}/.local/bin:{os.environ.get('PATH', '')}")
        try:
            self.proc = subprocess.Popen(
                list(command), cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, start_new_session=True,
                preexec_fn=(lambda: _join_cgroup(self._cgroup)) if self._cgroup else None,  # noqa: PLW1509 - containment before ACP exec
            )
            callback = _on_adapter_started.get()
            if callback is not None:
                try:
                    callback(self.proc.pid)
                except BaseException:
                    self._terminate(force=True)
                    try:
                        self.proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        # Do not mask the ownership-recording error.  The
                        # cgroup deliberately remains evidence if containment
                        # itself is uncertain.
                        pass
                    for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                        if pipe is not None:
                            try:
                                pipe.close()
                            except OSError:
                                pass
                    raise
        except OSError as exc:
            _remove_cgroup(self._cgroup)
            raise AcpLaunchError(f"ACP launcher failure: {sanitize_diagnostic(str(exc))}") from exc
        except BaseException:
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

    def stream(self, **kwargs: object) -> AcpJsonRpcStream:
        """Make the existing wire over this process's deadline-aware pipes."""
        return AcpJsonRpcStream(self.reader, self.writer, clock=self._clock, **kwargs)

    def check(self) -> None:
        if self._clock() >= self.deadline:
            raise AcpProcessTimeout("ACP total deadline expired")
        code = self.proc.poll()
        if code is not None:
            raise AcpUnexpectedExit(self._exit_message(code))

    def transport_eof(self, exc: AcpEofError) -> None:
        """Convert wire EOF to local transport evidence, preserving exit state."""
        code = self.proc.poll()
        if code is not None:
            raise AcpUnexpectedExit(self._exit_message(code)) from exc
        raise AcpTransportEof(f"ACP transport EOF: {sanitize_diagnostic(str(exc))}") from exc

    def _exit_message(self, code: int) -> str:
        detail = f"; stderr: {self.diagnostics}" if self.diagnostics else ""
        if code < 0:
            return f"ACP child terminated by signal {-code}{detail}"
        return f"ACP child exited unexpectedly with status {code}{detail}"

    def _terminate(self, *, force: bool = False) -> None:
        if self.proc.poll() is not None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGKILL if force else signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    def close(self) -> None:
        """Boundedly close, terminate and reap; retained cgroup is survivor evidence."""
        if self._closed:
            return
        self._closed = True
        for pipe in (self.proc.stdin, self.proc.stdout):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        self._terminate()
        try:
            self.proc.wait(timeout=min(self._grace, max(0.0, self.deadline - self._clock())))
        except subprocess.TimeoutExpired:
            self._terminate(force=True)
            self.proc.wait()
        self._stderr_done.wait(timeout=self._grace)
        self._drainer.join(timeout=0)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text(self.diagnostics + ("\n" if self.diagnostics else ""))
        _remove_cgroup(self._cgroup)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


AcpWireProcess = AcpSubprocess
AcpProcessSupervisor = AcpSubprocess


def launch_acp(command: Sequence[str], *, cwd: Path, deadline: float, log_path: Path,
               clock: Callable[[], float] = time.monotonic) -> AcpSubprocess:
    """Launch an owned ACP subprocess; callers must close it in ``finally``."""
    return AcpSubprocess(command, cwd=cwd, deadline=deadline, log_path=log_path, clock=clock)
