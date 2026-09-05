import json

from nc import protocol
from nc.state import State


def make_state(tmp_path) -> State:
    state = State(tmp_path / "state.db")
    state.add_project("neocortex", "Neocortex", str(tmp_path / "repo"), "pytest -q")
    return state


def test_task_ids_are_per_project_and_do_not_collide(tmp_path):
    state = make_state(tmp_path)
    state.add_project("aiscreeps", "AIScreeps", str(tmp_path / "s"), None)
    a = state.add_task("neocortex", "one", "do one", ["x"])
    b = state.add_task("neocortex", "two", "do two", ["x"])
    c = state.add_task("aiscreeps", "one", "do one", ["x"])
    assert (a, b, c) == ("neocortex-T001", "neocortex-T002", "aiscreeps-T001")

    state.x("DELETE FROM task WHERE id=?", (b,))
    assert state.add_task("neocortex", "three", "do three", ["x"]) == "neocortex-T003"


def test_task_fields_roundtrip(tmp_path):
    state = make_state(tmp_path)
    tid = state.add_task("neocortex", "t", "obj", ["$ pytest -q", "code is readable"],
                         boundaries=["do not touch systemd"], budget_turns=3)
    row = state.one("SELECT * FROM task WHERE id=?", (tid,))
    assert row["status"] == "queued"
    assert json.loads(row["acceptance"]) == ["$ pytest -q", "code is readable"]
    assert json.loads(row["boundaries"]) == ["do not touch systemd"]
    assert row["budget_turns"] == 3

    state.set_task(tid, status="in_review", attempts=1)
    row = state.one("SELECT * FROM task WHERE id=?", (tid,))
    assert (row["status"], row["attempts"]) == ("in_review", 1)


def test_inbox_delivers_once(tmp_path):
    state = make_state(tmp_path)
    tid = state.add_task("neocortex", "t", "obj", ["x"])
    state.add_agent("worker-1", "worker", "neocortex", tid, "m")
    mid = state.send(protocol.QUESTION, "worker-1", "owner", {"question": "which port?"},
                     task_id=tid)

    pending = state.inbox("owner")
    assert [r["id"] for r in pending] == [mid]
    state.mark_delivered([mid])
    assert state.inbox("owner") == []
    assert len(state.inbox("owner", undelivered_only=False)) == 1


def test_runs_and_incidents(tmp_path):
    state = make_state(tmp_path)
    tid = state.add_task("neocortex", "t", "obj", ["x"])
    state.add_agent("worker-1", "worker", "neocortex", tid, "m")
    run_id = state.start_run("worker-1", tid, "worker", "m", "/tmp/log")
    state.end_run(run_id, protocol.DONE, "did the thing", tokens=42)
    row = state.one("SELECT * FROM run WHERE id=?", (run_id,))
    assert (row["outcome"], row["tokens"]) == (protocol.DONE, 42)
    assert row["ended_at"] is not None

    state.incident("preflight", "model unavailable")
    assert len(state.open_incidents()) == 1
