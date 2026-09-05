from nc.cli import main
from nc.config import Config
from nc.state import State


def test_health_empty_home(tmp_path, capsys):
    home = tmp_path / "new-home"

    assert main(["--home", str(home), "health"]) == 0

    assert capsys.readouterr().out.splitlines() == [
        f"database: {home / 'state.db'}",
        "tasks queued: 0",
        "tasks in_progress: 0",
        "tasks in_review: 0",
        "tasks done: 0",
        "tasks failed: 0",
        "tasks blocked: 0",
        "runnable agents: 0",
        "open incidents: 0",
    ]


def test_health_counts(tmp_path, capsys):
    state = State(Config.load(tmp_path).db_path)
    for project in ("one", "two"):
        state.add_project(project, project, str(tmp_path), None)
        for status in ("queued", "in_progress", "in_review", "done", "failed", "blocked"):
            task = state.add_task(project, status, "objective", [])
            state.set_task(task, status=status)
        for status in ("runnable", "blocked", "done", "failed"):
            agent = state.add_agent(f"{project}-{status}", "worker", project, None, "m")
            state.set_agent(agent, state=status)
    state.incident("test", "open one")
    state.incident("test", "open two")
    resolved = state.incident("test", "resolved")
    state.x("UPDATE incident SET resolved=1 WHERE id=?", (resolved,))
    state.db.close()

    assert main(["--home", str(tmp_path), "health"]) == 0

    assert capsys.readouterr().out.splitlines() == [
        f"database: {tmp_path / 'state.db'}",
        "tasks queued: 2",
        "tasks in_progress: 2",
        "tasks in_review: 2",
        "tasks done: 2",
        "tasks failed: 2",
        "tasks blocked: 2",
        "runnable agents: 2",
        "open incidents: 2",
    ]


def test_why_evidence(tmp_path, capsys):
    state = State(Config.load(tmp_path).db_path)
    state.add_project("demo", "Demo", str(tmp_path), None)
    task = state.add_task("demo", "Review me", "objective", ["$ pytest -q", "Works well"])
    other = state.add_task("demo", "Unrelated", "objective", [])
    state.set_task(task, status="done")
    for index, (role, outcome) in enumerate(
        [("worker", "ASK"), ("worker", "YIELD"), ("worker", "DONE"), ("critic", "DONE")]
    ):
        agent = state.add_agent(f"agent-{index}", role, "demo", task, "model")
        run = state.start_run(agent, task, role, "model", f"/logs/run-{index}.txt")
        state.end_run(run, outcome)
        state.x("UPDATE run SET started_at=100, ended_at=112.5 WHERE id=?", (run,))
    question = state.send("question", "agent-0", "owner", {"question": "Which color?"}, task)
    state.mark_delivered([question])
    state.send("answer", "owner", "agent-0", {"answer": "Blue"}, task, question)
    state.send("incident", "scheduler", "owner", {"reason": "unrelated message"}, other)
    state.start_run("agent-0", other, "worker", "model", "/logs/unrelated.txt")
    state.db.close()
    checks = tmp_path / "checks"
    checks.mkdir()
    (checks / f"{task}.txt").write_text("$ pytest -q\n42 passed\n")

    assert main(["--home", str(tmp_path), "why", task]) == 0

    output = capsys.readouterr().out
    assert f"{task}: Review me\nstatus: done" in output
    assert "$ pytest -q" in output
    assert "Works well" in output
    for index, (role, outcome) in enumerate(
        [("worker", "ASK"), ("worker", "YIELD"), ("worker", "DONE"), ("critic", "DONE")]
    ):
        assert (f"agent=agent-{index} role={role} outcome={outcome} "
                f"duration=12.5s log=/logs/run-{index}.txt") in output
    assert "agent-0 -> owner" in output
    assert "Which color?" in output
    assert "owner -> agent-0" in output
    assert "Blue" in output
    assert "$ pytest -q\n42 passed\n" in output
    assert "unrelated" not in output


def test_why_empty_and_running(tmp_path, capsys, monkeypatch):
    state = State(Config.load(tmp_path).db_path)
    state.add_project("demo", "Demo", str(tmp_path), None)
    task = state.add_task("demo", "New task", "objective", [])
    assert main(["--home", str(tmp_path), "why", task]) == 0
    output = capsys.readouterr().out
    assert "status: queued" in output
    for section in ("acceptance criteria", "runs", "messages"):
        assert f"{section}:\n  (none)" in output
    assert "(no stored check output)" in output

    agent = state.add_agent("worker", "worker", "demo", task, "model")
    run = state.start_run(agent, task, "worker", "model", "")
    state.x("UPDATE run SET started_at=100 WHERE id=?", (run,))
    state.db.close()
    monkeypatch.setattr("nc.cli.time.time", lambda: 115)
    assert main(["--home", str(tmp_path), "why", task]) == 0
    assert "outcome=running duration=15.0s elapsed log=(none)" in capsys.readouterr().out


def test_why_unknown_task(tmp_path, capsys):
    assert main(["--home", str(tmp_path), "why", "nonexistent-T001"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "unknown task: nonexistent-T001\n"
