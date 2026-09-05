"""End-to-end scheduler tests with a scripted adapter instead of a real CLI."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from nc import protocol
from nc.adapters import SessionResult
from nc.config import Config
from nc.scheduler import Scheduler
from nc.state import State

OUTCOME_RE = re.compile(r"(\S+/outcome\.json)")


class ScriptedAdapter:
    """Plays a list of (behaviour) callables, one per turn."""

    name = "scripted"

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[tuple[str, Path]] = []
        self.briefs: list[str] = []

    def available(self) -> bool:
        return True

    def run(self, prompt, cwd, model, log_path, timeout_s) -> SessionResult:
        self.briefs.append(prompt)
        outcome_path = Path(OUTCOME_RE.search(prompt).group(1))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("scripted session")
        step = self.script.pop(0) if self.script else None
        if step is not None:
            step(Path(cwd), outcome_path)
        self.calls.append((model, Path(cwd)))
        return SessionResult(exit_code=0, log_path=log_path, tokens=None, timed_out=False)


def emit(payload: dict):
    def step(cwd: Path, outcome_path: Path) -> None:
        outcome_path.parent.mkdir(parents=True, exist_ok=True)
        outcome_path.write_text(json.dumps(payload))
    return step


def commit_and_emit(filename: str, content: str, payload: dict):
    def step(cwd: Path, outcome_path: Path) -> None:
        (cwd / filename).write_text(content)
        subprocess.run(["git", "add", filename], cwd=cwd, check=True)
        subprocess.run(["git", "commit", "-m", "work"], cwd=cwd, check=True)
        outcome_path.parent.mkdir(parents=True, exist_ok=True)
        outcome_path.write_text(json.dumps(payload))
    return step


def nothing(cwd: Path, outcome_path: Path) -> None:
    """A turn that ends without writing an outcome file."""


@pytest.fixture
def repo(tmp_path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "nc@test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "nc"], cwd=path, check=True)
    (path / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=path, check=True)
    return path


@pytest.fixture
def setup(tmp_path, repo):
    cfg = Config(home=tmp_path / "home")
    cfg.turn_timeout_s = 5
    state = State(cfg.db_path)
    state.add_project("neocortex", "Neocortex", str(repo), None)
    return cfg, state, repo


def sched(cfg, state, script) -> Scheduler:
    scheduler = Scheduler(cfg, state)
    scheduler.adapter = ScriptedAdapter(script)
    return scheduler


def test_accepted_task_is_merged_only_after_a_passing_critic(setup):
    cfg, state, repo = setup
    tid = state.add_task("neocortex", "add marker", "create marker.txt",
                         ["$ test -f marker.txt", "the file says hello"])

    scheduler = sched(cfg, state, [
        commit_and_emit("marker.txt", "hello\n",
                        {"outcome": "DONE", "summary": "created marker.txt"}),
        emit({"outcome": "DONE", "verdict": "pass", "summary": "criteria met"}),
    ])

    assert scheduler.step() == protocol.DONE            # worker
    task = state.one("SELECT * FROM task WHERE id=?", (tid,))
    assert task["status"] == "in_review"                # worker cannot accept its own work
    assert state.one("SELECT * FROM agent WHERE id=?", (f"worker-{tid}",))["state"] == "blocked"

    assert scheduler.step() == protocol.DONE            # critic
    task = state.one("SELECT * FROM task WHERE id=?", (tid,))
    assert task["status"] == "done"
    assert "merged as" in task["result"]
    assert (repo / "marker.txt").exists()               # merged into main
    assert scheduler.step() == "idle"


def test_failing_acceptance_check_sends_the_worker_back_without_calling_the_critic(setup):
    cfg, state, _repo = setup
    tid = state.add_task("neocortex", "add marker", "create marker.txt",
                         ["$ test -f marker.txt"])

    scheduler = sched(cfg, state, [
        commit_and_emit("other.txt", "nope\n",
                        {"outcome": "DONE", "summary": "all done, trust me"}),
        commit_and_emit("marker.txt", "hello\n", {"outcome": "DONE", "summary": "fixed"}),
        emit({"outcome": "DONE", "verdict": "pass", "summary": "ok"}),
    ])

    scheduler.step()
    assert state.one("SELECT * FROM task WHERE id=?", (tid,))["status"] == "in_progress"
    assert state.one("SELECT * FROM agent WHERE id=?", (f"worker-{tid}",))["state"] == "runnable"
    assert state.q("SELECT * FROM agent WHERE role='critic'") == []

    scheduler.step()
    scheduler.step()
    assert state.one("SELECT * FROM task WHERE id=?", (tid,))["status"] == "done"

    # the worker was told what failed, in its brief, on the retry
    assert "test -f marker.txt" in scheduler.adapter.briefs[1]


def test_critic_never_sees_the_worker_self_report(setup):
    cfg, state, _repo = setup
    state.add_task("neocortex", "add marker", "create marker.txt", ["$ test -f marker.txt"])
    scheduler = sched(cfg, state, [
        commit_and_emit("marker.txt", "hi\n",
                        {"outcome": "DONE", "summary": "MAGIC-SELF-REPORT", "memo": "MAGIC-MEMO"}),
        emit({"outcome": "DONE", "verdict": "pass", "summary": "ok"}),
    ])
    scheduler.step()
    scheduler.step()
    critic_brief = scheduler.adapter.briefs[1]
    assert "MAGIC-SELF-REPORT" not in critic_brief
    assert "MAGIC-MEMO" not in critic_brief
    assert "git diff" in critic_brief


def test_rework_verdict_reopens_the_task_with_findings(setup):
    cfg, state, _repo = setup
    tid = state.add_task("neocortex", "add marker", "create marker.txt", [])
    scheduler = sched(cfg, state, [
        commit_and_emit("marker.txt", "hi\n", {"outcome": "DONE", "summary": "done"}),
        emit({"outcome": "DONE", "verdict": "rework", "summary": "not quite",
              "findings": ["marker.txt must end with a newline"]}),
        commit_and_emit("marker.txt", "hi, with a newline\n",
                        {"outcome": "DONE", "summary": "fixed"}),
    ])
    scheduler.step()
    scheduler.step()
    task = state.one("SELECT * FROM task WHERE id=?", (tid,))
    assert (task["status"], task["attempts"]) == ("in_progress", 1)

    scheduler.step()
    assert "must end with a newline" in scheduler.adapter.briefs[2]


def test_ask_suspends_the_agent_until_the_owner_answers(setup):
    cfg, state, _repo = setup
    tid = state.add_task("neocortex", "add marker", "create marker.txt", [])
    scheduler = sched(cfg, state, [
        emit({"outcome": "ASK", "to": "owner", "question": "which filename?",
              "memo": "waiting on the owner"}),
        commit_and_emit("marker.txt", "hi\n", {"outcome": "DONE", "summary": "done"}),
    ])

    assert scheduler.step() == protocol.ASK
    agent = state.one("SELECT * FROM agent WHERE id=?", (f"worker-{tid}",))
    assert agent["state"] == "blocked"
    assert agent["memo"] == "waiting on the owner"
    assert state.one("SELECT * FROM task WHERE id=?", (tid,))["status"] == "blocked"
    assert scheduler.step() == "idle"                   # nothing runnable while blocked

    question = state.inbox("owner")[0]
    assert json.loads(question["payload"])["question"] == "which filename?"

    state.send(protocol.ANSWER, "owner", f"worker-{tid}", {"answer": "marker.txt"},
               task_id=tid, in_reply_to=question["id"])
    state.set_agent(f"worker-{tid}", state="runnable")

    assert scheduler.step() == protocol.DONE
    brief = scheduler.adapter.briefs[1]
    assert "marker.txt" in brief
    assert "waiting on the owner" in brief              # memo survived the suspension


def test_missing_outcome_file_counts_as_a_failed_turn(setup):
    cfg, state, _repo = setup
    tid = state.add_task("neocortex", "t", "obj", [])
    scheduler = sched(cfg, state, [nothing, nothing, nothing])

    assert scheduler.step() == protocol.NO_OUTCOME
    assert state.one("SELECT * FROM task WHERE id=?", (tid,))["attempts"] == 1
    scheduler.step()
    scheduler.step()
    task = state.one("SELECT * FROM task WHERE id=?", (tid,))
    assert task["status"] == "blocked"
    assert task["attempts"] == 3
    assert [i["kind"] for i in state.open_incidents()] == ["task_blocked"]


def test_circuit_breaker_stops_the_loop(setup):
    cfg, state, _repo = setup
    cfg.max_consecutive_failures = 2
    state.add_task("neocortex", "t", "obj", [])
    scheduler = sched(cfg, state, [nothing] * 5)
    scheduler.preflight = lambda: (True, "test")

    scheduler.run()
    assert scheduler.consecutive_failures >= 2
    assert any(i["kind"] == "circuit_breaker" for i in state.open_incidents())


def test_turn_budget_is_enforced(setup):
    cfg, state, _repo = setup
    tid = state.add_task("neocortex", "t", "obj", [], budget_turns=2)
    scheduler = sched(cfg, state, [
        emit({"outcome": "YIELD", "summary": "step 1", "memo": "continue"}),
        emit({"outcome": "YIELD", "summary": "step 2", "memo": "continue"}),
    ])
    scheduler.step()
    scheduler.step()
    assert scheduler.step() == "budget_exhausted"
    assert state.one("SELECT * FROM task WHERE id=?", (tid,))["status"] == "blocked"


def test_projects_are_isolated_and_priority_wins(setup, tmp_path):
    cfg, state, _repo = setup
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-b", "main", "-q"], cwd=other, check=True)
    subprocess.run(["git", "config", "user.email", "nc@test"], cwd=other, check=True)
    subprocess.run(["git", "config", "user.name", "nc"], cwd=other, check=True)
    (other / "f").write_text("x")
    subprocess.run(["git", "add", "f"], cwd=other, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=other, check=True)
    state.add_project("aiscreeps", "AIScreeps", str(other), None)

    state.add_task("neocortex", "low", "obj", [], priority=200)
    urgent = state.add_task("aiscreeps", "high", "obj", [], priority=10)

    scheduler = sched(cfg, state, [emit({"outcome": "YIELD", "summary": "s"})])
    scheduler.step()
    assert state.one("SELECT * FROM task WHERE id=?", (urgent,))["status"] == "in_progress"
    assert scheduler.adapter.calls[0][1].name == urgent
