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
