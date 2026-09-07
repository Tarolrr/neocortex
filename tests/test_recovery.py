"""Interrupted-run recovery: evidence is not mistaken for task liveness."""

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from nc import operations
from nc.adapters import _adapter_cgroup, _run, adapter_ownership
from nc.config import Config
from nc.state import State


@pytest.fixture
def recovery_state(tmp_path):
    cfg = Config(home=tmp_path / "home")
    cfg.home.mkdir()
    state = State(cfg.db_path)
    state.add_project("one", "One", str(tmp_path), None)
    task = state.add_task("one", "blocked", "objective", [])
    state.set_task(task, status="blocked")
    state.add_agent("worker", "worker", "one", task, "m")
    state.add_agent("planner", "planner", "one", None, "m")
    yield cfg, state, task
    state.db.close()


def adapter_process(state, run, *cmd):
    """Make test ownership use the same dedicated containment as adapters."""
    proc = subprocess.Popen(list(cmd), start_new_session=True)
    cgroup = _adapter_cgroup()
    assert cgroup is not None
    (cgroup / "cgroup.procs").write_text(str(proc.pid))
    state.record_adapter_owner(run, proc.pid)
    return proc, cgroup


def test_surviving_adapter_group_refuses_then_selected_recovery(recovery_state):
    _cfg, state, task = recovery_state
    run = state.start_run("worker", task, "worker", "m", "log")
    # A disposable adapter session has a child; recovery must see it even when
    # the recorded scheduler process has disappeared.
    proc, cgroup = adapter_process(state, run, "sleep", "30")
    try:
        state.record_adapter_owner(run, proc.pid)
        state.x("UPDATE run SET owner_pid=99999999, owner_start='gone' WHERE id=?", (run,))
        rows = operations.unfinished_runs(state)
        assert "live adapter process or descendant" in rows[0]["ownership"]
        with pytest.raises(ValueError, match="live ownership"):
            operations.recover_runs(state, [run], "scheduler interrupted")
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        cgroup.rmdir()
    operations.recover_runs(state, [run], "scheduler interrupted")
    saved = state.one("SELECT * FROM run WHERE id=?", (run,))
    assert saved["outcome"] == "INTERRUPTED"
    assert saved["recovery_reason"] == "scheduler interrupted"
    assert saved["ended_at"] is not None


def test_legacy_requires_explicit_quiescence_and_preserves_task(recovery_state):
    _cfg, state, task = recovery_state
    run = state.start_run("planner", None, "planner", "m", "log")
    state.x("UPDATE run SET owner_pid=NULL, owner_start=NULL, ownership_version=0 WHERE id=?", (run,))
    before = dict(state.one("SELECT * FROM task WHERE id=?", (task,)))
    with pytest.raises(ValueError, match="acknowledge"):
        operations.recover_runs(state, [run], "verified")
    operations.recover_runs(state, [run], "verified scheduler and adapter processes", True)
    assert dict(state.one("SELECT * FROM task WHERE id=?", (task,))) == before


def test_recovery_preserves_long_original_detail_and_mixed_selection_is_atomic(recovery_state):
    _cfg, state, task = recovery_state
    one = state.start_run("worker", task, "worker", "m", "log")
    two = state.start_run("planner", None, "planner", "m", "log")
    original = "adapter traceback\n" + "x" * 5000
    state.x("UPDATE run SET detail=?, owner_pid=NULL, owner_start=NULL, ownership_version=0 WHERE id=?",
            (original, one))
    # A live member makes the whole submitted set fail; the legacy row must
    # not be partially recovered just because it appeared first.
    proc, cgroup = adapter_process(state, two, "sleep", "30")
    try:
        state.record_adapter_owner(two, proc.pid)
        state.x("UPDATE run SET owner_pid=99999999, owner_start='gone' WHERE id=?", (two,))
        with pytest.raises(ValueError, match="live ownership"):
            operations.recover_runs(state, [one, two], "mixed selection", True)
        assert state.one("SELECT ended_at FROM run WHERE id=?", (one,))[0] is None
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        cgroup.rmdir()
    operations.recover_runs(state, [one], "verified quiescence", True)
    saved = state.one("SELECT * FROM run WHERE id=?", (one,))
    assert saved["detail"] == original
    assert saved["recovery_reason"] == "verified quiescence"
    assert saved["interrupted_at"] == saved["recovered_at"]


def test_terminated_disposable_scheduler_blocks_then_allows_budget_requeue(recovery_state, tmp_path):
    """Registration belongs to a disposable scheduler, not this test process.

    Its adapter is separately sessioned, so killing the scheduler first proves
    that an unfinished record blocks requeue while no agent is runnable, and
    that a surviving descendant still vetoes recovery.
    """
    cfg, state, task = recovery_state
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "README").write_text("seed\n")
    subprocess.run(["git", "add", "README"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)
    state.x("UPDATE project SET repo_path=? WHERE id='one'", (str(repo),))
    state.x("UPDATE agent SET state='blocked'")
    code = textwrap.dedent("""
        import subprocess, sys, time
        from nc.adapters import _adapter_cgroup
        from nc.state import State
        state = State(sys.argv[1])
        run = state.start_run('worker', sys.argv[2], 'worker', 'm', 'log')
        adapter = subprocess.Popen(['sleep', '30'], start_new_session=True)
        cgroup = _adapter_cgroup()
        assert cgroup is not None
        (cgroup / 'cgroup.procs').write_text(str(adapter.pid))
        state.record_adapter_owner(run, adapter.pid)
        print(run, adapter.pid, flush=True)
        time.sleep(30)
    """)
    scheduler = subprocess.Popen([sys.executable, "-c", code, str(cfg.db_path), task],
                                 stdout=subprocess.PIPE, text=True)
    line = scheduler.stdout.readline().strip()
    run, adapter_pid = map(int, line.split())
    scheduler.terminate()
    scheduler.wait(timeout=5)
    assert state.q("SELECT * FROM agent WHERE state='runnable'") == []
    with pytest.raises(ValueError, match=fr"#{run}"):
        operations.requeue_task(cfg, state, task, budget=9)
    with pytest.raises(ValueError, match="live ownership"):
        operations.recover_runs(state, [run], "scheduler was terminated")
    os.killpg(adapter_pid, signal.SIGKILL)
    operations.recover_runs(state, [run], "scheduler terminated after registration")
    result = operations.requeue_task(cfg, state, task, budget=9)
    assert result["budget"] == 9
    saved_task = state.one("SELECT status, budget_turns FROM task WHERE id=?", (task,))
    assert tuple(saved_task) == ("queued", 9)


def test_cgroup_detects_descendant_that_escapes_adapter_process_group(recovery_state, tmp_path):
    """setsid defeats pgrp tracking but cannot leave launch-time containment."""
    _cfg, state, task = recovery_state
    run = state.start_run("worker", task, "worker", "m", "log")
    child_pid = tmp_path / "escaped.pid"
    code = (
        "import pathlib, os, subprocess, sys; "
        "p=subprocess.Popen([sys.executable, '-c', "
        "'import os,time; os.setsid(); time.sleep(30)']); "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(p.pid))"
    )
    with adapter_ownership(lambda pid: state.record_adapter_owner(run, pid)):
        _run([sys.executable, "-c", code], tmp_path, tmp_path / "adapter.log", 10)
    escaped = int(child_pid.read_text())
    state.x("UPDATE run SET owner_pid=99999999, owner_start='gone' WHERE id=?", (run,))
    try:
        ownership = operations.unfinished_runs(state)[0]["ownership"]
        assert "live adapter process or descendant" in ownership
        assert "cgroup" in ownership
        with pytest.raises(ValueError, match="live ownership"):
            operations.recover_runs(state, [run], "scheduler interrupted")
    finally:
        os.kill(escaped, signal.SIGKILL)
        for _ in range(50):
            try:
                status = Path(f"/proc/{escaped}/stat").read_text().rsplit(") ", 1)[1].split()[0]
            except FileNotFoundError:
                break
            if status == "Z":
                break
            time.sleep(0.01)
    operations.recover_runs(state, [run], "scheduler interrupted after escaped child ended")


def test_incomplete_new_ownership_is_not_overridable(recovery_state):
    _cfg, state, task = recovery_state
    run = state.start_run("worker", task, "worker", "m", "log")
    # Simulates a scheduler death after run registration but before adapter
    # ownership can be persisted.  This is not a legacy row.
    state.x("UPDATE run SET owner_pid=99999999, owner_start='gone' WHERE id=?", (run,))
    with pytest.raises(ValueError, match="ambiguous new ownership"):
        operations.recover_runs(state, [run], "operator checked", True)


def test_blocker_diagnostic_includes_cross_project_and_taskless_rows(recovery_state):
    cfg, state, task = recovery_state
    state.add_project("two", "Two", str(cfg.home), None)
    other = state.add_task("two", "other", "objective", [])
    state.add_agent("other-worker", "worker", "two", other, "m")
    one = state.start_run("worker", task, "worker", "m", "log")
    two = state.start_run("planner", None, "planner", "m", "log")
    three = state.start_run("other-worker", other, "worker", "m", "log")
    text = operations.unfinished_blockers(state)
    for run in (one, two, three):
        assert f"#{run}" in text
    assert "taskless planner" in text
    assert "agent=other-worker" in text
