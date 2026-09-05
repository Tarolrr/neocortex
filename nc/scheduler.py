"""Cooperative scheduler.

Parallelism on this box is 1, but agents never learn that. An agent that needs an
answer ends its turn with ASK and stops existing; the scheduler wakes it when the
answer lands in its inbox. Waiting therefore costs no memory and no process.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

from . import arbiter, protocol, turn
from .adapters import Adapter, get_adapter
from .config import Config
from .state import State

log = logging.getLogger("nc.scheduler")

MAX_ATTEMPTS = 3


class Scheduler:
    def __init__(self, cfg: Config, state: State):
        self.cfg = cfg
        self.state = state
        self.adapter: Adapter = get_adapter(cfg.adapter)
        self.consecutive_failures = 0

    # --- preflight --------------------------------------------------------
    def preflight(self) -> tuple[bool, str]:
        if not self.adapter.available():
            return False, f"adapter {self.adapter.name} is not installed"

        free_mb = self._free_mb()
        if free_mb is not None and free_mb < self.cfg.min_free_mb:
            return False, f"only {free_mb} MB RAM available"

        probe_dir = self.cfg.home / "preflight"
        probe_dir.mkdir(parents=True, exist_ok=True)
        model = self.cfg.model_for("worker")
        result = self.adapter.run(
            "Reply with exactly: OK", probe_dir, model,
            probe_dir / "probe.log", self.cfg.preflight_timeout_s,
        )
        text = result.log_path.read_text(errors="replace")
        if result.exit_code != 0 or "OK" not in text:
            tail = text.strip()[-500:]
            return False, f"model {model} is not usable (exit {result.exit_code}): {tail}"
        return True, f"model {model} responds, {free_mb} MB free"

    @staticmethod
    def _free_mb() -> int | None:
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemAvailable"):
                    return int(line.split()[1]) // 1024
        except OSError:
            return None
        return None

    # --- scheduling -------------------------------------------------------
    def pick(self) -> sqlite3.Row | None:
        """Critics first (they unblock finished work), then workers by task priority."""
        row = self.state.one(
            "SELECT a.* FROM agent a JOIN task t ON t.id = a.task_id"
            " WHERE a.state='runnable'"
            " ORDER BY CASE a.role WHEN 'critic' THEN 0 ELSE 1 END, t.priority, t.created_at"
            " LIMIT 1"
        )
        if row:
            return row
        self.spawn_for_queued_task()
        return self.state.one(
            "SELECT a.* FROM agent a JOIN task t ON t.id = a.task_id"
            " WHERE a.state='runnable' ORDER BY t.priority, t.created_at LIMIT 1"
        )

    def spawn_for_queued_task(self) -> str | None:
        task = self.state.one(
            "SELECT * FROM task WHERE status='queued' ORDER BY priority, created_at LIMIT 1"
        )
        if not task:
            return None
        agent_id = f"worker-{task['id']}"
        existing = self.state.one("SELECT * FROM agent WHERE id=?", (agent_id,))
        if existing is None:
            self.state.add_agent(agent_id, "worker", task["project_id"], task["id"],
                                 self.cfg.model_for("worker"))
        else:
            self.state.set_agent(agent_id, state="runnable")
        self.state.set_task(task["id"], status="in_progress")
        log.info("spawned %s for %s", agent_id, task["id"])
        return agent_id

    # --- one turn ---------------------------------------------------------
    def step(self) -> str:
        agent = self.pick()
        if agent is None:
            return "idle"

        task = self.state.one("SELECT * FROM task WHERE id=?", (agent["task_id"],))
        project = self.state.one("SELECT * FROM project WHERE id=?", (agent["project_id"],))
        repo = Path(project["repo_path"])
        cwd, branch = arbiter.ensure_worktree(repo, self.cfg.work_dir, task["id"])

        if agent["turns"] >= task["budget_turns"]:
            self._block(task, agent, f"turn budget ({task['budget_turns']}) exhausted")
            return "budget_exhausted"

        checks_text = ""
        if agent["role"] == "critic":
            checks_text = self._last_checks_text(task["id"])

        outcome = turn.run_turn(self.state, self.cfg, self.adapter, agent, cwd, branch,
                                checks_text)
        log.info("%s (%s) -> %s: %s", agent["id"], agent["role"], outcome.kind,
                 outcome.summary[:200])

        if outcome.kind in (protocol.NO_OUTCOME, protocol.FAIL):
            self.consecutive_failures += 1
        else:
            self.consecutive_failures = 0

        handler = {
            protocol.ASK: self._on_ask,
            protocol.YIELD: self._on_yield,
            protocol.FAIL: self._on_fail,
            protocol.NO_OUTCOME: self._on_fail,
            protocol.DONE: self._on_done,
        }[outcome.kind]
        handler(agent, task, project, cwd, branch, outcome)
        return outcome.kind

    # --- outcome handlers -------------------------------------------------
    def _on_ask(self, agent, task, project, cwd, branch, outcome) -> None:
        recipient = "owner" if outcome.to not in ("worker", "critic") else outcome.to
        self.state.send(protocol.QUESTION, agent["id"], recipient,
                        {"question": outcome.question, "summary": outcome.summary},
                        task_id=task["id"])
        self.state.set_agent(agent["id"], state="blocked")
        self.state.set_task(task["id"], status="blocked")

    def _on_yield(self, agent, task, project, cwd, branch, outcome) -> None:
        self.state.set_agent(agent["id"], state="runnable")

    def _on_fail(self, agent, task, project, cwd, branch, outcome) -> None:
        attempts = task["attempts"] + 1
        self.state.set_task(task["id"], attempts=attempts)
        if attempts >= MAX_ATTEMPTS:
            self._block(task, agent, f"failed {attempts} times: {outcome.summary}")
        else:
            self.state.set_agent(agent["id"], state="runnable")

    def _on_done(self, agent, task, project, cwd, branch, outcome) -> None:
        if agent["role"] == "critic":
            self._apply_verdict(agent, task, project, cwd, branch, outcome)
            return

        repo = Path(project["repo_path"])
        if not arbiter.has_commits(repo, cwd, branch):
            self._rework(agent, task, ["You reported DONE but the branch has no commits."])
            return

        commands, _ = arbiter.parse_acceptance(json.loads(task["acceptance"]))
        if project["test_cmd"]:
            commands = commands + [project["test_cmd"]]
        results = arbiter.run_checks(cwd, commands)
        self._store_checks(task["id"], results)

        failed = [r for r in results if not r.ok]
        if failed:
            self._rework(agent, task, [r.render() for r in failed])
            return

        critic_id = f"critic-{task['id']}-{task['attempts'] + 1}"
        self.state.add_agent(critic_id, "critic", project["id"], task["id"],
                             self.cfg.model_for("critic"))
        self.state.set_agent(agent["id"], state="blocked")
        self.state.set_task(task["id"], status="in_review")

    def _apply_verdict(self, agent, task, project, cwd, branch, outcome) -> None:
        verdict = outcome.verdict or "rework"
        worker_id = f"worker-{task['id']}"
        self.state.send(protocol.REVIEW_VERDICT, agent["id"], worker_id,
                        {"verdict": verdict, "summary": outcome.summary,
                         "findings": outcome.findings}, task_id=task["id"])
        self.state.set_agent(agent["id"], state="done")

        if verdict == "pass":
            repo = Path(project["repo_path"])
            commit = arbiter.integrate(repo, branch, task["id"])
            error = arbiter.mirror(repo, project["mirror"], branch)
            arbiter.remove_worktree(repo, cwd)
            if error:
                self.state.incident("mirror_push", f"{task['id']}: {error}")
            self.state.set_task(task["id"], status="done", merge_commit=commit,
                                result=f"{outcome.summary} (merged as {commit})")
            self.state.set_agent(worker_id, state="done")
            log.info("task %s accepted and merged as %s", task["id"], commit)
        elif verdict == "reject":
            self._block(task, agent, f"critic rejected the approach: {outcome.summary}")
        else:
            self._rework(agent, task, outcome.findings or [outcome.summary])

    # --- helpers ----------------------------------------------------------
    def _rework(self, agent, task, findings: list[str]) -> None:
        worker_id = f"worker-{task['id']}"
        attempts = task["attempts"] + 1
        self.state.send(protocol.REVIEW_VERDICT, "arbiter", worker_id,
                        {"verdict": "rework", "summary": "acceptance not met",
                         "findings": findings}, task_id=task["id"])
        self.state.set_task(task["id"], status="in_progress", attempts=attempts)
        if attempts >= MAX_ATTEMPTS:
            self._block(task, agent, f"{attempts} rework cycles without acceptance")
            return
        self.state.set_agent(worker_id, state="runnable")

    def _block(self, task, agent, reason: str) -> None:
        self.state.set_task(task["id"], status="blocked")
        self.state.set_agent(agent["id"], state="blocked")
        self.state.incident("task_blocked", f"{task['id']}: {reason}")
        self.state.send(protocol.INCIDENT, "scheduler", "owner",
                        {"task": task["id"], "reason": reason}, task_id=task["id"])
        log.warning("task %s blocked: %s", task["id"], reason)

    def _store_checks(self, task_id: str, results) -> None:
        path = self.cfg.home / "checks" / f"{task_id}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(arbiter.checks_summary(results))

    def _last_checks_text(self, task_id: str) -> str:
        path = self.cfg.home / "checks" / f"{task_id}.txt"
        return path.read_text() if path.exists() else "(none)"

    # --- loop -------------------------------------------------------------
    def run(self, max_turns: int = 0) -> None:
        ok, detail = self.preflight()
        if not ok:
            self.state.incident("preflight", detail)
            log.error("preflight failed: %s", detail)
            return
        log.info("preflight ok: %s", detail)

        turns = 0
        while max_turns == 0 or turns < max_turns:
            if (self.cfg.home / "STOP").exists():
                log.info("stop file present, exiting")
                return
            result = self.step()
            if result == "idle":
                log.info("no runnable agents; exiting (idle is a valid outcome)")
                return
            turns += 1
            if self.consecutive_failures >= self.cfg.max_consecutive_failures:
                self.state.incident(
                    "circuit_breaker",
                    f"{self.consecutive_failures} consecutive failed turns; stopping",
                )
                # The timer would otherwise restart us straight into the same
                # failure every few minutes. Stay down until a human says go.
                (self.cfg.home / "STOP").write_text(
                    f"circuit breaker: {self.consecutive_failures} consecutive failed turns\n"
                )
                log.error("circuit breaker tripped after %d failed turns; wrote STOP",
                          self.consecutive_failures)
                return
            time.sleep(1)
