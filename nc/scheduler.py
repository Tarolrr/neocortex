"""Cooperative scheduler.

Parallelism on this box is 1, but agents never learn that. An agent that needs an
answer ends its turn with ASK and stops existing; the scheduler wakes it when the
answer lands in its inbox. Waiting therefore costs no memory and no process.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import time
from pathlib import Path

from . import arbiter, protocol, turn
from .adapters import Adapter, assess_session, get_adapter
from .config import Config
from .lifecycle import LifecycleBusy, lifecycle_lock, repository_lock
from .state import State

log = logging.getLogger("nc.scheduler")

VERDICTS = {"pass", "rework", "reject"}


class Scheduler:
    def __init__(self, cfg: Config, state: State):
        self.cfg = cfg
        self.state = state
        self.consecutive_failures = 0
        # A deliberately narrow test seam for process barriers.  Hooks run
        # while the lifecycle/repository exclusions are actually owned.
        self._lifecycle_hook = None
        self._preflight_pairs: set[tuple[str, str]] = set()
        self._preflight_category: str | None = None
        self._in_run = False
        self._preflight_role = "worker"

    def _hook(self, phase: str) -> None:
        if self._lifecycle_hook is not None:
            self._lifecycle_hook(phase)

    def _adapter_for(self, role: str) -> Adapter:
        return get_adapter(self.cfg.adapter_for(role))

    # --- preflight --------------------------------------------------------
    def preflight(self) -> tuple[bool, str]:
        """Probe the selected adapter/model pair once for this invocation."""
        role = self._preflight_role
        adapter = self._adapter_for(role)
        if not adapter.available():
            return False, f"adapter {adapter.name} is not installed"

        free_mb = self._free_mb()
        if free_mb is not None and free_mb < self.cfg.min_free_mb:
            return False, f"only {free_mb} MB RAM available"

        probe_dir = self.cfg.home / "preflight"
        probe_dir.mkdir(parents=True, exist_ok=True)
        model = self.cfg.model_for(role)
        result = adapter.run(
            "Reply with exactly: OK", probe_dir, model,
            probe_dir / "probe.log", self.cfg.preflight_timeout_s,
        )
        assessment = assess_session(result, adapter.name)
        self._preflight_category = assessment.category if assessment.failed else None
        text = result.log_path.read_text(errors="replace")
        # The probe's terminal nonblank response is the contract; an arbitrary
        # OK somewhere in CLI/tool output is not completion evidence.
        terminal = next((line.strip() for line in reversed(text.splitlines()) if line.strip()), "")
        if assessment.failed or terminal != "OK":
            tail = text.strip()[-500:]
            return False, f"model {model} is not usable ({assessment.category}): {tail}"
        return True, f"model {model} responds, {free_mb} MB free"

    def _preflight_selected(self, role: str) -> str | None:
        if not self._in_run:
            return None
        pair = (self.cfg.adapter_for(role), self.cfg.model_for(role))
        if pair in self._preflight_pairs:
            return None
        self._preflight_role = role
        self._preflight_category = None
        ok, detail = self.preflight()
        self._preflight_pairs.add(pair)
        if ok:
            return None
        # Preflight output is host diagnostics.  It has no task/agent effects.
        # A supported temporary terminal category is deferred by the timer;
        # unknown/permanent readiness failures retain the existing incident path.
        if self._preflight_category in {
            "subscription_limit", "throttled", "overloaded", "transient",
        }:
            log.warning("temporary %s preflight failure: %s", role, detail)
            return "deferred"
        self.state.incident("preflight", detail)
        log.error("preflight failed: %s", detail)
        return "preflight_failed"

    def readiness(self) -> tuple[bool, str]:
        """Check host tools and each project's base checkout once per invocation."""
        adapters = {self.cfg.adapter, *self.cfg.adapters.values()}
        reports, errors, python = arbiter.host_requirements(adapters)
        if not errors:
            for project in self.state.q("SELECT id, repo_path, test_cmd FROM project ORDER BY id"):
                if not project["test_cmd"]:
                    continue
                results = arbiter.readiness_check(Path(project["repo_path"]), project["test_cmd"],
                                                  python=python)
                failed = [result for result in results if not result.ok]
                if failed:
                    errors.append(f"project {project['id']}: " + "; ".join(
                        result.render() for result in failed))
        if errors:
            return False, "\n".join(reports + errors + [
                "guidance: run scripts/bootstrap.sh on the runner host, then re-run nc doctor",
            ])
        return True, "\n".join(reports + ["configured project test commands passed"])

    def _host_environment_incident(self, detail: str) -> None:
        """Keep one unresolved host-readiness incident across timer restarts.

        Readiness detail includes command output from a disposable worktree.
        That output can legitimately contain unstable values (durations and
        temporary paths), so it must not be used as the deduplication key.
        """
        if self.state.one("SELECT id FROM incident WHERE kind='host_environment'"
                          " AND resolved=0") is None:
            self.state.incident("host_environment", detail)

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
        """Critics first, then workers by task priority, then project planners."""
        query = (
            "SELECT a.* FROM agent a JOIN task t ON t.id = a.task_id"
            " WHERE a.state='runnable' AND a.role IN ('worker','critic')"
            " AND t.status != 'cancelled'"
            " ORDER BY CASE a.role WHEN 'critic' THEN 0 ELSE 1 END,"
            " t.priority, t.created_at, a.updated_at, a.id LIMIT 1"
        )
        work = self.state.one(query)
        if work is None:
            # ``step`` already owns the lifecycle lock.  Keep the actual
            # activation private so the public entry point can acquire it too.
            self._spawn_for_queued_task()
            work = self.state.one(query)
        planner = None
        for candidate in self.state.q(
            "SELECT * FROM agent WHERE role='planner' AND task_id IS NULL"
            " AND state IN ('runnable','waiting') ORDER BY updated_at, id"
        ):
            project_id = candidate["project_id"]
            counts = self.state.one(
                "SELECT (SELECT COUNT(*) FROM task WHERE project_id=?"
                " AND status='queued') AS queued,"
                " (SELECT COUNT(*) FROM proposal WHERE project_id=?"
                " AND status='pending') AS pending", (project_id, project_id),
            )
            reason = None
            if work is not None:
                reason = "worker or critic is runnable"
            elif counts["pending"] >= self.cfg.planner_max_pending_proposals:
                reason = (f"pending proposals {counts['pending']} reached "
                          f"planner_max_pending_proposals={self.cfg.planner_max_pending_proposals}")
            elif counts["queued"] > self.cfg.planner_max_queued:
                reason = (f"queued tasks {counts['queued']} exceed "
                          f"planner_max_queued={self.cfg.planner_max_queued}")
            if reason:
                if candidate["state"] != "waiting":
                    self.state.set_agent(candidate["id"], state="waiting")
                self.state.x("UPDATE project SET planner_skip_reason=? WHERE id=?",
                             (reason, project_id))
                log.info("%s waiting: %s", candidate["id"], reason)
            elif planner is None:
                if candidate["state"] == "waiting":
                    self.state.set_agent(candidate["id"], state="runnable")
                planner = self.state.one("SELECT * FROM agent WHERE id=?", (candidate["id"],))
        return work if work is not None else planner

    def next_ready_task(self) -> sqlite3.Row | None:
        """A task is ready once every task it depends on has been accepted."""
        for task in self.state.q(
            "SELECT * FROM task WHERE status='queued' ORDER BY priority, created_at"
        ):
            if not self.state.unmet_dependencies(task["id"]):
                return task
        return None

    def spawn_for_queued_task(self) -> str | None:
        """Safely expose queued-task activation to non-``step`` callers."""
        with lifecycle_lock(self.state):
            return self._spawn_for_queued_task()

    def _spawn_for_queued_task(self) -> str | None:
        """Activate one ready task; caller owns ``lifecycle_lock``."""
        # Serialize queue selection and activation against owner cancellation.
        with self.state.db:
            self.state.db.execute("BEGIN IMMEDIATE")
            task = self.next_ready_task()
            if not task:
                return None
            agent_id = f"worker-{task['id']}"
            now = time.time()
            self.state.db.execute(
                "INSERT INTO agent(id,role,project_id,task_id,state,model,created_at,updated_at)"
                " VALUES(?,'worker',?,?,'runnable',?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET state='runnable', updated_at=excluded.updated_at",
                (agent_id, task["project_id"], task["id"],
                 self.cfg.model_for("worker"), now, now),
            )
            self.state.db.execute(
                "UPDATE task SET status='in_progress', updated_at=? WHERE id=?",
                (now, task["id"]),
            )
        log.info("spawned %s for %s", agent_id, task["id"])
        return agent_id

    # --- one turn ---------------------------------------------------------
    def step(self) -> str:
        try:
            with lifecycle_lock(self.state):
                return self._step_locked()
        except LifecycleBusy:
            return "idle"

    def _step_locked(self) -> str:
        agent = self.pick()
        if agent is None or agent["role"] == "planner":
            proposal = self.state.one(
                "SELECT p.* FROM proposal p WHERE p.status='pending'"
                " AND NOT EXISTS (SELECT 1 FROM plan_review r"
                " WHERE r.proposal_id=p.id AND r.spec=p.spec AND r.status IN ('running','done','failed'))"
                " ORDER BY p.id LIMIT 1"
            )
            if proposal:
                preflight = self._preflight_selected("plan_critic")
                if preflight is not None:
                    return preflight
                outcome = turn.run_plan_critic_turn(
                    self.state, self.cfg, proposal, self._adapter_for("plan_critic"),
                )
                # Advisory failures must not trip the workers' circuit breaker.
                return "deferred" if outcome.deferred else outcome.kind
        if agent is None:
            return "idle"

        if agent["role"] == "planner" and agent["task_id"] is None:
            preflight = self._preflight_selected("planner")
            if preflight is not None:
                return preflight
            outcome = turn.run_planner_turn(
                self.state, self.cfg, agent, self._adapter_for("planner"),
            )
            if outcome.deferred:
                return "deferred"
            if outcome.kind in (protocol.NO_OUTCOME, protocol.FAIL):
                self.consecutive_failures += 1
            else:
                self.consecutive_failures = 0
            log.info("%s (planner) -> %s: %s", agent["id"], outcome.kind, outcome.summary)
            return outcome.kind

        task = self.state.one("SELECT * FROM task WHERE id=?", (agent["task_id"],))
        project = self.state.one("SELECT * FROM project WHERE id=?", (agent["project_id"],))
        repo = Path(project["repo_path"])
        with repository_lock(repo):
            # Selection is a hint; reread eligibility before worktree preparation.
            current = self.state.one("SELECT * FROM agent WHERE id=?", (agent["id"],))
            latest = self.state.one("SELECT * FROM task WHERE id=?", (task["id"],))
            if (current is None or current["state"] != "runnable"
                    or latest["status"] not in ("in_progress", "in_review", "blocked")
                    or current["updated_at"] != agent["updated_at"]
                    or self.state.one("SELECT 1 FROM run WHERE ended_at IS NULL")):
                return "idle"
            return self._task_turn(current, latest, project, repo)

    def _task_turn(self, agent, task, project, repo):
        preflight = self._preflight_selected(agent["role"])
        if preflight is not None:
            return preflight
        if agent["turns"] >= task["budget_turns"]:
            self._block(task, agent, f"turn budget ({task['budget_turns']}) exhausted")
            return "budget_exhausted"

        # This is inside lifecycle_lock and repository_lock.  It is before the
        # run is registered, so a finished-run timestamp is never its boundary.
        self._hook("before_worktree_preparation")
        cwd, branch = arbiter.ensure_worktree(repo, self.cfg.work_dir, task["id"])

        checks_text = ""
        if agent["role"] == "critic":
            checks_text = self._last_checks_text(task["id"])

        adapter = self._adapter_for(agent["role"])
        outcome = turn.run_turn(self.state, self.cfg, adapter, agent, cwd, branch,
                                checks_text)
        # run_turn has recorded end_run, but acceptance and integration below
        # remain part of the same ownership window.
        self._hook("after_end_run")
        log.info("%s (%s) -> %s: %s", agent["id"], agent["role"], outcome.kind,
                 outcome.summary[:200])

        if outcome.kind in (protocol.NO_OUTCOME, protocol.FAIL):
            if outcome.deferred:
                return "deferred"
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
        try:
            handler(agent, task, project, cwd, branch, outcome)
        except Exception as exc:
            log.exception("error handling %s for %s", outcome.kind,
                          task["id"] if task else agent["id"])
            self.state.incident(
                "handler_error",
                f"{task['id'] if task else agent['id']}: {exc!r}",
            )
            if task is not None:
                self._block(task, agent, f"scheduler error while applying "
                             f"{outcome.kind}: {exc}")
            else:
                self.state.set_agent(agent["id"], state="blocked")
            self.consecutive_failures += 1
            return "error"
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
        if attempts >= self.cfg.max_attempts:
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

        reviews = self.state.one(
            "SELECT COUNT(*) AS c FROM agent WHERE task_id=? AND role='critic'", (task["id"],)
        )["c"]
        critic_id = f"critic-{task['id']}-{reviews + 1}"
        self.state.add_agent(critic_id, "critic", project["id"], task["id"],
                             self.cfg.model_for("critic"))
        self.state.set_agent(agent["id"], state="blocked")
        self.state.set_task(task["id"], status="in_review")

    def _apply_verdict(self, agent, task, project, cwd, branch, outcome) -> None:
        verdict = outcome.verdict
        if verdict not in VERDICTS:
            # A review without a verdict is a protocol violation by the critic, not
            # a finding against the worker: retry the review instead of reworking.
            self.state.incident(
                "protocol",
                f"{task['id']}: {agent['id']} ended DONE without a verdict "
                f"(got {verdict!r}): {outcome.summary[:200]}",
            )
            self.consecutive_failures += 1
            self._on_fail(agent, task, project, cwd, branch, outcome)
            return
        worker_id = f"worker-{task['id']}"
        self.state.send(protocol.REVIEW_VERDICT, agent["id"], worker_id,
                        {"verdict": verdict, "summary": outcome.summary,
                         "findings": outcome.findings}, task_id=task["id"])
        self.state.set_agent(agent["id"], state="done")

        if verdict == "pass":
            repo = Path(project["repo_path"])
            try:
                self._hook("before_integration")
                commit = arbiter.integrate(repo, branch, task["id"])
            except arbiter.MergeConflict as exc:
                base = arbiter.base_branch(repo)
                self.state.incident("merge_conflict", f"{task['id']}: {exc}")
                self._rework(agent, task, [
                    (
                        f"The critic accepted your work, but the branch no longer merges into {base}: "
                        f"conflicts in {', '.join(exc.files)}. Merge {base} into your branch, resolve the "
                        f"conflicts, keep the acceptance checks green and commit. Do not rewrite history."
                    )
                ], attempt=False)
                return
            # Persist acceptance before cleanup so owner rollback remains available.
            self.state.set_task(task["id"], status="done", merge_commit=commit,
                                result=f"{outcome.summary} (merged as {commit})")
            self.state.set_agent(worker_id, state="done")
            try:
                error = arbiter.mirror(repo, project["mirror"], branch)
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                error = str(exc)
            if error:
                self.state.incident("mirror_push", f"{task['id']}: {error}")
            try:
                arbiter.remove_worktree(repo, cwd)
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                self.state.incident("worktree_cleanup", f"{task['id']}: {exc}")
            log.info("task %s accepted and merged as %s", task["id"], commit)
        elif verdict == "reject":
            self._block(task, agent, f"critic rejected the approach: {outcome.summary}")
        else:
            self._rework(agent, task, outcome.findings or [outcome.summary])

    # --- helpers ----------------------------------------------------------
    def _rework(self, agent, task, findings: list[str], *, attempt: bool = True) -> None:
        worker_id = f"worker-{task['id']}"
        attempts = task["attempts"] + (1 if attempt else 0)
        self.state.send(protocol.REVIEW_VERDICT, "arbiter", worker_id,
                        {"verdict": "rework", "summary": "acceptance not met",
                         "findings": findings}, task_id=task["id"])
        self.state.set_task(task["id"], status="in_progress", attempts=attempts)
        if attempts >= self.cfg.max_attempts:
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
    def _escalate_unanswered_questions(self) -> None:
        agents = self.state.q(
            "SELECT a.* FROM agent a WHERE a.state='blocked' AND a.updated_at < ?"
            " AND NOT EXISTS (SELECT 1 FROM task t WHERE t.id=a.task_id"
            " AND t.status='cancelled')"
            " AND EXISTS (SELECT 1 FROM message q WHERE q.sender=a.id AND q.kind=?"
            " AND NOT EXISTS (SELECT 1 FROM message r WHERE r.in_reply_to=q.id AND r.kind=?))",
            (time.time() - self.cfg.ask_timeout_s, protocol.QUESTION, protocol.ANSWER),
        )
        for agent in agents:
            detail = f"{agent['id']}: blocked on an unanswered question (task {agent['task_id']})"
            # Keep deduplication in the database across scheduler restarts and
            # incident resolution: the timeout is reported once per agent.
            # Recheck eligibility in the insert itself: cancellation may have
            # committed since the candidate query above.
            inserted = self.state.x(
                "INSERT INTO incident(kind,detail,created_at)"
                " SELECT 'ask_timeout', ?, ? FROM agent a"
                " WHERE a.id=? AND a.state='blocked' AND a.updated_at < ?"
                " AND NOT EXISTS (SELECT 1 FROM task t WHERE t.id=a.task_id"
                " AND t.status='cancelled')"
                " AND EXISTS (SELECT 1 FROM message q WHERE q.sender=a.id AND q.kind=?"
                " AND NOT EXISTS (SELECT 1 FROM message r"
                " WHERE r.in_reply_to=q.id AND r.kind=?))"
                " AND NOT EXISTS (SELECT 1 FROM incident"
                " WHERE kind='ask_timeout' AND detail=?)",
                (detail, time.time(), agent["id"], time.time() - self.cfg.ask_timeout_s,
                 protocol.QUESTION, protocol.ANSWER, detail),
            ).rowcount
            if inserted:
                log.warning("%s", detail)

    def run(self, max_turns: int = 0) -> bool:
        # `nc run` exit status describes host readiness/scheduler operation,
        # not an individual child CLI exit or agent outcome.  Each child run
        # records its own evidence; a temporary provider failure returns from
        # this timer invocation so the normal five-minute timer retries it.
        ok, detail = self.readiness()
        if not ok:
            self._host_environment_incident(detail)
            log.error("host environment readiness failed: %s", detail)
            return False
        log.info("host environment readiness ok: %s", detail)

        turns = 0
        # A Scheduler object can serve more than one timer invocation in tests
        # and embedded callers.  The probe bound is per invocation, not for
        # the lifetime of that object.
        self._preflight_pairs.clear()
        self._in_run = True
        try:
            while max_turns == 0 or turns < max_turns:
                if (self.cfg.home / "STOP").exists():
                    log.info("stop file present, exiting")
                    return True
                self._escalate_unanswered_questions()
                result = self.step()
                if result == "deferred":
                    log.info("temporary provider failure deferred to next timer invocation")
                    return True
                if result == "preflight_failed":
                    return False
                if result == "idle":
                    log.info("no runnable agents; exiting (idle is a valid outcome)")
                    return True
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
                    return True
                time.sleep(1)
            return True
        finally:
            self._in_run = False
