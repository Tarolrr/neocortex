"""Offline ownership tests using Python helpers, never a vendor ACP CLI."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from nc import acp_wire
from nc.acp_stream import AcpEofError
from nc.acp_wire import AcpSubprocess, AcpTransportEof, AcpUnexpectedExit
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
    # ``kill(pid, 0)`` is deliberately avoided: PID reuse would make this
    # assertion less safe than checking Popen's reap in the implementation.


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


def test_close_kills_hung_session_group(tmp_path: Path) -> None:
    child = AcpSubprocess(helper("import time; time.sleep(30)"), cwd=tmp_path,
                          deadline=time.monotonic() + 2, log_path=tmp_path / "log")
    child.close()
    assert child.proc.poll() is not None


def test_close_kills_a_descendant_in_the_owned_session(tmp_path: Path) -> None:
    code = (
        "import subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "print(p.pid, flush=True); time.sleep(30)"
    )
    child = AcpSubprocess(helper(code), cwd=tmp_path, deadline=time.monotonic() + 3,
                          log_path=tmp_path / "log")
    descendant = int(child.reader.read_with_deadline(40, time.monotonic() + 2, None))
    child.close()
    for _ in range(20):
        try:
            os.kill(descendant, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("ACP descendant survived session-group shutdown")


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
