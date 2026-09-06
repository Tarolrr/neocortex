import sqlite3

import pytest

from nc.cli import main
from nc.config import Config
from nc.scheduler import Scheduler
from nc.state import SCHEMA, State


@pytest.fixture
def incidents(tmp_path):
    cfg = Config(home=tmp_path)
    state = State(cfg.db_path)
    yield cfg, state
    state.db.close()


def test_resolve_one_incident_preserves_state_and_timeout_deduplication(incidents, capsys):
    cfg, state = incidents
    state.add_project("demo", "Demo", ".", None)
    task = state.add_task("demo", "Task", "objective", [])
    state.set_task(task, status="blocked")
    agent = state.add_agent(f"worker-{task}", "worker", "demo", task, "m")
    state.set_agent(agent, state="blocked")
    state.x("UPDATE agent SET updated_at=0 WHERE id=?", (agent,))
    state.send("question", agent, "owner", {"question": "help?"}, task)
    scheduler = Scheduler(cfg, state)
    scheduler._escalate_unanswered_questions()
    selected = dict(state.open_incidents()[0])
    other = state.incident("test", "keep this open")
    stop = cfg.home / "STOP"
    stop.write_text("owner stopped")
    before_task = dict(state.one("SELECT * FROM task"))
    before_agent = dict(state.one("SELECT * FROM agent"))
    before_messages = [dict(row) for row in state.q("SELECT * FROM message")]

    assert main(["--home", str(cfg.home), "resolve", str(selected["id"]),
                 "--reason", "Acknowledged; investigating separately"]) == 0
    reopened = State(cfg.db_path)
    resolved = dict(reopened.one("SELECT * FROM incident WHERE id=?", (selected["id"],)))
    reopened.db.close()
    assert resolved["resolved"] == 1
    assert resolved["resolved_at"] >= selected["created_at"]
    assert resolved["resolution_note"] == "Acknowledged; investigating separately"
    for key in ("id", "kind", "detail", "created_at"):
        assert resolved[key] == selected[key]
    assert [r["id"] for r in state.open_incidents()] == [other]
    assert stop.read_text() == "owner stopped"
    assert dict(state.one("SELECT * FROM task")) == before_task
    assert dict(state.one("SELECT * FROM agent")) == before_agent
    assert [dict(row) for row in state.q("SELECT * FROM message")] == before_messages
    scheduler._escalate_unanswered_questions()
    assert len(state.q("SELECT * FROM incident")) == 2

    capsys.readouterr()
    assert main(["--home", str(cfg.home), "incidents"]) == 0
    output = capsys.readouterr().out
    assert selected["detail"] not in output
    assert "keep this open" in output
    assert main(["--home", str(cfg.home), "incidents", "--all"]) == 0
    output = capsys.readouterr().out
    assert selected["detail"] in output
    assert "resolved at " in output
    assert resolved["resolution_note"] in output


def test_unknown_and_repeated_resolution_do_not_write(incidents, capsys):
    cfg, state = incidents
    incident = state.incident("test", "original detail")
    assert main(["--home", str(cfg.home), "resolve", str(incident), "--reason", "first"]) == 0
    before = list(state.db.iterdump())
    assert main(["--home", str(cfg.home), "resolve", "999", "--reason", "unknown"]) == 1
    assert "unknown incident" in capsys.readouterr().err
    assert list(state.db.iterdump()) == before
    assert main(["--home", str(cfg.home), "resolve", str(incident), "--reason", "second"]) == 0
    assert "already resolved" in capsys.readouterr().out
    assert list(state.db.iterdump()) == before


@pytest.mark.parametrize("retry", [False, True])
def test_resume_bulk_resolution(incidents, retry):
    cfg, state = incidents
    state.add_project("demo", "Demo", ".", None)
    task = state.add_task("demo", "Task", "objective", [])
    state.set_task(task, status="blocked")
    agent = state.add_agent(f"worker-{task}", "worker", "demo", task, "m")
    state.set_agent(agent, state="blocked")
    closed = state.incident("test", "previously closed")
    state.resolve_incident(closed, "original note")
    original = dict(state.one("SELECT * FROM incident WHERE id=?", (closed,)))
    for detail in ("one", "two"):
        state.incident("test", detail)
    (cfg.home / "STOP").write_text("stopped")
    assert main(["--home", str(cfg.home), "resume", *(["--retry"] if retry else [])]) == 0
    assert not (cfg.home / "STOP").exists()
    assert not state.open_incidents()
    assert dict(state.one("SELECT * FROM incident WHERE id=?", (closed,))) == original
    for row in state.q("SELECT * FROM incident WHERE id != ?", (closed,)):
        assert row["resolved_at"] >= row["created_at"]
        assert row["resolution_note"] == "Closed by nc resume"
        assert row["detail"] in ("one", "two")
    assert state.one("SELECT status FROM task")["status"] == ("in_progress" if retry else "blocked")
    assert state.one("SELECT state FROM agent")["state"] == ("runnable" if retry else "blocked")


def test_existing_database_migration(tmp_path, capsys):
    cfg = Config(home=tmp_path)
    db = sqlite3.connect(cfg.db_path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO incident(kind,detail,resolved,created_at) VALUES('old','open',0,1)")
    db.execute("INSERT INTO incident(kind,detail,resolved,created_at) VALUES('old','closed',1,2)")
    db.commit()
    db.close()
    for _ in range(2):
        state = State(cfg.db_path)
        rows = state.q("SELECT * FROM incident ORDER BY id")
        assert [(r["detail"], r["resolved"], r["created_at"]) for r in rows] == [
            ("open", 0, 1), ("closed", 1, 2),
        ]
        assert all(r["resolved_at"] is None and r["resolution_note"] is None for r in rows)
        state.db.close()
    assert main(["--home", str(tmp_path), "incidents", "--all"]) == 0
    assert "resolved at unknown: (no recorded note)" in capsys.readouterr().out
    assert main(["--home", str(tmp_path), "resolve", "1", "--reason", "migrated"]) == 0


@pytest.mark.parametrize("reason", ["", "   "])
def test_empty_resolution_reason(incidents, reason):
    cfg, state = incidents
    incident = state.incident("test", "detail")
    before = list(state.db.iterdump())
    assert main(["--home", str(cfg.home), "resolve", str(incident), "--reason", reason]) == 1
    assert list(state.db.iterdump()) == before
