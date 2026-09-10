"""End-to-end scheduler tests with a scripted adapter instead of a real CLI."""

from __future__ import annotations

import json
import multiprocessing
import re
import subprocess
import time
from pathlib import Path

import pytest

from nc import cli, operations, protocol, turn
from nc.adapters import SessionResult
from nc.config import Config
from nc.lifecycle import LifecycleBusy, lifecycle_lock
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


def _run_scheduler_to_barrier(db_path: str, home: str, phase: str, entered, release) -> None:
    """Run a real turn and pause at a protected scheduler phase in a child."""
    state = State(Path(db_path), initialize=False)
    try:
        scheduler = Scheduler(Config(home=Path(home)), state)
        scheduler.adapter = ScriptedAdapter([emit({"outcome": "DONE", "verdict": "pass"})])
        scheduler._adapter_for = lambda role: scheduler.adapter

        def pause(current):
            if current == phase:
                entered.set()
                release.wait(10)

        scheduler._lifecycle_hook = pause
        scheduler.step()
    finally:
        state.db.close()


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
    scheduler._adapter_for = lambda role: scheduler.adapter
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


def test_merge_conflict_after_a_pass_sends_the_worker_back_without_an_attempt(setup):
    cfg, state, repo = setup
    tid = state.add_task("neocortex", "update README", "update README", [])

    def resolve_conflict(cwd: Path, outcome_path: Path) -> None:
        subprocess.run(["git", "merge", "main"], cwd=cwd, check=False,
                       capture_output=True, text=True)
        (cwd / "README.md").write_text("from main\nfrom worker\n")
        subprocess.run(["git", "add", "README.md"], cwd=cwd, check=True)
        subprocess.run(["git", "commit", "-m", "merged"], cwd=cwd, check=True)
        outcome_path.parent.mkdir(parents=True, exist_ok=True)
        outcome_path.write_text(json.dumps({
            "outcome": "DONE", "summary": "resolved the merge conflict",
        }))

    scheduler = sched(cfg, state, [
        commit_and_emit("README.md", "from worker\n",
                        {"outcome": "DONE", "summary": "updated README"}),
        emit({"outcome": "DONE", "verdict": "pass", "summary": "criteria met"}),
        resolve_conflict,
        emit({"outcome": "DONE", "verdict": "pass", "summary": "resolved"}),
    ])

    assert scheduler.step() == protocol.DONE
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True)
    (repo / "README.md").write_text("from main\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "main update"], cwd=repo, check=True)

    assert scheduler.step() == protocol.DONE
    task = state.one("SELECT * FROM task WHERE id=?", (tid,))
    assert (task["status"], task["attempts"]) == ("in_progress", 0)
    assert state.one("SELECT * FROM agent WHERE id=?", (f"worker-{tid}",))["state"] == "runnable"
    assert state.one("SELECT * FROM agent WHERE id=?", (f"critic-{tid}-1",))["state"] == "done"
    assert state.one("SELECT * FROM incident WHERE kind='merge_conflict'")
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout == ""
    assert (repo / "README.md").read_text() == "from main\n"

    assert scheduler.step() == protocol.DONE
    assert "no longer merges into main" in scheduler.adapter.briefs[2]
    assert scheduler.step() == protocol.DONE
    assert state.one("SELECT * FROM task WHERE id=?", (tid,))["status"] == "done"
    assert (repo / "README.md").read_text() == "from main\nfrom worker\n"


def test_critic_done_without_verdict_retries_the_review_instead_of_reworking(setup):
    cfg, state, _repo = setup
    tid = state.add_task("neocortex", "add marker", "create marker.txt", [])
    scheduler = sched(cfg, state, [
        commit_and_emit("marker.txt", "hello\n",
                        {"outcome": "DONE", "summary": "created marker.txt"}),
        emit({"outcome": "DONE", "summary": "Verdict: pass. Findings: none."}),
        emit({"outcome": "DONE", "verdict": "pass", "summary": "criteria met"}),
    ])

    assert scheduler.step() == protocol.DONE            # worker
    assert scheduler.step() == protocol.DONE            # critic forgot `verdict`
    task = state.one("SELECT * FROM task WHERE id=?", (tid,))
    assert (task["status"], task["attempts"]) == ("in_review", 1)
    assert state.one("SELECT * FROM agent WHERE id=?", (f"worker-{tid}",))["state"] == "blocked"
    assert state.one("SELECT * FROM agent WHERE id=?", (f"critic-{tid}-1",))["state"] == "runnable"
    assert not state.q("SELECT * FROM message WHERE kind='review_verdict'")
    assert "without a verdict" in state.one("SELECT * FROM incident WHERE kind='protocol'")["detail"]
    assert "verdict" in scheduler.adapter.briefs[1]

    assert scheduler.step() == protocol.DONE            # same critic, proper verdict
    assert state.one("SELECT * FROM task WHERE id=?", (tid,))["status"] == "done"


def test_unexpected_handler_error_blocks_the_task_instead_of_crashing(setup, monkeypatch):
    cfg, state, _repo = setup
    tid = state.add_task("neocortex", "add marker", "create marker.txt", [])
    scheduler = sched(cfg, state, [
        commit_and_emit("marker.txt", "hello\n",
                        {"outcome": "DONE", "summary": "created marker.txt"}),
        emit({"outcome": "DONE", "verdict": "pass", "summary": "criteria met"}),
    ])
    monkeypatch.setattr("nc.scheduler.arbiter.integrate",
                        lambda *_args: (_ for _ in ()).throw(RuntimeError("boom")))

    assert scheduler.step() == protocol.DONE
    assert scheduler.step() == "error"
    assert state.one("SELECT * FROM task WHERE id=?", (tid,))["status"] == "blocked"
    assert state.one("SELECT * FROM incident WHERE kind='handler_error'")
    assert scheduler.step() == "idle"


def test_accepted_work_is_mirrored_and_can_be_reverted(setup, tmp_path):
    cfg, state, repo = setup
    subprocess.run(["git", "branch", "-m", "trunk"], cwd=repo, check=True)
    remote = tmp_path / "mirror.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "trunk", str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "mirror", str(remote)], cwd=repo, check=True)
    state.add_project("neocortex", "Neocortex", str(repo), None, mirror="mirror")

    tid = state.add_task("neocortex", "add marker", "create marker.txt", [])
    scheduler = sched(cfg, state, [
        commit_and_emit("marker.txt", "hi\n", {"outcome": "DONE", "summary": "done"}),
        emit({"outcome": "DONE", "verdict": "pass", "summary": "ok"}),
    ])
    scheduler.step()
    scheduler.step()

    task = state.one("SELECT * FROM task WHERE id=?", (tid,))
    assert task["merge_commit"]
    mirrored = subprocess.run(["git", "log", "--oneline", "trunk"], cwd=remote,
                              capture_output=True, text=True, check=True).stdout
    assert f"{tid}: accepted by arbiter" in mirrored
    assert subprocess.run(["git", "rev-parse", "--verify", f"refs/heads/nc/{tid}"],
                          cwd=remote, capture_output=True, check=False).returncode == 0
    # New mirrors publish the real base, never the retired nc/<base> alias.
    assert subprocess.run(["git", "rev-parse", "--verify", "refs/heads/nc/trunk"],
                          cwd=remote, capture_output=True, check=False).returncode != 0
    assert state.open_incidents() == []

    assert cli.main(["--home", str(cfg.home), "rollback", tid, "--confirm-commit",
                     state.one("SELECT merge_commit FROM task WHERE id=?", (tid,))[0]]) == 0
    assert not (repo / "marker.txt").exists()
    assert state.one("SELECT * FROM task WHERE id=?", (tid,))["status"] == "blocked"
    reverted = subprocess.run(["git", "log", "--oneline", "trunk"], cwd=remote,
                               capture_output=True, text=True, check=True).stdout
    assert "Revert" in reverted


def test_diverged_mirror_does_not_undo_acceptance_or_block_queue(setup, tmp_path):
    cfg, state, repo = setup
    remote = tmp_path / "mirror.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "mirror", str(remote)], cwd=repo, check=True)
    subprocess.run(["git", "push", "mirror", "main:main"], cwd=repo, check=True)
    state.add_project("neocortex", "Neocortex", str(repo), None, mirror="mirror")
    first = state.add_task("neocortex", "first", "create one.txt", [])
    second = state.add_task("neocortex", "second", "create two.txt", [])
    scheduler = sched(cfg, state, [
        commit_and_emit("one.txt", "one\n", {"outcome": "DONE", "summary": "done"}),
        emit({"outcome": "DONE", "verdict": "pass", "summary": "ok"}),
        commit_and_emit("two.txt", "two\n", {"outcome": "DONE", "summary": "done"}),
        emit({"outcome": "DONE", "verdict": "pass", "summary": "ok"}),
    ])
    scheduler.step()                                    # first worker

    writer = tmp_path / "remote-writer"
    subprocess.run(["git", "clone", "-q", str(remote), str(writer)], check=True)
    subprocess.run(["git", "config", "user.email", "nc@test"], cwd=writer, check=True)
    subprocess.run(["git", "config", "user.name", "nc"], cwd=writer, check=True)
    (writer / "remote.txt").write_text("remote advance\n")
    subprocess.run(["git", "add", "remote.txt"], cwd=writer, check=True)
    subprocess.run(["git", "commit", "-qm", "remote advance"], cwd=writer, check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=writer, check=True)
    remote_tip = subprocess.run(["git", "rev-parse", "main"], cwd=remote,
                                capture_output=True, text=True, check=True).stdout.strip()

    scheduler.step()                                    # accepted locally; mirror rejected
    task = state.one("SELECT * FROM task WHERE id=?", (first,))
    assert task["status"] == "done" and task["merge_commit"]
    assert not (cfg.work_dir / first).exists()
    assert subprocess.run(["git", "rev-parse", "main"], cwd=remote,
                          capture_output=True, text=True, check=True).stdout.strip() == remote_tip
    incident = state.one("SELECT detail FROM incident WHERE kind='mirror_push'")
    assert first in incident["detail"]
    assert "remote=mirror base=refs/heads/main" in incident["detail"]
    assert "local_tip=" in incident["detail"] and f"remote_tip={remote_tip}" in incident["detail"]
    assert "reason=" in incident["detail"]

    scheduler.step()
    scheduler.step()
    assert state.one("SELECT status FROM task WHERE id=?", (second,))["status"] == "done"


def test_mirror_exceptions_do_not_undo_acceptance_or_completed_rollback(setup, monkeypatch):
    cfg, state, repo = setup
    state.db.execute("UPDATE project SET mirror='unreachable' WHERE id='neocortex'")
    tid = state.add_task("neocortex", "add marker", "create marker.txt", [])
    scheduler = sched(cfg, state, [
        commit_and_emit("marker.txt", "hi\n", {"outcome": "DONE", "summary": "done"}),
        emit({"outcome": "DONE", "verdict": "pass", "summary": "ok"}),
    ])

    def timed_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("git push", 300)

    monkeypatch.setattr("nc.scheduler.arbiter.mirror", timed_out)
    scheduler.step()
    scheduler.step()
    task = state.one("SELECT * FROM task WHERE id=?", (tid,))
    assert task["status"] == "done" and task["merge_commit"]
    assert state.one("SELECT detail FROM incident WHERE kind='mirror_push'")

    monkeypatch.setattr("nc.operations.arbiter.mirror", lambda *_args: (_ for _ in ()).throw(OSError("offline")))
    result = operations.rollback_task(state, tid, task["merge_commit"])
    assert result["mirror_error"] == "offline"
    assert state.one("SELECT status FROM task WHERE id=?", (tid,))["status"] == "blocked"
    assert not (repo / "marker.txt").exists()
    incidents = state.q("SELECT detail FROM incident WHERE kind='mirror_push'")
    assert any("offline" in row["detail"] for row in incidents)


def test_a_dependent_task_waits_until_its_dependency_is_accepted(setup):
    cfg, state, _repo = setup
    first = state.add_task("neocortex", "base", "create marker.txt", [], priority=10)
    second = state.add_task("neocortex", "follow-up", "extend marker.txt", [], priority=20,
                            depends_on=[first])

    scheduler = sched(cfg, state, [
        commit_and_emit("marker.txt", "hi\n", {"outcome": "DONE", "summary": "done"}),
        emit({"outcome": "DONE", "verdict": "pass", "summary": "ok"}),
        commit_and_emit("marker.txt", "hi again\n", {"outcome": "DONE", "summary": "done"}),
    ])

    scheduler.step()                                     # worker on the dependency
    assert state.one("SELECT * FROM task WHERE id=?", (second,))["status"] == "queued"
    assert state.one("SELECT * FROM agent WHERE task_id=?", (second,)) is None

    scheduler.step()                                     # critic accepts the dependency
    assert state.unmet_dependencies(second) == []
    scheduler.step()
    assert state.one("SELECT * FROM task WHERE id=?", (second,))["status"] == "in_review"


def test_a_dependent_task_stays_queued_while_its_dependency_is_blocked(setup):
    cfg, state, _repo = setup
    first = state.add_task("neocortex", "base", "create marker.txt", [], priority=10)
    second = state.add_task("neocortex", "follow-up", "extend it", [], priority=20,
                            depends_on=[first])
    state.set_task(first, status="blocked")

    scheduler = sched(cfg, state, [])
    assert scheduler.next_ready_task() is None
    assert state.unmet_dependencies(second) == [first]


def test_requeue_restarts_a_blocked_task_from_the_base_branch(setup):
    cfg, state, _repo = setup
    tid = state.add_task("neocortex", "add marker", "create marker.txt", [])
    scheduler = sched(cfg, state, [
        commit_and_emit("marker.txt", "stale\n", {"outcome": "DONE", "summary": "done"}),
        emit({"outcome": "DONE", "verdict": "rework", "summary": "no", "findings": ["redo"]}),
        commit_and_emit("marker.txt", "fresh\n", {"outcome": "DONE", "summary": "done"}),
    ])
    scheduler.step()
    scheduler.step()
    state.set_task(tid, status="blocked")

    from nc import operations
    token = operations.discard_preview(cfg, state, tid)["token"]
    assert cli.main(["--home", str(cfg.home), "requeue", tid, "--fresh",
                     "--confirm-discard", token]) == 0
    task = state.one("SELECT * FROM task WHERE id=?", (tid,))
    assert task["status"] == "queued" and task["attempts"] == 0
    assert all(agent["turns"] == 0 and agent["state"] == "blocked"
               for agent in state.q("SELECT * FROM agent WHERE task_id=?", (tid,)))
    assert not (cfg.work_dir / tid).exists()

    scheduler.step()
    worktree = cfg.work_dir / tid
    assert (worktree / "marker.txt").read_text() == "fresh\n"


def test_a_longer_attempt_budget_keeps_a_reworking_task_alive(setup):
    cfg, state, _repo = setup
    cfg.max_attempts = 5
    tid = state.add_task("neocortex", "add marker", "create marker.txt", [])
    scheduler = sched(cfg, state, [nothing, nothing, nothing])

    for _ in range(3):
        scheduler.step()

    task = state.one("SELECT * FROM task WHERE id=?", (tid,))
    assert (task["status"], task["attempts"]) == ("in_progress", 3)
    assert state.one("SELECT * FROM agent WHERE id=?", (f"worker-{tid}",))["state"] == "runnable"


def test_requeue_can_raise_the_turn_budget(setup):
    cfg, state, _repo = setup
    tid = state.add_task("neocortex", "add marker", "create marker.txt", [], budget_turns=2)
    state.set_task(tid, status="blocked")

    assert cli.main(["--home", str(cfg.home), "requeue", tid, "--budget", "9"]) == 0
    task = state.one("SELECT * FROM task WHERE id=?", (tid,))
    assert (task["status"], task["budget_turns"]) == ("queued", 9)


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

    # the breaker stays tripped: a restart must not walk into the same failure
    stop = cfg.home / "STOP"
    assert stop.exists()
    before = len(state.q("SELECT * FROM run"))
    scheduler.run()
    assert len(state.q("SELECT * FROM run")) == before

    stop.unlink()
    scheduler.consecutive_failures = 0
    scheduler.run()
    assert len(state.q("SELECT * FROM run")) > before


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


def test_ask_timeout_does_not_escalate_concurrently_cancelled_task(setup, monkeypatch):
    cfg, state, _repo = setup
    tid = state.add_task("neocortex", "question", "obj", [])
    scheduler = sched(cfg, state, [])
    agent = scheduler.spawn_for_queued_task()
    state.set_task(tid, status="blocked")
    state.set_agent(agent, state="blocked")
    state.x("UPDATE agent SET updated_at=0 WHERE id=?", (agent,))
    state.send(protocol.QUESTION, agent, "owner", {"question": "filename?"}, tid)
    original_q = state.q
    other = State(cfg.db_path)
    cancelled = []

    def cancel_after_selection(sql, params=()):
        rows = original_q(sql, params)
        if sql.startswith("SELECT a.* FROM agent a WHERE a.state='blocked'"):
            assert rows
            cancelled.append(other.cancel_task(tid, "concurrent cancellation"))
        return rows

    monkeypatch.setattr(state, "q", cancel_after_selection)
    try:
        scheduler._escalate_unanswered_questions()
        assert cancelled == [True]
        assert not state.q("SELECT * FROM incident WHERE kind='ask_timeout'")
        assert state.one("SELECT status FROM task WHERE id=?", (tid,))["status"] == "cancelled"
    finally:
        other.db.close()


@pytest.mark.parametrize("agent_count", [1, 2])
def test_ask_timeout_is_reported_once_per_agent_across_run_cycles(setup, monkeypatch, agent_count):
    cfg, state, _repo = setup
    cfg.ask_timeout_s = 0
    monkeypatch.setattr("nc.scheduler.time.sleep", lambda _: None)
    monkeypatch.setattr(Scheduler, "preflight", lambda self: (True, "test"))
    tids = [state.add_task("neocortex", "question", "obj", []) for _ in range(agent_count)]
    scheduler = sched(cfg, state, [
        emit({"outcome": "ASK", "to": "owner", "question": "which filename?"})
        for _ in range(agent_count)
    ])
    scheduler.run()
    for _ in range(3):
        # Reopening the database also checks persistence across process restarts.
        reopened = State(cfg.db_path)
        try:
            sched(cfg, reopened, []).run()
        finally:
            reopened.db.close()
    incidents = state.open_incidents()
    assert len(incidents) == agent_count
    for tid in tids:
        assert sum(f"worker-{tid}:" in i["detail"] for i in incidents) == 1
    assert all(i["kind"] == "ask_timeout" for i in incidents)

    state.x("UPDATE incident SET resolved=1")
    scheduler.run()
    assert len(state.q("SELECT * FROM incident")) == agent_count


@pytest.mark.parametrize("age,has_question,answered,expected", [
    (59, True, False, 0),
    (61, True, False, 1),
    (61, False, False, 0),
    (61, True, True, 0),
])
def test_ask_timeout_only_escalates_overdue_unanswered_questions(
    setup, monkeypatch, age, has_question, answered, expected,
):
    cfg, state, _repo = setup
    cfg.ask_timeout_s = 60
    monkeypatch.setattr("nc.scheduler.time.time", lambda: 1000)
    tid = state.add_task("neocortex", "question", "obj", [])
    scheduler = sched(cfg, state, [])
    agent_id = scheduler.spawn_for_queued_task()
    state.set_agent(agent_id, state="blocked")
    state.set_task(tid, status="blocked")
    state.x("UPDATE agent SET updated_at=? WHERE id=?", (1000 - age, agent_id))
    if has_question:
        question = state.send(protocol.QUESTION, agent_id, "owner", {"question": "filename?"})
        if answered:
            state.send(protocol.ANSWER, "owner", agent_id, {"answer": "marker.txt"},
                       in_reply_to=question)
    scheduler.preflight = lambda: (True, "test")
    scheduler.run()
    assert len(state.open_incidents()) == expected


@pytest.mark.parametrize("overrides,expected", [
    ({"worker": "codex", "critic": "claude"}, ["codex", "claude"]),
    ({"worker": "claude", "critic": "codex"}, ["claude", "codex"]),
    ({"critic": "codex"}, ["claude", "codex"]),
    ({}, ["claude", "claude"]),
])
def test_scheduler_selects_adapter_for_each_role(setup, monkeypatch, overrides, expected):
    cfg, state, _repo = setup
    cfg.adapter = "claude"
    cfg.adapters = overrides
    state.add_task("neocortex", "add marker", "create marker.txt", [])
    scripted = ScriptedAdapter([
        commit_and_emit("marker.txt", "hi\n", {"outcome": "DONE", "summary": "done"}),
        emit({"outcome": "YIELD", "summary": "reviewing"}),
    ])
    requested = []

    def get_adapter(name):
        requested.append(name)
        return scripted

    monkeypatch.setattr("nc.scheduler.get_adapter", get_adapter)
    scheduler = Scheduler(cfg, state)
    assert scheduler.step() == protocol.DONE
    assert scheduler.step() == protocol.YIELD
    assert requested == expected
    assert [model for model, _ in scripted.calls] == [
        cfg.model_for("worker"), cfg.model_for("critic"),
    ]


def test_preflight_uses_worker_adapter(setup, monkeypatch):
    from unittest.mock import Mock

    cfg, state, _repo = setup
    cfg.adapters = {"worker": "claude"}
    adapter = Mock()
    adapter.available.return_value = False
    adapter.name = "claude"
    lookup = Mock(return_value=adapter)
    monkeypatch.setattr("nc.scheduler.get_adapter", lookup)
    assert Scheduler(cfg, state).preflight() == (False, "adapter claude is not installed")
    lookup.assert_called_once_with("claude")


def test_feedback_and_plan_are_picked_up_on_next_scheduler_tick(setup, monkeypatch, capsys):
    cfg, state, _repo = setup

    def unexpected(*args, **kwargs):
        pytest.fail("planner must not create a worktree")

    adapter = ScriptedAdapter([emit({"outcome": "ASK", "question": "Which scope?"})])
    monkeypatch.setattr("nc.scheduler.get_adapter", lambda _: adapter)
    monkeypatch.setattr("nc.scheduler.arbiter.ensure_worktree", unexpected)
    monkeypatch.setattr("nc.turn.run_turn", unexpected)
    assert cli.main([
        "--home", str(cfg.home), "feedback", "Keep it simple", "--project", "neocortex",
    ]) == 0
    assert cli.main([
        "--home", str(cfg.home), "plan", "neocortex", "--note", "Review tests",
    ]) == 0
    agents = state.q("SELECT * FROM agent")
    assert len(agents) == 1
    planner = agents[0]
    assert (planner["role"], planner["state"], planner["task_id"]) == (
        "planner", "runnable", None,
    )
    assert state.q("SELECT * FROM run") == []
    scheduler = Scheduler(cfg, state)
    assert scheduler.pick()["id"] == planner["id"]
    # Exercise the timer's run loop, bypassing only its external model probe.
    monkeypatch.setattr(scheduler, "preflight", lambda: (True, "test"))
    monkeypatch.setattr("nc.scheduler.time.sleep", lambda _: None)
    scheduler.run(max_turns=3)
    assert len(state.q("SELECT * FROM run")) == 1
    assert scheduler.step() == "idle"
    run = state.one("SELECT * FROM run")
    assert (run["agent_id"], run["task_id"], run["outcome"]) == (
        planner["id"], None, protocol.ASK,
    )
    assert run["ended_at"] is not None
    assert state.one("SELECT * FROM agent")["turns"] == 1
    assert state.one("SELECT * FROM agent")["state"] == "blocked"
    assert state.q("SELECT * FROM task") == []
    assert state.inbox(planner["id"]) == []
    assert "Keep it simple" in adapter.briefs[0]
    assert "Review tests" in adapter.briefs[0]


def test_planner_does_not_starve_queued_work(setup):
    cfg, state, _repo = setup
    state.planner_feedback("neocortex", "Review tests", cfg.model_for("planner"))
    tid = state.add_task("neocortex", "work", "obj", [])
    scheduler = sched(cfg, state, [emit({"outcome": "YIELD"})])
    assert scheduler.step() == protocol.YIELD
    assert state.one("SELECT * FROM run")["agent_id"] == f"worker-{tid}"


def test_planner_preserves_new_owner_wake(setup, monkeypatch):
    cfg, state, _repo = setup
    agent_id, _ = state.planner_feedback("neocortex", "First", cfg.model_for("planner"))
    scheduler = sched(cfg, state, [emit({"outcome": "ASK", "question": "Scope?"})] * 3)
    end_run = state.end_run

    def feedback_during_turn(*args, **kwargs):
        end_run(*args, **kwargs)
        state.planner_feedback("neocortex", "New request", cfg.model_for("planner"))

    monkeypatch.setattr(state, "end_run", feedback_during_turn)
    assert scheduler.step() == protocol.ASK
    assert state.one("SELECT * FROM agent")["state"] == "runnable"
    monkeypatch.setattr(state, "end_run", end_run)
    assert scheduler.step() == protocol.ASK
    assert scheduler.step() == "idle"
    assert len(state.inbox(agent_id)) == 0
    assert cli.main(["--home", str(cfg.home), "plan", "neocortex"]) == 0
    assert scheduler.step() == protocol.ASK
    assert scheduler.step() == "idle"
    assert len(state.q("SELECT * FROM agent")) == 1
    assert len(state.inbox(agent_id)) == 0


def planner_spec(**extra):
    return {"project": "neocortex", "title": "Improve behavior", "objective": "Bounded change",
            "acceptance": ["$ true"], "boundaries": ["Existing behavior must remain compatible"],
            **extra}


@pytest.mark.parametrize("planner_override", [None, "gpt-5.6-luna"])
def test_planner_proposal_requires_approval(setup, monkeypatch, planner_override):
    cfg, state, repo = setup
    cfg.models = {"worker": "gpt-5.6-luna", "critic": "gpt-6-astra"}
    if planner_override is not None:
        cfg.models["planner"] = planner_override
    state.planner_feedback("neocortex", "Plan a change", "old-model")
    adapter = ScriptedAdapter([emit({"outcome": "DONE", "summary": "Two ordered changes",
                                    "proposal": [planner_spec(id="first"),
                                                 planner_spec(depends_on=["first"])]})])
    scheduler = Scheduler(cfg, state)
    monkeypatch.setattr(scheduler, "_adapter_for", lambda role: adapter)
    monkeypatch.setattr("nc.scheduler.arbiter.ensure_worktree",
                        lambda *args: pytest.fail("planner received a worktree"))
    before = subprocess.check_output(["git", "status", "--porcelain"], cwd=repo)
    assert scheduler.step() == protocol.DONE
    assert state.q("SELECT * FROM task") == []
    proposals = state.q("SELECT * FROM proposal")
    assert len(proposals) == 1 and proposals[0]["status"] == "pending"
    assert adapter.calls[0][0] == (planner_override or "gpt-6-astra")
    assert state.one("SELECT model FROM run")["model"] == adapter.calls[0][0]
    assert adapter.calls[0][1].parent == cfg.runs_dir
    assert not cfg.work_dir.exists()
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=repo) == before
    assert cli.main(["--home", str(cfg.home), "approve", str(proposals[0]["id"])]) == 0
    tasks = state.q("SELECT * FROM task ORDER BY id")
    assert len(tasks) == 2
    assert json.loads(tasks[1]["depends_on"]) == [tasks[0]["id"]]


def test_planner_rejects_oversized_batch(setup):
    cfg, state, _ = setup
    aid, _ = state.planner_feedback("neocortex", "Plan", cfg.model_for("planner"))
    scheduler = sched(cfg, state, [emit({"outcome": "DONE",
                                       "proposal": [planner_spec()] * 6})])
    assert scheduler.step() == protocol.FAIL
    assert "one and five" in state.one("SELECT * FROM run")["detail"]
    assert scheduler.consecutive_failures == 1
    assert state.q("SELECT * FROM proposal") == []
    assert state.q("SELECT * FROM task") == []
    assert len(state.inbox(aid)) == 1


def test_planner_brief_includes_state(setup):
    from nc.turn import build_planner_brief

    cfg, state, _ = setup
    feedback = "Full feedback " + "x" * 1000 + " END"
    aid, mid = state.planner_feedback("neocortex", feedback, cfg.model_for("planner"))
    done = state.add_task("neocortex", "Accepted change", "objective", [])
    state.set_task(done, status="done")
    blocked = state.add_task("neocortex", "Blocked change", "objective", [])
    state.set_task(blocked, status="blocked")
    state.send(protocol.INCIDENT, "scheduler", "owner", {"reason": "turn budget exhausted"},
               task_id=blocked)
    state.add_task("neocortex", "Queued change", "objective", [], depends_on=[blocked])
    state.send(protocol.QUESTION, "worker", "owner", {"question": "Missing input?"},
               task_id=blocked)
    brief, ids = build_planner_brief(state, state.one("SELECT * FROM agent WHERE id=?", (aid,)),
                                     cfg.runs_dir / "outcome.json")
    for value in (feedback, "neocortex", "README.md", "Accepted change", "Blocked change",
                  "Queued change", "turn budget exhausted", "Missing input?",
                  "Waiting for accepted dependencies"):
        assert value in brief
    assert ids == [mid]


@pytest.mark.parametrize('verdict', ['pass', 'reject', 'rework'])
@pytest.mark.parametrize('override', [None, 'custom-critic'])
def test_plan_critic_advisory_review(setup, monkeypatch, capsys, verdict, override):
    cfg, state, repo = setup
    cfg.models['planner'] = 'planner-model'
    if override:
        cfg.models['plan_critic'] = override
    state.planner_feedback('neocortex', 'private planner feedback', 'planner-model')
    proposed = [planner_spec(id='first'), planner_spec(depends_on=['first'])]
    adapter = ScriptedAdapter([
        emit({'outcome': 'DONE', 'summary': 'SECRET rationale', 'memo': 'SECRET memo',
              'proposal': proposed}),
        emit({'outcome': 'DONE', 'verdict': verdict, 'proposal': [],
              'findings': ['first assumes a missing table'],
              'recommendation': 'Ask the owner about the table'}),
    ])
    adapter.run_planner = adapter.run
    scheduler = Scheduler(cfg, state)
    monkeypatch.setattr(scheduler, '_adapter_for', lambda role: adapter)
    monkeypatch.setattr('nc.scheduler.arbiter.ensure_worktree',
                        lambda *args: pytest.fail('plan critic took a worktree'))
    assert scheduler.step() == protocol.DONE
    assert len(adapter.calls) == 1
    assert scheduler.step() == protocol.DONE
    assert adapter.calls[1] == (override or 'planner-model', adapter.calls[1][1])
    assert adapter.calls[1][1].parent == cfg.runs_dir
    brief = adapter.briefs[1]
    assert 'SECRET' not in brief and 'private planner feedback' not in brief
    assert json.dumps(proposed) in brief
    assert str(repo) in brief and 'README.md' in brief
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo).decode().strip() in brief
    assert not cfg.work_dir.exists()
    proposal = state.one('SELECT * FROM proposal')
    assert proposal['status'] == 'pending' and proposal['decided_at'] is None
    assert json.loads(proposal['spec']) == proposed
    assert not state.q('SELECT * FROM task')
    assert scheduler.step() == 'idle'
    # Claim survives a scheduler restart.
    assert Scheduler(cfg, state).step() == 'idle'
    args = ['--home', str(cfg.home)]
    assert cli.main([*args, 'proposal', str(proposal['id'])]) == 0
    review = json.loads(capsys.readouterr().out)['plan_review']
    assert review == {'status': 'done', 'findings': ['first assumes a missing table'],
                      'recommendation': 'Ask the owner about the table'}
    assert cli.main([*args, 'approve', str(proposal['id'])]) == 0
    assert len(state.q('SELECT * FROM task')) == 2


@pytest.mark.parametrize('role', ['worker', 'critic', 'queued'])
def test_plan_critic_yields_to_task_work(setup, monkeypatch, role):
    cfg, state, _repo = setup
    state.add_proposal('neocortex', 'planner', 'rationale', [planner_spec()])
    tid = state.add_task('neocortex', 'Task', 'Objective', ['$ true'])
    if role != 'queued':
        state.set_task(tid, status='in_progress')
        state.add_agent('active', role, 'neocortex', tid, cfg.model_for(role))
    scheduler = sched(cfg, state, [emit({'outcome': 'YIELD'})])
    monkeypatch.setattr('nc.turn.run_plan_critic_turn',
                        lambda *args: pytest.fail('plan critic delayed task work'))
    assert scheduler.step() == protocol.YIELD
    assert not state.q('SELECT * FROM plan_review')


def test_plan_critic_failed_attempt_and_changed_proposal(setup):
    cfg, state, _repo = setup
    pid = state.add_proposal('neocortex', 'planner', 'rationale', [planner_spec()])
    scheduler = sched(cfg, state, [emit({'outcome': 'ASK', 'question': 'URL?'}),
                                  emit({'outcome': 'DONE', 'findings': [],
                                        'recommendation': 'No defects found'})])
    scheduler.adapter.run_planner = scheduler.adapter.run
    assert scheduler.step() == protocol.FAIL
    assert scheduler.step() == 'idle'
    assert state.one('SELECT status FROM proposal')['status'] == 'pending'
    assert not state.q('SELECT * FROM task')
    assert scheduler.consecutive_failures == 0
    state.x('UPDATE proposal SET spec=? WHERE id=?',
            (json.dumps([planner_spec(title='Changed plan')]), pid))
    assert scheduler.step() == protocol.DONE
    assert scheduler.step() == 'idle'
    assert len(state.q('SELECT * FROM plan_review')) == 2


def test_plan_critic_requires_restricted_adapter(setup):
    cfg, state, _repo = setup
    state.add_proposal('neocortex', 'planner', 'rationale', [planner_spec()])
    scheduler = sched(cfg, state, [])
    assert scheduler.step() == protocol.NO_OUTCOME
    assert scheduler.adapter.calls == []
    assert scheduler.step() == 'idle'


@pytest.mark.parametrize('role', ['worker', 'critic'])
def test_runnable_task_agents_always_precede_planner(setup, role):
    cfg, state, _ = setup
    aid, _ = state.planner_feedback('neocortex', 'Plan', cfg.model_for('planner'))
    tid = state.add_task('neocortex', 'Work', 'Objective', [])
    state.set_task(tid, status='in_progress')
    state.add_agent('active', role, 'neocortex', tid, cfg.model_for(role))
    scheduler = sched(cfg, state, [])
    assert scheduler.pick()['id'] == 'active'
    assert state.one('SELECT state FROM agent WHERE id=?', (aid,))['state'] == 'waiting'
    assert len(state.inbox(aid)) == 1


@pytest.mark.parametrize('decision', ['approve', 'reject'])
def test_pending_proposal_defers_planner_until_owner_decision(setup, decision):
    cfg, state, _ = setup
    aid, _ = state.planner_feedback('neocortex', 'Plan', cfg.model_for('planner'))
    pid = state.add_proposal('neocortex', 'planner', 'rationale', [planner_spec()])
    scheduler = sched(cfg, state, [emit({'outcome': 'ASK', 'question': 'Scope?'})])
    assert scheduler.pick() is None
    project = state.one('SELECT * FROM project')
    assert project['planner_last_ran_at'] is None
    assert 'pending proposals' in project['planner_skip_reason']
    assert state.one('SELECT state FROM agent WHERE id=?', (aid,))['state'] == 'waiting'
    assert len(state.inbox(aid)) == 1
    if decision == 'approve':
        for tid in state.approve_proposal(pid):
            # Approved work must finish before the planner gets a turn.
            state.set_task(tid, status='done')
    else:
        state.reject_proposal(pid, 'Not needed')
    assert scheduler.step() == protocol.ASK
    project = state.one('SELECT * FROM project')
    assert project['planner_last_ran_at'] is not None
    assert project['planner_skip_reason'] is None
    assert scheduler.step() == 'idle'


def test_queue_limit_waits_without_spinning_and_resumes_at_limit(setup, monkeypatch):
    cfg, state, _ = setup
    cfg.planner_max_queued = 1
    aid, _ = state.planner_feedback('neocortex', 'Plan', cfg.model_for('planner'))
    blocked = state.add_task('neocortex', 'Blocked', 'Objective', [])
    state.set_task(blocked, status='blocked')
    queued = [state.add_task('neocortex', 'Queued', 'Objective', [], depends_on=[blocked])
              for _ in range(2)]
    scheduler = sched(cfg, state, [emit({'outcome': 'ASK', 'question': 'Scope?'})])
    monkeypatch.setattr(scheduler, 'preflight', lambda: (True, 'test'))
    monkeypatch.setattr('nc.scheduler.time.sleep',
                        lambda _: pytest.fail('skipped planner kept scheduler running'))
    scheduler.run()
    assert not scheduler.adapter.calls
    assert state.one('SELECT state FROM agent WHERE id=?', (aid,))['state'] == 'waiting'
    assert 'queued tasks 2' in state.one('SELECT * FROM project')['planner_skip_reason']
    # Waiting and the trigger survive restarting the scheduler/database.
    reopened = State(cfg.db_path)
    try:
        assert Scheduler(cfg, reopened).step() == 'idle'
    finally:
        reopened.db.close()
    assert len(state.inbox(aid)) == 1
    state.set_task(queued[0], status='done')
    assert scheduler.step() == protocol.ASK
    assert scheduler.step() == 'idle'


def test_proposal_revision_cycle(setup, capsys):
    cfg, state, _ = setup
    original_specs = [planner_spec(id='first', objective='Original full objective'),
                      planner_spec(depends_on=['first'])]
    original = state.add_proposal('neocortex', 'planner', 'OLD rationale', original_specs)
    scheduler = sched(cfg, state, [
        emit({'outcome': 'DONE', 'findings': ['Old finding'], 'recommendation': 'Revise'}),
        emit({'outcome': 'DONE', 'summary': 'PRIVATE revised rationale',
              'proposal': original_specs}),
        emit({'outcome': 'DONE', 'findings': ['New finding'], 'recommendation': 'Advisory'}),
        emit({'outcome': 'DONE', 'proposal': original_specs}),
        emit({'outcome': 'DONE', 'findings': [], 'recommendation': 'Ready'}),
    ])
    scheduler.adapter.run_planner = scheduler.adapter.run
    assert scheduler.step() == protocol.DONE
    argv = ['--home', str(cfg.home)]
    assert cli.main([*argv, 'feedback', '--proposal', str(original), 'Owner revision text']) == 0
    assert cli.main([*argv, 'approve', str(original), '--force']) == 1
    assert scheduler.step() == protocol.DONE
    revision = state.one('SELECT * FROM proposal_revision')
    replacement = revision['replacement_id']
    assert replacement != original
    assert state.one('SELECT status FROM proposal WHERE id=?', (original,))[0] == 'superseded'
    assert len(state.q("SELECT * FROM proposal WHERE status='pending'")) == 1
    assert not state.q('SELECT * FROM task')
    brief = scheduler.adapter.briefs[1]
    assert 'Original full objective' in brief and 'Owner revision text' in brief
    assert json.dumps(json.dumps(original_specs))[1:-1] in brief
    assert scheduler.step() == protocol.DONE
    assert 'PRIVATE' not in scheduler.adapter.briefs[2]
    assert len(state.q('SELECT * FROM plan_review')) == 2
    assert state.one('SELECT findings FROM proposal WHERE id=?', (replacement,))[0] == '[]'
    capsys.readouterr()
    assert cli.main([*argv, 'proposal', str(original)]) == 0
    detail = json.loads(capsys.readouterr().out)
    assert detail['spec'] == original_specs
    assert detail['plan_review']['findings'] == ['Old finding']
    assert detail['revisions'][0]['replacement_id'] == replacement
    assert detail['revisions'][0]['feedback']['text'] == 'Owner revision text'
    assert cli.main([*argv, 'proposal', str(replacement)]) == 0
    detail = json.loads(capsys.readouterr().out)
    assert detail['plan_review']['findings'] == ['New finding']
    assert detail['revisions'][0]['original_id'] == original
    assert cli.main([*argv, 'feedback', '--proposal', str(replacement), 'One more iteration']) == 0
    assert scheduler.step() == protocol.DONE
    assert scheduler.step() == protocol.DONE
    latest = state.one("SELECT * FROM proposal WHERE status='pending'")
    assert state.one('SELECT replacement_id FROM proposal_revision WHERE original_id=?',
                     (replacement,))[0] == latest['id']
    assert not state.q('SELECT * FROM task')
    ids = state.approve_proposal(latest['id'])
    assert json.loads(state.one('SELECT depends_on FROM task WHERE id=?', (ids[1],))[0]) == [
        ids[0],
    ]


@pytest.mark.parametrize('failure', ['ask', 'invalid', 'missing', 'exception'])
def test_revision_survives_retry(setup, failure):
    cfg, state, _ = setup
    original = state.add_proposal('neocortex', 'planner', '', [planner_spec(objective='Keep me')])
    aid, _ = state.planner_feedback(None, 'Retain revision feedback', 'model',
                                   proposal_id=original)

    def fail_session(cwd, outcome_path):
        raise RuntimeError('session crashed')

    first = {
        'ask': emit({'outcome': 'ASK', 'to': 'owner', 'question': 'Which behavior?'}),
        'invalid': emit({'outcome': 'DONE', 'proposal': []}),
        'missing': nothing,
        'exception': fail_session,
    }[failure]
    scheduler = sched(cfg, state, [
        first, emit({'outcome': 'DONE', 'proposal': [planner_spec()]}),
    ])
    scheduler.step()
    assert state.pending_revision(aid) is not None
    assert len(state.q('SELECT * FROM proposal')) == 1
    # Re-open the database to ensure the context is durable, including after ASK delivery.
    reopened = State(cfg.db_path)
    assert reopened.pending_revision(aid) is not None
    reopened.db.close()
    state.planner_feedback('neocortex', 'Retry now', 'model')
    assert scheduler.step() == protocol.DONE
    assert 'Keep me' in scheduler.adapter.briefs[1]
    assert 'Retain revision feedback' in scheduler.adapter.briefs[1]
    assert state.pending_revision(aid) is None
    assert len(state.q('SELECT * FROM proposal')) == 2


@pytest.mark.parametrize("role", ["worker", "critic"])
def test_session_exception_finalizes_worker_and_critic_without_consuming_feedback(setup, role):
    cfg, state, repo = setup
    task = state.add_task("neocortex", "exception", "objective", [])
    state.set_task(task, status="in_review" if role == "critic" else "in_progress")
    agent = state.add_agent(f"{role}-exception", role, "neocortex", task, "model")
    state.send("feedback", "owner", agent, {"text": "retain me"}, task)
    adapter = ScriptedAdapter([lambda _cwd, _outcome: (_ for _ in ()).throw(RuntimeError("adapter exploded"))])
    outcome = turn.run_turn(state, cfg, adapter, state.one("SELECT * FROM agent WHERE id=?", (agent,)),
                            repo, "main")
    run = state.one("SELECT * FROM run WHERE agent_id=?", (agent,))
    assert outcome.kind == protocol.NO_OUTCOME
    assert run["outcome"] == protocol.NO_OUTCOME and "adapter exploded" in run["detail"]
    assert run["timed_out"] is None and run["exit_code"] is None
    assert json.loads(state.inbox(agent)[0]["payload"]) == {"text": "retain me"}


def test_session_exception_finalizes_planner_and_plan_critic_with_context(setup):
    cfg, state, _repo = setup
    original = state.add_proposal("neocortex", "planner", "", [planner_spec()])
    planner, _message = state.planner_feedback(None, "retain revision feedback", "model",
                                                proposal_id=original)
    adapter = ScriptedAdapter([lambda _cwd, _outcome: (_ for _ in ()).throw(RuntimeError("planner exploded"))])
    agent = state.one("SELECT * FROM agent WHERE id=?", (planner,))
    outcome = turn.run_planner_turn(state, cfg, agent, adapter)
    run = state.one("SELECT * FROM run WHERE agent_id=? ORDER BY id DESC", (planner,))
    assert outcome.kind == protocol.NO_OUTCOME and run["outcome"] == protocol.NO_OUTCOME
    assert "planner exploded" in run["detail"]
    assert run["timed_out"] is None and run["exit_code"] is None
    assert state.pending_revision(planner) is not None

    proposal = state.add_proposal("neocortex", planner, "", [planner_spec()])
    adapter.run_planner = adapter.run
    adapter.script = [lambda _cwd, _outcome: (_ for _ in ()).throw(RuntimeError("critic exploded"))]
    outcome = turn.run_plan_critic_turn(state, cfg, state.one("SELECT * FROM proposal WHERE id=?", (proposal,)), adapter)
    run = state.one("SELECT * FROM run WHERE role='plan_critic' ORDER BY id DESC")
    assert outcome.kind == protocol.NO_OUTCOME and run["outcome"] == protocol.NO_OUTCOME
    assert "critic exploded" in run["detail"]
    assert run["timed_out"] is None and run["exit_code"] is None


@pytest.mark.parametrize("role", ["worker", "critic"])
@pytest.mark.parametrize("failure", ["nonzero", "timeout", "terminal"])
def test_host_failure_with_valid_outcome_keeps_task_inbox_and_memo(setup, role, failure):
    """Synthetic terminal evidence must beat a valid agent-authored DONE."""
    cfg, state, repo = setup
    task = state.add_task("neocortex", "host evidence", "objective", [])
    state.set_task(task, status="in_review" if role == "critic" else "in_progress")
    agent_id = state.add_agent(f"{role}-host", role, "neocortex", task, "model")
    state.set_agent(agent_id, memo="keep")
    state.send("feedback", "owner", agent_id, {"text": "keep inbox"}, task)
    adapter = ScriptedAdapter([emit({"outcome": "DONE", "verdict": "pass", "memo": "lose"})])
    original = adapter.run

    def failed_session(*args):
        result = original(*args)
        if failure == "nonzero":
            result.exit_code = 1
        elif failure == "timeout":
            result.timed_out = True
        else:
            result.terminal_category = "overloaded"
        return result

    adapter.run = failed_session
    agent = state.one("SELECT * FROM agent WHERE id=?", (agent_id,))
    outcome = turn.run_turn(state, cfg, adapter, agent, repo, "main")
    run = state.one("SELECT * FROM run WHERE agent_id=?", (agent_id,))
    assert outcome.kind == protocol.FAIL
    assert run["outcome"] == protocol.DONE and run["host_assessment"] == "FAILED"
    assert state.one("SELECT memo FROM agent WHERE id=?", (agent_id,))[0] == "keep"
    assert state.inbox(agent_id)
    assert state.one("SELECT turns FROM agent WHERE id=?", (agent_id,))[0] == (
        0 if failure == "terminal" else 1
    )


@pytest.mark.parametrize("role", ["worker", "critic"])
def test_outcome_read_oserror_is_local_host_failure_for_task_roles(setup, monkeypatch, role):
    """A filesystem failure after a clean CLI exit cannot consume task context."""
    cfg, state, repo = setup
    task = state.add_task("neocortex", "unreadable outcome", "objective", [])
    state.set_task(task, status="in_review" if role == "critic" else "in_progress")
    agent_id = state.add_agent(f"{role}-unreadable", role, "neocortex", task, "model")
    state.set_agent(agent_id, memo="retain memo")
    state.send("feedback", "owner", agent_id, {"text": "retain inbox"}, task)
    adapter = ScriptedAdapter([emit({"outcome": "DONE", "memo": "discard"})])

    def unreadable(_path):
        raise OSError("simulated outcome filesystem error")

    monkeypatch.setattr(protocol, "read_outcome", unreadable)
    outcome = turn.run_turn(state, cfg, adapter,
                            state.one("SELECT * FROM agent WHERE id=?", (agent_id,)),
                            repo, "main")
    run = state.one("SELECT * FROM run WHERE agent_id=?", (agent_id,))
    assert outcome.kind == protocol.NO_OUTCOME
    assert run["outcome"] == protocol.NO_OUTCOME
    assert (run["host_assessment"], run["terminal_category"], run["exit_code"], run["timed_out"]) == (
        "FAILED", "local_error", 0, 0,
    )
    assert state.one("SELECT memo FROM agent WHERE id=?", (agent_id,))[0] == "retain memo"
    assert state.inbox(agent_id)


def test_outcome_read_oserror_is_local_host_failure_for_planner(setup, monkeypatch):
    cfg, state, _repo = setup
    original = state.add_proposal("neocortex", "planner", "", [planner_spec()])
    planner_id, _ = state.planner_feedback(
        None, "retain revision feedback", "model", proposal_id=original,
    )
    adapter = ScriptedAdapter([emit({"outcome": "DONE", "proposal": [planner_spec()]})])

    monkeypatch.setattr(protocol, "read_outcome",
                        lambda _path: (_ for _ in ()).throw(OSError("outcome unreadable")))
    outcome = turn.run_planner_turn(
        state, cfg, state.one("SELECT * FROM agent WHERE id=?", (planner_id,)), adapter,
    )
    run = state.one("SELECT * FROM run WHERE agent_id=? ORDER BY id DESC", (planner_id,))
    assert outcome.kind == protocol.NO_OUTCOME
    assert run["outcome"] == protocol.NO_OUTCOME
    assert (run["host_assessment"], run["terminal_category"], run["exit_code"], run["timed_out"]) == (
        "FAILED", "local_error", 0, 0,
    )
    assert state.pending_revision(planner_id) is not None
    assert len(state.q("SELECT * FROM proposal")) == 1


def test_outcome_read_oserror_is_local_host_failure_for_plan_critic(setup, monkeypatch):
    cfg, state, _repo = setup
    proposal = state.add_proposal("neocortex", "planner", "", [planner_spec()])
    adapter = ScriptedAdapter([emit({"outcome": "DONE", "recommendation": "yes", "findings": []})])
    adapter.run_planner = adapter.run
    monkeypatch.setattr(protocol, "read_outcome",
                        lambda _path: (_ for _ in ()).throw(OSError("outcome unreadable")))
    outcome = turn.run_plan_critic_turn(
        state, cfg, state.one("SELECT * FROM proposal WHERE id=?", (proposal,)), adapter,
    )
    run = state.one("SELECT * FROM run WHERE role='plan_critic' ORDER BY id DESC")
    review = state.one("SELECT * FROM plan_review WHERE proposal_id=?", (proposal,))
    assert outcome.kind == protocol.NO_OUTCOME and review["status"] == "failed"
    assert run["outcome"] == protocol.NO_OUTCOME
    assert (run["host_assessment"], run["terminal_category"], run["exit_code"], run["timed_out"]) == (
        "FAILED", "local_error", 0, 0,
    )


@pytest.mark.parametrize("role", ["worker", "critic", "planner", "plan_critic"])
def test_terminal_provider_failure_without_outcome_preserves_provider_evidence(setup, role):
    """Provider evidence wins when an interrupted child writes no outcome file."""
    cfg, state, repo = setup
    adapter = ScriptedAdapter([nothing])
    original = adapter.run

    def interrupted(*args):
        result = original(*args)
        result.terminal_category = "throttled"
        return result

    adapter.run = interrupted
    if role in {"worker", "critic"}:
        task = state.add_task("neocortex", f"{role} outage", "objective", [])
        state.set_task(task, status="in_review" if role == "critic" else "in_progress")
        agent_id = state.add_agent(f"{role}-outage", role, "neocortex", task, "model")
        state.set_agent(agent_id, memo="retain")
        state.send("feedback", "owner", agent_id, {"text": "retain"}, task)
        outcome = turn.run_turn(state, cfg, adapter,
                                state.one("SELECT * FROM agent WHERE id=?", (agent_id,)),
                                repo, "main")
        agent = state.one("SELECT turns, memo FROM agent WHERE id=?", (agent_id,))
        assert agent["turns"] == 0 and agent["memo"] == "retain" and state.inbox(agent_id)
    elif role == "planner":
        original_proposal = state.add_proposal("neocortex", "planner", "", [planner_spec()])
        planner_id, _ = state.planner_feedback(None, "retain", "model", proposal_id=original_proposal)
        outcome = turn.run_planner_turn(
            state, cfg, state.one("SELECT * FROM agent WHERE id=?", (planner_id,)), adapter)
        assert state.pending_revision(planner_id) is not None
    else:
        proposal_id = state.add_proposal("neocortex", "planner", "", [planner_spec()])
        adapter.run_planner = adapter.run
        outcome = turn.run_plan_critic_turn(
            state, cfg, state.one("SELECT * FROM proposal WHERE id=?", (proposal_id,)), adapter)
        assert state.one("SELECT status FROM plan_review WHERE proposal_id=?", (proposal_id,))[0] == "retryable"

    run = state.one("SELECT * FROM run ORDER BY id DESC")
    assert outcome.kind == protocol.FAIL and outcome.deferred
    assert run["outcome"] == protocol.NO_OUTCOME
    assert (run["host_assessment"], run["terminal_category"], run["exit_code"], run["timed_out"]) == (
        "FAILED", "throttled", 0, 0,
    )


@pytest.mark.parametrize("failure", ["nonzero", "timeout", "terminal"])
def test_plan_critic_host_failure_with_done_cannot_complete_review(setup, failure):
    cfg, state, _repo = setup
    proposal = state.add_proposal("neocortex", "planner", "", [planner_spec()])
    adapter = ScriptedAdapter([emit({"outcome": "DONE", "recommendation": "yes", "findings": []})])
    adapter.run_planner = adapter.run
    original = adapter.run_planner

    def failed_session(*args):
        result = original(*args)
        if failure == "nonzero":
            result.exit_code = 1
        elif failure == "timeout":
            result.timed_out = True
        else:
            result.terminal_category = "transient"
        return result

    adapter.run_planner = failed_session
    outcome = turn.run_plan_critic_turn(
        state, cfg, state.one("SELECT * FROM proposal WHERE id=?", (proposal,)), adapter,
    )
    review = state.one("SELECT * FROM plan_review WHERE proposal_id=?", (proposal,))
    assert outcome.kind == protocol.FAIL and review["status"] == (
        "retryable" if failure == "terminal" else "failed"
    )
    agent = state.one("SELECT state, turns FROM agent WHERE role='plan_critic' ORDER BY id DESC")
    assert (agent["state"], agent["turns"]) == (
        "done", 0 if failure == "terminal" else 1,
    )


def test_plan_critic_provider_retry_reuses_logical_review_and_records_attempts(setup):
    """A timer's next invocation retries advice, not a new logical review."""
    cfg, state, _repo = setup
    proposal = state.add_proposal("neocortex", "planner", "", [planner_spec()])
    adapter = ScriptedAdapter([
        emit({"outcome": "DONE", "recommendation": "discard", "findings": []}),
        emit({"outcome": "DONE", "recommendation": "keep", "findings": ["one"]}),
    ])
    adapter.run_planner = adapter.run
    original = adapter.run_planner

    def temporarily_unavailable(*args):
        result = original(*args)
        result.terminal_category = "transient"
        return result

    adapter.run_planner = temporarily_unavailable
    scheduler = sched(cfg, state, [])
    scheduler._adapter_for = lambda _role: adapter
    assert scheduler.step() == "deferred"
    review = state.one("SELECT * FROM plan_review WHERE proposal_id=?", (proposal,))
    assert review["status"] == "retryable"
    adapter.run_planner = original
    # A timer starts a fresh scheduler process; the retryable claim and its
    # first attempt live in SQLite rather than in Scheduler memory.
    scheduler = sched(cfg, state, [])
    scheduler._adapter_for = lambda _role: adapter
    assert scheduler.step() == protocol.DONE
    review = state.one("SELECT * FROM plan_review WHERE proposal_id=?", (proposal,))
    attempts = state.q("SELECT status FROM plan_review_attempt WHERE review_id=? ORDER BY id",
                       (review["id"],))
    assert review["status"] == "done" and review["recommendation"] == "keep"
    assert [row["status"] for row in attempts] == ["retryable", "done"]
    assert state.one("SELECT COUNT(*) FROM agent WHERE id=?", (f"plan-critic-{review['id']}",))[0] == 1


@pytest.mark.parametrize("role", ["worker", "critic", "planner", "plan_critic"])
def test_typed_provider_exception_persists_reset_epoch_for_every_role(setup, role):
    """A typed launch exception has the same reset evidence as a terminal event."""
    cfg, state, repo = setup
    future = time.time() + 3600

    class ProviderUnavailable(Exception):
        terminal_category = "throttled"

    def unavailable(_cwd, _outcome):
        raise ProviderUnavailable(f"rate limited; retry at {future}")

    adapter = ScriptedAdapter([unavailable])
    if role in {"worker", "critic"}:
        task = state.add_task("neocortex", f"{role} reset", "objective", [])
        state.set_task(task, status="in_review" if role == "critic" else "in_progress")
        agent_id = state.add_agent(f"{role}-reset", role, "neocortex", task, "model")
        outcome = turn.run_turn(state, cfg, adapter,
                                state.one("SELECT * FROM agent WHERE id=?", (agent_id,)),
                                repo, "main")
    elif role == "planner":
        proposal = state.add_proposal("neocortex", "planner", "", [planner_spec()])
        planner_id, _ = state.planner_feedback(None, "revise", "model", proposal_id=proposal)
        outcome = turn.run_planner_turn(
            state, cfg, state.one("SELECT * FROM agent WHERE id=?", (planner_id,)), adapter,
        )
    else:
        proposal = state.add_proposal("neocortex", "planner", "", [planner_spec()])
        adapter.run_planner = adapter.run
        outcome = turn.run_plan_critic_turn(
            state, cfg, state.one("SELECT * FROM proposal WHERE id=?", (proposal,)), adapter,
        )
    run = state.one("SELECT * FROM run ORDER BY id DESC")
    assert outcome.deferred
    assert run["terminal_category"] == "throttled"
    assert run["defer_until"] == pytest.approx(future)


def test_typed_preflight_exception_defers_without_incident_and_retries(setup, monkeypatch):
    cfg, state, _repo = setup
    state.add_task("neocortex", "preflight retry", "objective", [])
    scheduler = sched(cfg, state, [emit({"outcome": "YIELD", "summary": "later"})])
    monkeypatch.setattr(scheduler, "readiness", lambda: (True, "test"))

    class ProviderUnavailable(Exception):
        terminal_category = "throttled"

    calls = [ProviderUnavailable("retry at 1999999999"), None]

    def preflight():
        item = calls.pop(0)
        if item:
            raise item
        return True, "ok"

    monkeypatch.setattr(scheduler, "preflight", preflight)
    assert scheduler.run(max_turns=1)
    assert not scheduler.adapter.calls
    attempt = state.one("SELECT * FROM preflight_attempt")
    assert attempt["role"] == "worker" and attempt["category"] == "throttled"
    assert not state.open_incidents()
    assert scheduler.run(max_turns=1)
    assert len(scheduler.adapter.calls) == 1


class TimerFlakyAdapter(ScriptedAdapter):
    """A fake provider which fails completed sessions before later recovery."""

    def __init__(self, script, temporary_failures=1, *, permanent=False):
        super().__init__(script)
        self.temporary_failures = temporary_failures
        self.permanent = permanent

    def run(self, *args, **kwargs):
        result = super().run(*args, **kwargs)
        if self.temporary_failures:
            self.temporary_failures -= 1
            if self.permanent:
                result.exit_code = 1
            else:
                # This is adapter-owned terminal evidence, after the agent has
                # made a partial change/written an outcome, not agent JSON.
                result.terminal_category = "transient"
                result.terminal_diagnostic = "provider connection reset"
        return result


def timer_invocation(cfg, state, adapter):
    """Fresh Scheduler instance: equivalent to the next five-minute timer run."""
    scheduler = Scheduler(cfg, state)
    scheduler._adapter_for = lambda _role: adapter
    scheduler.readiness = lambda: (True, "isolated fake host")
    scheduler.preflight = lambda: (True, "isolated fake model")
    assert scheduler.run(max_turns=1)
    return scheduler


@pytest.mark.parametrize("role", ["worker", "critic", "planner", "plan_critic"])
def test_timer_invocation_defers_each_role_and_later_applies_once(setup, role):
    """A provider outage ends this timer run; its next run resumes logical work."""
    cfg, state, _repo = setup
    def partial(cwd, outcome_path):
        (cwd / "partial-provider-state").write_text("keep\n")
        outcome_path.parent.mkdir(parents=True, exist_ok=True)
        outcome_path.write_text(json.dumps({"outcome": "YIELD", "summary": "interrupted"}))
    if role == "worker":
        task = state.add_task("neocortex", "worker outage", "objective", [])
        adapter = TimerFlakyAdapter([
            partial,
            commit_and_emit("complete", "ok\n", {"outcome": "DONE", "summary": "done"}),
        ])
        # Undelivered feedback must survive the failed host session.
        state.add_agent(f"worker-{task}", "worker", "neocortex", task, "model")
        state.set_task(task, status="in_progress")
        state.send(protocol.FEEDBACK, "owner", f"worker-{task}", {"text": "retain"}, task)
    elif role == "critic":
        task = state.add_task("neocortex", "critic outage", "objective", [])
        state.set_task(task, status="in_review")
        state.add_agent(f"worker-{task}", "worker", "neocortex", task, "model")
        state.set_agent(f"worker-{task}", state="blocked")
        state.add_agent(f"critic-{task}-1", "critic", "neocortex", task, "model")
        state.send(protocol.FEEDBACK, "owner", f"critic-{task}-1", {"text": "retain"}, task)
        adapter = TimerFlakyAdapter([
            partial,
            emit({"outcome": "DONE", "verdict": "rework", "findings": ["fix"]}),
        ])
    elif role == "planner":
        planner, _ = state.planner_feedback(None, "retain revision wake", "model")
        adapter = TimerFlakyAdapter([
            partial,
            emit({"outcome": "DONE", "summary": "proposal", "proposal": [planner_spec()]}),
        ])
    else:
        proposal = state.add_proposal("neocortex", "planner", "", [planner_spec()])
        adapter = TimerFlakyAdapter([
            partial,
            emit({"outcome": "DONE", "recommendation": "keep", "findings": ["sound"]}),
        ])
        adapter.run_planner = adapter.run

    # The first invocation makes exactly one dispatch and returns on deferral.
    timer_invocation(cfg, state, adapter)
    assert len(adapter.calls) == 1
    assert not state.open_incidents()
    assert state.one("SELECT COUNT(*) FROM run")[0] == 1
    assert state.one("SELECT turns FROM agent WHERE role=? ORDER BY id LIMIT 1", (role,))[0] == 0
    if role in {"worker", "critic"}:
        assert (cfg.work_dir / task / "partial-provider-state").read_text() == "keep\n"
        recipient = f"worker-{task}" if role == "worker" else f"critic-{task}-1"
        assert state.inbox(recipient), "host outage cannot acknowledge feedback"
        assert state.one("SELECT attempts FROM task WHERE id=?", (task,))[0] == 0
    elif role == "planner":
        assert state.inbox(planner), "planner feedback and wake remain durable"
        assert state.one("SELECT state FROM agent WHERE id=?", (planner,))[0] == "runnable"
    else:
        review = state.one("SELECT * FROM plan_review WHERE proposal_id=?", (proposal,))
        assert review["status"] == "retryable"

    # A new Scheduler object models the later timer process, not an in-memory retry.
    timer_invocation(cfg, state, adapter)
    assert len(adapter.calls) == 2
    if role == "worker":
        assert state.one("SELECT status FROM task WHERE id=?", (task,))[0] == "in_review"
        assert state.one("SELECT COUNT(*) FROM agent WHERE role='critic' AND task_id=?", (task,))[0] == 1
    elif role == "critic":
        assert state.one("SELECT status FROM task WHERE id=?", (task,))[0] == "in_progress"
        assert state.one("SELECT COUNT(*) FROM message WHERE kind=? AND sender=? AND task_id=?",
                         (protocol.REVIEW_VERDICT, f"critic-{task}-1", task))[0] == 1
    elif role == "planner":
        assert state.one("SELECT COUNT(*) FROM proposal")[0] == 1
        assert not state.inbox(planner)
    else:
        review = state.one("SELECT * FROM plan_review WHERE proposal_id=?", (proposal,))
        attempts = state.q("SELECT status FROM plan_review_attempt WHERE review_id=? ORDER BY id",
                           (review["id"],))
        assert review["status"] == "done"
        assert [row["status"] for row in attempts] == ["retryable", "done"]


def test_timer_outages_exceed_task_and_turn_budgets_without_breaker(setup):
    """Repeated provider outages are recorded runs, never task/turn failures."""
    cfg, state, _repo = setup
    cfg.max_attempts = 2
    cfg.max_consecutive_failures = 2
    task = state.add_task("neocortex", "many outages", "objective", [], budget_turns=2)
    state.add_agent(f"worker-{task}", "worker", "neocortex", task, "model")
    state.set_task(task, status="in_progress")
    adapter = TimerFlakyAdapter(
        [emit({"outcome": "YIELD", "summary": "interrupted"})] * 3
        + [emit({"outcome": "YIELD", "summary": "recovered"})],
        temporary_failures=3,
    )
    for _ in range(3):
        scheduler = timer_invocation(cfg, state, adapter)
        assert scheduler.consecutive_failures == 0
    agent = state.one("SELECT turns, state FROM agent WHERE id=?", (f"worker-{task}",))
    task_row = state.one("SELECT attempts, status FROM task WHERE id=?", (task,))
    assert (agent["turns"], agent["state"], task_row["attempts"], task_row["status"]) == (0, "runnable", 0, "in_progress")
    assert not (cfg.home / "STOP").exists()
    timer_invocation(cfg, state, adapter)
    assert state.one("SELECT turns FROM agent WHERE id=?", (f"worker-{task}",))[0] == 1


def test_timer_keeps_stop_and_nontransient_fail_policy(setup):
    """Deferral coverage does not weaken owner STOP or ordinary host failures."""
    cfg, state, _repo = setup
    task = state.add_task("neocortex", "stop first", "objective", [])
    adapter = TimerFlakyAdapter([emit({"outcome": "YIELD", "summary": "failed"})], permanent=True)
    cfg.home.mkdir(parents=True, exist_ok=True)
    (cfg.home / "STOP").write_text("owner stop\n")
    timer_invocation(cfg, state, adapter)
    assert not adapter.calls
    (cfg.home / "STOP").unlink()
    timer_invocation(cfg, state, adapter)
    assert state.one("SELECT attempts FROM task WHERE id=?", (task,))[0] == 1


def test_agent_authored_host_deferred_flag_does_not_bypass_fail_policy(setup, monkeypatch):
    """Only adapter assessment, never outcome JSON, may defer a failed turn."""
    cfg, state, _repo = setup
    task_id = state.add_task("neocortex", "forged defer", "objective", [])
    scheduler = sched(cfg, state, [emit({"outcome": "FAIL", "summary": "real failure",
                                        "host_deferred": True})])
    monkeypatch.setattr(scheduler, "preflight", lambda: (True, "ok"))
    assert scheduler.step() == protocol.FAIL
    task = state.one("SELECT status, attempts FROM task WHERE id=?", (task_id,))
    assert task["attempts"] == 1
    assert scheduler.consecutive_failures == 1


def test_preflight_reset_in_log_tail_is_not_trusted_without_terminal_evidence(setup, monkeypatch):
    """A quoted retry epoch in ordinary CLI output cannot defer the scheduler."""
    cfg, state, _repo = setup
    state.add_task("neocortex", "untrusted reset", "objective", [])
    scheduler = sched(cfg, state, [])
    future = time.time() + 3600

    class TailOnlyAdapter(ScriptedAdapter):
        def run(self, prompt, cwd, model, log_path, timeout_s):
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(f"tool said retry at {future}")
            return SessionResult(1, log_path, None, False, "transient", "provider unavailable")

    adapter = TailOnlyAdapter([])
    scheduler._adapter_for = lambda _role: adapter
    monkeypatch.setattr(scheduler, "_free_mb", lambda: 9999)
    scheduler._in_run = True
    try:
        assert scheduler.step() == "deferred"
    finally:
        scheduler._in_run = False
    attempt = state.one("SELECT defer_until FROM preflight_attempt")
    assert attempt["defer_until"] is None


@pytest.mark.parametrize("failure", ["nonzero", "timeout", "terminal"])
def test_planner_host_failure_with_valid_done_keeps_revision_context(setup, failure):
    """A planner's valid proposal never consumes feedback after host failure."""
    cfg, state, _repo = setup
    original_proposal = state.add_proposal("neocortex", "planner", "", [planner_spec()])
    planner_id, _ = state.planner_feedback(
        None, "retain revision feedback", "model", proposal_id=original_proposal,
    )
    state.set_agent(planner_id, memo="retain planner memo")
    adapter = ScriptedAdapter([emit({"outcome": "DONE", "proposal": [planner_spec()],
                                    "memo": "discard planner memo"})])
    original_run = adapter.run

    def failed_session(*args):
        result = original_run(*args)
        if failure == "nonzero":
            result.exit_code = 1
        elif failure == "timeout":
            result.timed_out = True
        else:
            result.terminal_category = "transient"
        return result

    adapter.run = failed_session
    planner = state.one("SELECT * FROM agent WHERE id=?", (planner_id,))
    outcome = turn.run_planner_turn(state, cfg, planner, adapter)
    run = state.one("SELECT * FROM run WHERE agent_id=? ORDER BY id DESC", (planner_id,))
    assert outcome.kind == protocol.FAIL
    assert run["outcome"] == protocol.DONE and run["host_assessment"] == "FAILED"
    assert state.pending_revision(planner_id) is not None
    assert len(state.q("SELECT * FROM proposal")) == 1
    agent = state.one("SELECT state, turns, memo FROM agent WHERE id=?", (planner_id,))
    assert (agent["state"], agent["turns"], agent["memo"]) == (
        "runnable" if failure == "terminal" else "blocked",
        0 if failure == "terminal" else 1,
        "retain planner memo",
    )


@pytest.mark.parametrize('role', ['worker', 'critic', 'capacity'])
def test_revision_respects_other_work(setup, role):
    cfg, state, _ = setup
    original = state.add_proposal('neocortex', 'planner', '', [planner_spec()])
    aid, _ = state.planner_feedback(None, 'Revise', 'model', proposal_id=original)
    scheduler = sched(cfg, state, [])
    if role == 'capacity':
        state.add_proposal('neocortex', 'other', '', [planner_spec()])
        assert scheduler.pick() is None
        assert 'pending proposals' in state.one('SELECT planner_skip_reason FROM project')[0]
    else:
        tid = state.add_task('neocortex', 'Work', 'Do work', ['$ true'])
        state.add_agent('priority-agent', role, 'neocortex', tid, 'model')
        assert scheduler.pick()['id'] == 'priority-agent'
    assert state.pending_revision(aid) is not None


@pytest.mark.parametrize('timed_out', [False, True])
def test_revision_rejects_done_from_failed_session(setup, timed_out):
    cfg, state, _ = setup
    original = state.add_proposal('neocortex', 'planner', '', [planner_spec()])
    aid, _ = state.planner_feedback(None, 'Revise', 'model', proposal_id=original)
    scheduler = sched(cfg, state, [
        emit({'outcome': 'DONE', 'proposal': [planner_spec()]}),
    ])
    run = scheduler.adapter.run

    def failed_run(*args):
        result = run(*args)
        result.exit_code = 1
        result.timed_out = timed_out
        return result

    scheduler.adapter.run = failed_run
    assert scheduler.step() == protocol.FAIL
    assert state.pending_revision(aid) is not None
    assert len(state.q('SELECT * FROM proposal')) == 1
    assert not state.q('SELECT * FROM task')


@pytest.mark.parametrize("role", ["worker", "planner", "requeued", "fresh"])
@pytest.mark.parametrize("channel", ["cli", "http"])
def test_owner_answers_scheduler_question(setup, role, channel, capsys):
    import http.client
    import threading
    import urllib.parse

    from nc.ui import make_server

    cfg, state, _repo = setup
    if role != "planner":
        tid = state.add_task("neocortex", "question", "ask owner", [])
        agent_id = f"worker-{tid}"
        if role in ("requeued", "fresh"):
            from nc import operations

            accepted = sched(cfg, state, [
                commit_and_emit("marker.txt", "accepted\n", {"outcome": "DONE", "summary": "done"}),
                emit({"outcome": "DONE", "verdict": "pass", "summary": "accepted"}),
            ])
            assert accepted.step() == protocol.DONE
            assert accepted.step() == protocol.DONE
            historical = state.send(protocol.QUESTION, agent_id, "owner",
                                    {"question": "Old question"}, tid)
            operations.rollback_task(
                state, tid, state.one("SELECT merge_commit FROM task WHERE id=?", (tid,))[0])
            discard = (operations.discard_preview(cfg, state, tid)["token"]
                       if role == "fresh" else None)
            operations.requeue_task(cfg, state, tid, fresh=role == "fresh",
                                    expected_discard=discard)
            assert state.one("SELECT merge_commit FROM task WHERE id=?", (tid,))[0]
            with pytest.raises(ValueError, match="not a currently answerable"):
                operations.answer_message(state, historical, "Obsolete")
    else:
        tid = None
        agent_id = "planner-neocortex"
        state.add_agent(agent_id, "planner", "neocortex", None, "model")
    scheduler = sched(cfg, state, [emit({
        "outcome": "ASK", "to": "owner", "question": "<script>scope?</script>",
    })])
    assert scheduler.step() == protocol.ASK
    question = state.inbox("owner")[0]
    assert question["kind"] == protocol.QUESTION
    if channel == "cli":
        assert cli.main([
            "--home", str(cfg.home), "answer", str(question["id"]), "Proceed",
        ]) == 0
        assert f"answered {agent_id}; it is runnable again" in capsys.readouterr().out
    else:
        server = make_server(cfg, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            conn.request("GET", "/inbox")
            response = conn.getresponse()
            cookie = response.getheader("Set-Cookie").split(";")[0]
            body = response.read().decode()
            assert response.status == 200
            assert "<script>scope?" not in body
            assert "&lt;script&gt;scope?" in body
            path = f'/messages/{question["id"]}/answer'
            assert path in body
            token = re.search(r'name="csrf_token" value="([^"]+)"', body)[1]
            conn.request("POST", path, urllib.parse.urlencode({
                "csrf_token": token, "text": "Proceed",
            }), {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded",
                 "Origin": f"http://127.0.0.1:{server.server_port}"})
            response = conn.getresponse()
            response.read()
            assert response.status == 303
        finally:
            conn.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
    assert state.one("SELECT state FROM agent WHERE id=?", (agent_id,))[0] == "runnable"
    assert state.one("SELECT delivered FROM message WHERE id=?", (question["id"],))[0] == 1
    answer = state.one("SELECT * FROM message WHERE in_reply_to=?", (question["id"],))
    assert answer["kind"] == protocol.ANSWER
    assert json.loads(answer["payload"]) == {"answer": "Proceed"}
    if tid:
        assert state.one("SELECT status FROM task WHERE id=?", (tid,))[0] == "in_progress"


def test_acceptance_survives_locked_worktree_and_allows_rollback(setup):
    from nc import arbiter, operations

    cfg, state, repo = setup
    tid = state.add_task("neocortex", "marker", "create marker", [])
    scheduler = sched(cfg, state, [
        commit_and_emit("marker.txt", "accepted\n", {"outcome": "DONE", "summary": "done"}),
        emit({"outcome": "DONE", "verdict": "pass", "summary": "accepted"}),
    ])
    assert scheduler.step() == protocol.DONE
    worktree = cfg.work_dir / tid
    arbiter.git(repo, "worktree", "lock", str(worktree))
    assert scheduler.step() == protocol.DONE
    task = state.one("SELECT * FROM task WHERE id=?", (tid,))
    assert task["status"] == "done"
    assert task["merge_commit"] == arbiter.git(repo, "rev-parse", "--short", "HEAD")
    assert (repo / "marker.txt").read_text() == "accepted\n"
    assert worktree.exists()
    assert all(a["state"] == "done" for a in state.q(
        "SELECT * FROM agent WHERE task_id=?", (tid,),
    ))
    incident = state.one("SELECT * FROM incident WHERE kind='worktree_cleanup'")
    assert tid in incident["detail"] and "locked" in incident["detail"]
    result = operations.rollback_task(state, tid, task["merge_commit"])
    assert result["reverted_commit"] == task["merge_commit"]
    assert not (repo / "marker.txt").exists()
    assert state.one("SELECT status FROM task WHERE id=?", (tid,))[0] == "blocked"


@pytest.mark.parametrize('action', ['cancel', 'requeue'])
def test_stale_selection_does_not_prepare_worktree(setup, monkeypatch, action):
    from nc import arbiter, operations

    cfg, state, _repo = setup
    tid = state.add_task('neocortex', 'stale', 'must not run', [])
    scheduler = sched(cfg, state, [])
    selected = scheduler.pick()
    if action == 'cancel':
        state.set_task(tid, status='blocked')
        operations.cancel_task(state, tid, 'owner cancelled')
    else:
        operations.requeue_task(cfg, state, tid)
    before = list(state.db.iterdump())
    monkeypatch.setattr(scheduler, 'pick', lambda: selected)

    def unexpected(*args):
        pytest.fail('stale selection prepared a worktree')

    monkeypatch.setattr(arbiter, 'ensure_worktree', unexpected)
    assert scheduler.step() == 'idle'
    assert list(state.db.iterdump()) == before


def test_direct_queue_activation_respects_lifecycle_ownership(setup):
    cfg, state, _repo = setup
    state.add_task("neocortex", "queued", "must wait", [])
    scheduler = sched(cfg, state, [])

    with lifecycle_lock(state), pytest.raises(LifecycleBusy, match="retry"):
        scheduler.spawn_for_queued_task()

    assert scheduler.spawn_for_queued_task()


@pytest.mark.parametrize("phase", ["before_worktree_preparation", "after_end_run"])
def test_real_scheduler_barriers_reject_fresh_requeue_until_release(setup, phase):
    """Actual scheduler barriers cover preparation and post-end_run checks."""
    from nc import arbiter, operations

    cfg, state, repo = setup
    tid = state.add_task("neocortex", "barrier", "do not discard", [])
    state.set_task(tid, status="in_progress")
    state.add_agent(f"worker-{tid}", "worker", "neocortex", tid, "model")
    worktree, branch = arbiter.ensure_worktree(repo, cfg.work_dir, tid)
    token = operations.discard_preview(cfg, state, tid)["token"]
    entered, release = multiprocessing.Event(), multiprocessing.Event()
    child = multiprocessing.Process(
        target=_run_scheduler_to_barrier,
        args=(str(cfg.db_path), str(cfg.home), phase, entered, release),
    )
    child.start()
    try:
        assert entered.wait(5)
        # Snapshot after the real scheduler reached its protected phase.
        before = list(state.db.iterdump())
        with pytest.raises(LifecycleBusy, match="retry"):
            operations.requeue_task(cfg, state, tid, fresh=True, expected_discard=token)
        assert worktree.exists()
        assert arbiter.git(repo, "rev-parse", "--verify", branch)
        assert list(state.db.iterdump()) == before
    finally:
        release.set()
        child.join(10)
        if child.is_alive():
            child.terminate()
            child.join()
    assert child.exitcode == 0
    # Revalidation makes the original confirmation stale after the real turn.
    token = operations.discard_preview(cfg, state, tid)["token"]
    operations.requeue_task(cfg, state, tid, fresh=True, expected_discard=token)
    assert not worktree.exists()
    assert state.one("SELECT status FROM task WHERE id=?", (tid,))[0] == "queued"


def test_real_integration_barrier_blocks_rollback_across_repository_aliases(setup, tmp_path):
    """Rollback cannot overlap a real scheduler integration through an alias."""
    from nc import arbiter, operations

    cfg, state, repo = setup
    accepted = state.add_task("neocortex", "accepted", "already merged", [])
    other = state.add_task("neocortex", "rollback", "undo me", [])
    (repo / "accepted.txt").write_text("accepted\n")
    arbiter.git(repo, "add", "accepted.txt")
    arbiter.git(repo, "commit", "-m", "accepted")
    commit = arbiter.git(repo, "rev-parse", "--short", "HEAD")
    state.set_task(accepted, status="in_review")
    state.set_task(other, status="done", merge_commit=commit)
    alias = tmp_path / "repo-alias"
    alias.symlink_to(repo, target_is_directory=True)
    state.x("UPDATE project SET repo_path=? WHERE id='neocortex'", (str(alias),))
    worktree, _branch = arbiter.ensure_worktree(alias, cfg.work_dir, accepted)
    (worktree / "integrated.txt").write_text("integrated\n")
    arbiter.git(worktree, "add", "integrated.txt")
    arbiter.git(worktree, "commit", "-m", "integration work")
    state.add_agent(f"worker-{accepted}", "worker", "neocortex", accepted, "model")
    state.set_agent(f"worker-{accepted}", state="blocked")
    state.add_agent(f"critic-{accepted}-1", "critic", "neocortex", accepted, "model")
    head = arbiter.git(repo, "rev-parse", "HEAD")
    entered, release = multiprocessing.Event(), multiprocessing.Event()
    child = multiprocessing.Process(target=_run_scheduler_to_barrier,
                                    args=(str(cfg.db_path), str(cfg.home),
                                          "before_integration", entered, release))
    child.start()
    try:
        assert entered.wait(5)
        # The critic has recorded its verdict, but integration has not started.
        before = list(state.db.iterdump())
        with pytest.raises(LifecycleBusy, match="busy"):
            operations.rollback_task(state, other, commit)
        assert arbiter.git(repo, "rev-parse", "HEAD") == head
        assert list(state.db.iterdump()) == before
    finally:
        release.set()
        child.join(10)
        if child.is_alive():
            child.terminate()
            child.join()
    assert child.exitcode == 0
    assert (repo / "integrated.txt").exists()
    result = operations.rollback_task(state, other, commit)
    assert result["reverted_commit"] == commit
    assert state.one("SELECT status FROM task WHERE id=?", (other,))[0] == "blocked"
