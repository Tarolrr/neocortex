"""Offline ownership tests using Python helpers, never a vendor ACP CLI."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from nc import acp_wire
from nc.acp_stream import AcpCancelled, AcpEofError
from nc.acp_wire import (
    AcpLaunchError,
    AcpProcessTimeout,
    AcpSubprocess,
    AcpTransportEof,
    AcpUnexpectedExit,
)
from nc.adapters import adapter_ownership


def helper(code: str) -> list[str]:
    return [sys.executable, "-c", code]


@pytest.fixture(autouse=True)
def no_host_cgroup(monkeypatch: pytest.MonkeyPatch) -> None:
    """These helpers exercise session containment; cgroup v2 is host-owned."""
    monkeypatch.setattr(acp_wire, "_adapter_cgroup", lambda: None)


def test_stderr_flood_is_drained_and_saved_bounded(tmp_path: Path) -> None:
    log = tmp_path / "acp.stderr.log"
    with AcpSubprocess(
        helper("import sys; sys.stderr.write('x'*200000); sys.stdout.write('ok\\n'); sys.stdout.flush()"),
        cwd=tmp_path, deadline=time.monotonic() + 3, log_path=log,
    ) as child:
        assert child.reader.read_with_deadline(16, time.monotonic() + 2, None) == b"ok\n"
    assert log.exists()
    assert len(log.read_bytes()) <= 1001  # sanitized diagnostic reference is bounded too


def test_ownership_callback_is_before_wire_and_failure_reaps(tmp_path: Path) -> None:
    seen: list[int] = []

    def fail(pid: int) -> None:
        seen.append(pid)
        raise RuntimeError("recording failed")

    with adapter_ownership(fail), pytest.raises(RuntimeError, match="recording failed"):
        AcpSubprocess(helper("import time; time.sleep(30)"), cwd=tmp_path,
                      deadline=time.monotonic() + 2, log_path=tmp_path / "log")
    assert seen
    # The callback is invoked by the Popen parent.  ChildProcessError proves
    # that Popen already reaped that exact child rather than merely killing it.
    with pytest.raises(ChildProcessError):
        os.waitpid(seen[0], os.WNOHANG)


def test_launcher_failure_is_local_and_has_no_child(tmp_path: Path) -> None:
    with pytest.raises(AcpLaunchError, match="launcher failure"):
        AcpSubprocess(["definitely-not-an-acp-helper"], cwd=tmp_path,
                      deadline=time.monotonic() + 1, log_path=tmp_path / "log")


def test_stream_uses_total_deadline_for_hung_request(tmp_path: Path) -> None:
    child = AcpSubprocess(
        helper("import sys,time; sys.stdin.readline(); time.sleep(30)"), cwd=tmp_path,
        deadline=time.monotonic() + 0.2, log_path=tmp_path / "log",
    )
    try:
        with pytest.raises(AcpProcessTimeout, match="total deadline"):
            child.stream().request("hung")
    finally:
        child.close()
    # ``close`` itself reaps after the protocol deadline has expired.  Do not
    # call poll(): it would hide an unreaped-child bug by doing the reaping.
    assert child.proc.returncode is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(child.proc.pid, os.WNOHANG)


def test_pipe_cancellation_remains_distinct_from_deadline_or_write_failure(tmp_path: Path) -> None:
    child = AcpSubprocess(helper("import time; time.sleep(30)"), cwd=tmp_path,
                          deadline=time.monotonic() + 2, log_path=tmp_path / "log")
    try:
        with pytest.raises(AcpCancelled, match="cancelled"):
            child.reader.read_with_deadline(1, time.monotonic() + 1, lambda: True)
        with pytest.raises(AcpCancelled, match="cancelled"):
            child.writer.write_with_deadline(b"x", time.monotonic() + 1, lambda: True)
        with pytest.raises(AcpCancelled, match="cancelled"):
            child.writer.flush_with_deadline(time.monotonic() + 1, lambda: True)
    finally:
        child.close()


def test_full_stdin_pipe_expires_at_total_deadline_and_is_reaped(tmp_path: Path) -> None:
    """A large frame cannot make the raw pipe write block past its deadline."""
    child = AcpSubprocess(
        helper("import time; time.sleep(30)"), cwd=tmp_path,
        deadline=time.monotonic() + 0.15, log_path=tmp_path / "log", grace_s=0.05,
    )
    try:
        started = time.monotonic()
        with pytest.raises(AcpProcessTimeout, match="total deadline"):
            child.stream().send_request("blocked", {"payload": "x" * 128_000})
        assert time.monotonic() - started < 0.8
    finally:
        child.close()
    assert child.proc.returncode is not None


def test_pipe_cancellation_interrupts_started_read_and_write(tmp_path: Path) -> None:
    """Cancellation is polled while a pipe is idle/full, not just on entry."""
    reader_child = AcpSubprocess(
        helper("import time; time.sleep(30)"), cwd=tmp_path,
        deadline=time.monotonic() + 2, log_path=tmp_path / "read.log", grace_s=0.05,
    )
    cancelled = threading.Event()
    timer = threading.Timer(0.05, cancelled.set)
    try:
        timer.start()
        with pytest.raises(AcpCancelled, match="cancelled"):
            reader_child.reader.read_with_deadline(
                1, time.monotonic() + 1, cancelled.is_set,
            )
    finally:
        timer.cancel()
        timer.join()
        reader_child.close()

    writer_child = AcpSubprocess(
        helper("import time; time.sleep(30)"), cwd=tmp_path,
        deadline=time.monotonic() + 2, log_path=tmp_path / "write.log", grace_s=0.05,
    )
    cancelled = threading.Event()
    timer = threading.Timer(0.05, cancelled.set)
    try:
        timer.start()
        with pytest.raises(AcpCancelled, match="cancelled"):
            writer_child.stream().send_request(
                "blocked", {"payload": "x" * 128_000}, cancelled=cancelled.is_set,
            )
    finally:
        timer.cancel()
        timer.join()
        writer_child.close()
    assert reader_child.proc.returncode is not None
    assert writer_child.proc.returncode is not None


def test_close_escalates_hung_graceful_shutdown_and_reaps(tmp_path: Path) -> None:
    child = AcpSubprocess(
        helper(
            "import signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); sys.stdin.read(); time.sleep(30)"
        ),
        cwd=tmp_path, deadline=time.monotonic() - 1, log_path=tmp_path / "log",
        grace_s=0.05,
    )
    assert child.reader.read_with_deadline(16, time.monotonic() + 1, None) == b"ready\n"
    child.close()
    # SIGKILL is required because EOF and SIGTERM were both deliberately
    # ignored.  returncode is populated by close's wait, rather than poll().
    assert child.proc.returncode == -9
    with pytest.raises(ChildProcessError):
        os.waitpid(child.proc.pid, os.WNOHANG)


def test_exit_and_live_stdout_eof_are_not_provider_or_success(tmp_path: Path) -> None:
    with AcpSubprocess(helper("import sys; sys.exit(7)"), cwd=tmp_path,
                       deadline=time.monotonic() + 2, log_path=tmp_path / "log") as child:
        child.proc.wait(timeout=1)
        with pytest.raises(AcpUnexpectedExit, match="status 7"):
            child.transport_eof(AcpEofError("stdout EOF"))
    with (
        AcpSubprocess(helper("import os,time; os.close(1); time.sleep(2)"), cwd=tmp_path,
                      deadline=time.monotonic() + 3, log_path=tmp_path / "log2") as child,
        pytest.raises(AcpTransportEof, match="transport EOF"),
    ):
        child.transport_eof(AcpEofError("stdout EOF"))


def test_stream_request_automatically_translates_exit_and_eof(tmp_path: Path) -> None:
    with (
        AcpSubprocess(helper("import sys; sys.exit(7)"), cwd=tmp_path,
                      deadline=time.monotonic() + 2, log_path=tmp_path / "exit.log") as child,
        pytest.raises(AcpUnexpectedExit, match="status 7"),
    ):
        # Normal request use, rather than caller-side error translation.
        child.stream().request("request")
    with (
        AcpSubprocess(helper("import os,time; os.close(1); time.sleep(2)"), cwd=tmp_path,
                      deadline=time.monotonic() + 3, log_path=tmp_path / "eof.log") as child,
        pytest.raises(AcpTransportEof, match="transport EOF"),
    ):
        child.stream().request("request")


def test_stream_write_error_rechecks_child_exit(tmp_path: Path) -> None:
    """A death after the proxy poll must not escape as generic AcpWriteError."""
    child = AcpSubprocess(
        helper("import time,sys; time.sleep(.1); sys.exit(7)"), cwd=tmp_path,
        deadline=time.monotonic() + 2, log_path=tmp_path / "exit-write.log",
    )
    original_write = child.writer.write_with_deadline

    def write_after_child_exit(*args: object, **kwargs: object) -> int:
        child.proc.wait(timeout=1)
        return original_write(*args, **kwargs)

    child.writer.write_with_deadline = write_after_child_exit  # type: ignore[method-assign]
    try:
        with pytest.raises(AcpUnexpectedExit, match="status 7"):
            child.stream().send_request("request")
    finally:
        child.close()


def test_stream_request_automatically_translates_total_timeout(tmp_path: Path) -> None:
    child = AcpSubprocess(
        helper("import sys,time; sys.stdin.readline(); time.sleep(30)"), cwd=tmp_path,
        deadline=time.monotonic() + 0.1, log_path=tmp_path / "timeout.log",
    )
    try:
        with pytest.raises(AcpProcessTimeout, match="total deadline"):
            child.stream().request("hung")
    finally:
        child.close()


def test_signal_exit_is_reported_as_local_process_evidence(tmp_path: Path) -> None:
    code = "import os,signal; os.kill(os.getpid(), signal.SIGTERM)"
    with AcpSubprocess(helper(code), cwd=tmp_path,
                       deadline=time.monotonic() + 2, log_path=tmp_path / "log") as child:
        child.proc.wait(timeout=1)
        with pytest.raises(AcpUnexpectedExit, match="signal 15"):
            child.check()


def test_normal_reply_is_followed_by_cleanup_of_live_server(tmp_path: Path) -> None:
    code = (
        "import sys,time; sys.stdin.readline(); "
        "print('{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{}}', flush=True); "
        "time.sleep(30)"
    )
    child = AcpSubprocess(helper(code), cwd=tmp_path,
                          deadline=time.monotonic() + 2, log_path=tmp_path / "log")
    try:
        assert child.stream().request("complete") == {
            "jsonrpc": "2.0", "id": 1, "result": {},
        }
        assert child.proc.poll() is None
    finally:
        child.close()
    assert child.proc.poll() is not None


def test_close_kills_hung_session_group(tmp_path: Path) -> None:
    child = AcpSubprocess(helper("import time; time.sleep(30)"), cwd=tmp_path,
                          deadline=time.monotonic() + 2, log_path=tmp_path / "log")
    child.close()
    assert child.proc.poll() is not None


def _assert_helpers_gone(pids: list[int]) -> None:
    for _ in range(100):
        survivors = []
        for pid in pids:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            survivors.append(pid)
        if not survivors:
            return
        time.sleep(0.02)
    pytest.fail(f"ACP helper descendants survived shutdown: {survivors}")


def test_close_kills_child_and_grandchild_in_owned_session(tmp_path: Path) -> None:
    code = (
        "import subprocess,sys,time; "
        "code=\"import subprocess,sys,time; p=subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)']); print(p.pid, flush=True); time.sleep(30)\"; "
        "p=subprocess.Popen([sys.executable, '-c', code], stdout=sys.stdout); "
        "print(p.pid, flush=True); time.sleep(30)"
    )
    child = AcpSubprocess(helper(code), cwd=tmp_path, deadline=time.monotonic() + 3,
                          log_path=tmp_path / "log")
    pids = [int(child.reader.read_with_deadline(40, time.monotonic() + 2, None))]
    pids.append(int(child.reader.read_with_deadline(40, time.monotonic() + 2, None)))
    child.close()
    _assert_helpers_gone(pids)


def test_close_kills_descendant_after_parent_already_exited(tmp_path: Path) -> None:
    code = (
        "import subprocess,sys; "
        "p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "print(p.pid, flush=True)"
    )
    child = AcpSubprocess(helper(code), cwd=tmp_path, deadline=time.monotonic() + 3,
                          log_path=tmp_path / "log")
    descendant = int(child.reader.read_with_deadline(40, time.monotonic() + 2, None))
    child.proc.wait(timeout=1)
    child.close()
    for _ in range(20):
        try:
            os.kill(descendant, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("ACP descendant survived after parent exit")


def test_no_cgroup_retains_uncertainty_for_session_escaped_descendant(tmp_path: Path) -> None:
    """Without cgroups, an escaped session cannot be disproven after close."""
    child = AcpSubprocess(
        helper(
            "import subprocess,sys,time; "
            "code=\"import os,time; os.setsid(); print(os.getpid(), flush=True); time.sleep(30)\"; "
            "subprocess.Popen([sys.executable, '-c', code], stdout=sys.stdout); time.sleep(30)"
        ),
        cwd=tmp_path, deadline=time.monotonic() + 3, log_path=tmp_path / "log", grace_s=0.05,
    )
    escaped = int(child.reader.read_with_deadline(40, time.monotonic() + 2, None))
    try:
        child.close()
        assert child.cleanup_uncertain
    finally:
        # This deliberately escaped the unavailable-cgroup fallback.  The
        # test removes its harmless helper explicitly so no test process leaks.
        try:
            os.kill(escaped, 9)
        except ProcessLookupError:
            pass
    _assert_helpers_gone([escaped])


def test_cgroup_cleanup_catches_descendant_escaped_from_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cgroup path is mocked: tests never create or alter host cgroups."""
    cgroup = tmp_path / "mock-cgroup"
    cgroup.mkdir()
    (cgroup / "cgroup.procs").write_text("")
    (cgroup / "cgroup.kill").write_text("")
    monkeypatch.setattr(acp_wire, "_adapter_cgroup", lambda: cgroup)
    monkeypatch.setattr(acp_wire, "_join_cgroup", lambda _path: None)
    child = AcpSubprocess(
        helper(
            "import subprocess,sys,time; "
            "code=\"import os,time; os.setsid(); print(os.getpid(), flush=True); time.sleep(30)\"; "
            "subprocess.Popen([sys.executable, '-c', code]); time.sleep(30)"
        ),
        cwd=tmp_path, deadline=time.monotonic() + 3, log_path=tmp_path / "log", grace_s=0.05,
    )
    escaped = int(child.reader.read_with_deadline(40, time.monotonic() + 2, None))
    (cgroup / "cgroup.procs").write_text(f"{escaped}\n")
    original_write_text = Path.write_text
    killed: list[Path] = []

    def mock_cgroup_kill(path: Path, data: str, *args: object, **kwargs: object) -> int:
        if path == cgroup / "cgroup.kill" and data == "1":
            killed.append(path)
            os.kill(escaped, 9)
            original_write_text(cgroup / "cgroup.procs", "")
        return original_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", mock_cgroup_kill)
    child.close()
    assert killed == [cgroup / "cgroup.kill"]
    assert not child.cleanup_uncertain
    _assert_helpers_gone([escaped])


def test_callback_failure_uses_cgroup_cleanup_for_escaped_descendant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callback errors retain containment semantics before they are re-raised."""
    cgroup = tmp_path / "mock-cgroup"
    cgroup.mkdir()
    (cgroup / "cgroup.procs").write_text("")
    (cgroup / "cgroup.kill").write_text("")
    pid_file = tmp_path / "escaped.pid"
    monkeypatch.setattr(acp_wire, "_adapter_cgroup", lambda: cgroup)
    monkeypatch.setattr(acp_wire, "_join_cgroup", lambda _path: None)
    original_write_text = Path.write_text
    killed: list[Path] = []
    escaped: list[int] = []

    def mock_cgroup_kill(path: Path, data: str, *args: object, **kwargs: object) -> int:
        if path == cgroup / "cgroup.kill" and data == "1":
            killed.append(path)
            os.kill(escaped[0], 9)
            original_write_text(cgroup / "cgroup.procs", "")
        return original_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", mock_cgroup_kill)

    def fail(_pid: int) -> None:
        for _ in range(100):
            if pid_file.exists():
                escaped.append(int(pid_file.read_text()))
                original_write_text(cgroup / "cgroup.procs", f"{escaped[0]}\n")
                raise RuntimeError("recording failed")
            time.sleep(0.01)
        raise AssertionError("escaped helper did not start")

    code = (
        "import subprocess,sys,time; "
        f"pidfile={str(pid_file)!r}; "
        "child=\"import os,time; os.setsid(); open(" + repr(str(pid_file))
        + ", 'w').write(str(os.getpid())); time.sleep(30)\"; "
        "subprocess.Popen([sys.executable, '-c', child]); time.sleep(30)"
    )
    with adapter_ownership(fail), pytest.raises(RuntimeError, match="recording failed"):
        AcpSubprocess(helper(code), cwd=tmp_path, deadline=time.monotonic() + 3,
                      log_path=tmp_path / "log", grace_s=0.05)
    assert killed == [cgroup / "cgroup.kill"]
    _assert_helpers_gone(escaped)


def test_uninspectable_owned_cgroup_is_retained_as_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cgroup = tmp_path / "missing-cgroup"
    monkeypatch.setattr(acp_wire, "_adapter_cgroup", lambda: cgroup)
    monkeypatch.setattr(acp_wire, "_join_cgroup", lambda _path: None)
    child = AcpSubprocess(helper("pass"), cwd=tmp_path, deadline=time.monotonic() + 2,
                          log_path=tmp_path / "log")
    child.close()
    assert child.cleanup_uncertain
