"""Interrupted-run recovery: evidence is not mistaken for task liveness."""

import os
import signal
import subprocess

import pytest

from nc import operations
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


def test_surviving_adapter_group_refuses_then_selected_recovery(recovery_state):
    _cfg, state, task = recovery_state
    run = state.start_run("worker", task, "worker", "m", "log")
    # A disposable adapter session has a child; recovery must see it even when
    # the recorded scheduler process has disappeared.
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
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
    operations.recover_runs(state, [run], "scheduler interrupted")
    saved = state.one("SELECT * FROM run WHERE id=?", (run,))
    assert saved["outcome"] == "INTERRUPTED"
    assert saved["recovery_reason"] == "scheduler interrupted"
    assert saved["ended_at"] is not None


def test_legacy_requires_explicit_quiescence_and_preserves_task(recovery_state):
    _cfg, state, task = recovery_state
    run = state.start_run("planner", None, "planner", "m", "log")
    state.x("UPDATE run SET owner_pid=NULL, owner_start=NULL WHERE id=?", (run,))
    before = dict(state.one("SELECT * FROM task WHERE id=?", (task,)))
    with pytest.raises(ValueError, match="acknowledge"):
        operations.recover_runs(state, [run], "verified")
    operations.recover_runs(state, [run], "verified scheduler and adapter processes", True)
    assert dict(state.one("SELECT * FROM task WHERE id=?", (task,))) == before


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
