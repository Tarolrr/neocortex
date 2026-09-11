"""Running a single agent turn: assemble brief, run one CLI session, read outcome."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path

from . import arbiter, protocol, roles
from .adapters import (
    Adapter,
    HostAssessment,
    adapter_ownership,
    assess_session,
    sanitize_diagnostic,
)
from .config import Config
from .proposals import check_proposal
from .state import State


def _decoded_payload(payload: str) -> object:
    """Decode durable message JSON for a human brief without changing storage."""
    return json.loads(payload)


def _payload_for_brief(payload: object) -> str:
    """Keep the JSON record and render every string leaf verbatim for agents.

    JSON escaping is useful provenance, but it turns embedded newlines into the
    two-character ``\\n`` sequence.  The verbatim rendering is therefore part
    of the authoritative handoff, not a shortened display preview.
    """
    strings: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    record = json.dumps(payload, ensure_ascii=False)
    if not strings:
        return record
    return f"{record}\nverbatim content:\n" + "\n".join(strings)


def _message_for_brief(msg: sqlite3.Row) -> str:
    payload = _decoded_payload(msg["payload"])
    metadata = dict(msg)
    metadata.pop("payload", None)
    return (f"{msg['kind']} from {msg['sender']} (message #{msg['id']}):\n"
            f"message metadata: {json.dumps(metadata, ensure_ascii=False)}\n"
            f"{_payload_for_brief(payload)}")


def _inbox_lines(state: State, agent_id: str) -> tuple[list[str], list[int]]:
    lines, ids = [], []
    for msg in state.inbox(agent_id):
        payload = _decoded_payload(msg["payload"])
        if msg["kind"] == protocol.ANSWER:
            lines.append(f"answer from {msg['sender']}: {payload.get('answer', '')}")
        elif msg["kind"] == protocol.REVIEW_VERDICT:
            findings = "; ".join(payload.get("findings", []))
            lines.append(
                f"review verdict `{payload.get('verdict')}` — {payload.get('summary', '')}"
                + (f" Findings: {findings}" if findings else "")
            )
        else:
            lines.append(_message_for_brief(msg))
        ids.append(int(msg["id"]))
    return lines, ids


def build_brief(state: State, cfg: Config, agent: sqlite3.Row, cwd: Path, branch: str,
                outcome_path: Path, checks: str = "") -> tuple[str, list[int]]:
    task = state.one("SELECT * FROM task WHERE id=?", (agent["task_id"],))
    project = state.one("SELECT * FROM project WHERE id=?", (agent["project_id"],))
    acceptance = json.loads(task["acceptance"])
    boundaries = json.loads(task["boundaries"])
    inbox, inbox_ids = _inbox_lines(state, agent["id"])
    contract = protocol.OUTCOME_CONTRACT.format(outcome_path=outcome_path)

    common = {
        "project_title": project["title"],
        "cwd": str(cwd),
        "branch": branch,
        "task_id": task["id"],
        "title": task["title"],
        "objective": task["objective"].strip(),
        "acceptance": roles.bullets(acceptance),
        "contract": contract,
    }

    if agent["role"] == "critic":
        repo = Path(project["repo_path"])
        brief = roles.render(
            roles.CRITIC,
            base_branch=arbiter.base_branch(repo),
            checks=checks or "(none)",
            **common,
        )
    else:
        brief = roles.render(
            roles.WORKER,
            boundaries=roles.bullets(boundaries, empty="(none beyond the rules below)"),
            memo_section=roles.memo_section(agent["memo"]),
            inbox_section=roles.inbox_section(inbox),
            **common,
        )
    return brief, inbox_ids


def build_planner_brief(state: State, agent: sqlite3.Row,
                        outcome_path: Path) -> tuple[str, list[int]]:
    project = state.one("SELECT * FROM project WHERE id=?", (agent["project_id"],))
    repo = Path(project["repo_path"])
    layout = arbiter.git(repo, "ls-files")
    messages = state.inbox(agent["id"])
    tasks = []
    for row in state.q(
        "SELECT * FROM task WHERE project_id=? AND status IN ('queued','blocked')"
        " ORDER BY priority, created_at", (project["id"],),
    ):
        task = dict(row)
        reasons = []
        deps = state.unmet_dependencies(row["id"])
        if deps:
            reasons.append("Waiting for accepted dependencies: " + ", ".join(deps))
        for msg in state.q(
            "SELECT * FROM message m WHERE task_id=? AND (kind=? OR (kind=?"
            " AND NOT EXISTS (SELECT 1 FROM message r WHERE r.in_reply_to=m.id"
            " AND r.kind=?))) ORDER BY id",
            (row["id"], protocol.INCIDENT, protocol.QUESTION, protocol.ANSWER),
        ):
            reasons.append(json.loads(msg["payload"]))
        task["blocked_reasons"] = reasons or [
            "No recorded blocker" if row["status"] == "queued" else "Reason not recorded"
        ]
        tasks.append(task)
    accepted = [dict(row) for row in state.q(
        "SELECT * FROM task WHERE project_id=? AND status='done'"
        " ORDER BY updated_at DESC LIMIT 10", (project["id"],),
    )]
    revision = state.pending_revision(agent["id"])
    feedback = "\n\n".join(_message_for_brief(message) for message in messages)
    revision_data = dict(revision) if revision else None
    revision_text = json.dumps(revision_data, ensure_ascii=False)
    if revision_data is not None:
        revision_text += "\nverbatim feedback content:\n" + _payload_for_brief(
            _decoded_payload(revision_data["payload"])
        )
    return roles.render(
        roles.PLANNER, project_id=project["id"], repo=str(repo), layout=layout,
        revision=revision_text,
        feedback=feedback,
        tasks=json.dumps(tasks, ensure_ascii=False),
        accepted=json.dumps(accepted, ensure_ascii=False),
        memo_section=roles.memo_section(agent["memo"]), outcome_path=str(outcome_path),
    ), [m["id"] for m in messages]


def _planner_specs(outcome: protocol.Outcome, project_id: str) -> list[dict]:
    specs = outcome.raw.get("proposal")
    if not isinstance(specs, list) or not 1 <= len(specs) <= 5:
        raise ValueError("planner proposal must contain between one and five tasks")
    for spec in specs:
        if not isinstance(spec, dict) or spec.get("project") != project_id:
            raise ValueError("each proposed task must belong to the planner project")
        for key in ("title", "objective"):
            if not isinstance(spec.get(key), str) or not spec[key].strip():
                raise ValueError(f"proposed task requires a nonempty {key}")
        for key in ("acceptance", "boundaries"):
            value = spec.get(key)
            if not isinstance(value, list) or not value or any(
                not isinstance(item, str) or not item.strip() for item in value
            ):
                raise ValueError(f"proposed task requires nonempty {key} strings")
        deps = spec.get("depends_on", [])
        if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps):
            raise ValueError("depends_on must be a list of task IDs")
        if "id" in spec and not isinstance(spec["id"], str):
            raise ValueError("proposal-local id must be a string")
    for finding in check_proposal(specs, set()):
        if "boundary names a path instead of an invariant" in finding:
            raise ValueError(finding)
    return specs


def _defer_until(diagnostic: str) -> float | None:
    """Return a provider-supplied, bounded reset epoch, if one is present.

    This is deliberately shared by terminal SessionResults and typed launch
    exceptions: in both cases the adapter has classified the provider event.
    """
    match = re.search(r"(?:retry|reset)[ _-]?(?:at|until)[:= ]+(1[0-9]{9}(?:\.[0-9]+)?)",
                      diagnostic, re.IGNORECASE)
    if match:
        until = float(match.group(1))
        if time.time() < until <= time.time() + 7 * 86400:
            return until
    return None


def _set_defer_until(state: State, run_id: int, diagnostic: str) -> None:
    until = _defer_until(diagnostic)
    if until is not None:
        state.x("UPDATE run SET defer_until=? WHERE id=?", (until, run_id))


def _record_host(state: State, run_id: int, result, assessment: HostAssessment) -> None:
    state.record_host_assessment(
        run_id, exit_code=result.exit_code, timed_out=result.timed_out,
        category=assessment.category, diagnostic=assessment.diagnostic,
        assessment=assessment.status,
    )
    _set_defer_until(state, run_id, assessment.diagnostic)


def _record_exception_host(state: State, run_id: int,
                           assessment: HostAssessment | None, exc: Exception) -> None:
    """Persist a pre-result launcher failure, including typed reset evidence."""
    diagnostic = assessment.diagnostic if assessment else sanitize_diagnostic(str(exc))
    state.record_host_assessment(
        run_id, exit_code=None, timed_out=None,
        category=assessment.category if assessment else "local_error",
        diagnostic=diagnostic, assessment="FAILED",
    )
    if assessment is not None:
        _set_defer_until(state, run_id, diagnostic)


def _record_outcome_read_failure(state: State, run_id: int, result,
                                 exc: Exception) -> None:
    """Replace a completed-session assessment when its local output is unreadable.

    A SessionResult is still useful evidence (in particular its exit status and
    timeout observation), but an OSError while reading the agent-owned outcome
    is a host filesystem failure, not a successful host session.
    """
    state.record_host_assessment(
        run_id, exit_code=result.exit_code, timed_out=result.timed_out,
        category="local_error", diagnostic=sanitize_diagnostic(str(exc)),
        assessment="FAILED",
    )


def _host_failure(role: str, assessment: HostAssessment) -> protocol.Outcome:
    detail = f" ({assessment.diagnostic})" if assessment.diagnostic else ""
    return protocol.Outcome(
        kind=protocol.FAIL,
        summary=f"{role} host session failed: {assessment.category}{detail}",
        # This is deliberately host-owned metadata, not an agent outcome.
        host_deferred=assessment.category in {
            "subscription_limit", "throttled", "overloaded", "transient",
        },
    )


def _no_outcome(role: str, exc: Exception) -> protocol.Outcome:
    """Represent host-side absence of an agent result without forging FAIL.

    FAIL is an agent-authored, successfully parsed outcome.  A launcher or
    filesystem exception has no such decision, even when the session managed
    to write a valid-looking file before the host failed to read it.
    """
    return protocol.Outcome(kind=protocol.NO_OUTCOME,
                            summary=f"{role} outcome unavailable: {exc}")


def _exception_assessment(exc: Exception) -> HostAssessment | None:
    """Accept only adapter-supplied, typed provider evidence from launch errors."""
    category = getattr(exc, "terminal_category", None)
    if category not in {"subscription_limit", "throttled", "overloaded", "transient"}:
        return None
    return HostAssessment("FAILED", category, sanitize_diagnostic(str(exc)))


def run_planner_turn(state: State, cfg: Config, agent: sqlite3.Row,
                     adapter: Adapter) -> protocol.Outcome:
    """Run a project session; only the host records the pending proposal."""
    run_dir = cfg.runs_dir / f"{agent['id']}_{time.time_ns()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    outcome_path = run_dir / "outcome.json"
    log_path = run_dir / "session.log"
    revision = state.pending_revision(agent["id"])
    brief, inbox_ids = build_planner_brief(state, agent, outcome_path)
    (run_dir / "brief.md").write_text(brief)
    model = cfg.model_for("planner")
    run_id = state.start_run(agent["id"], None, "planner", model, str(log_path))
    state.x("UPDATE project SET planner_last_ran_at=?, planner_skip_reason=NULL WHERE id=?",
            (time.time(), agent["project_id"]))
    tokens = None
    result_recorded = False
    outcome_read = False
    host_failure: protocol.Outcome | None = None
    assessment: HostAssessment | None = None
    try:
        # Advisory roles must have an adapter-specific restricted launch.
        # Do not alias a missing method to ``run``: supported adapters use an
        # unrestricted worker policy there.
        run_session = adapter.run_planner
        with adapter_ownership(lambda pid: state.record_adapter_owner(run_id, pid)):
            result = run_session(brief, run_dir, model, log_path, cfg.turn_timeout_s)
        tokens = result.tokens
        assessment = assess_session(result, adapter.name)
        _record_host(state, run_id, result, assessment)
        result_recorded = True
        outcome = protocol.read_outcome(outcome_path)
        outcome_read = True
        if assessment.failed:
            # Keep parsed outcome in the run for diagnosis, but never let it
            # create a proposal/question or consume planner context.
            host_failure = _host_failure("Planner", assessment)
    except Exception as exc:
        logging.getLogger(__name__).exception("Planner session failed")
        # A terminal provider assessment is stronger evidence than a missing
        # agent-owned outcome file.  In particular, do not relabel a
        # throttled/overloaded session as a local filesystem error merely
        # because the provider stopped before writing its outcome.
        saved_failure = (_host_failure("Planner", assessment)
                         if result_recorded and assessment is not None and assessment.failed
                         and assessment.category in {
                             "subscription_limit", "throttled", "overloaded", "transient",
                         }
                         else None)
        exceptional = _exception_assessment(exc)
        outcome = (_no_outcome("Planner", exc) if saved_failure is not None else
                   _host_failure("Planner", exceptional) if exceptional else
                   _no_outcome("Planner", exc))
        if saved_failure is not None:
            host_failure = saved_failure
        elif exceptional:
            host_failure = outcome
        if result_recorded and not outcome_read and saved_failure is None:
            _record_outcome_read_failure(state, run_id, result, exc)
        elif not result_recorded:
            # The adapter raised before yielding a SessionResult, so timeout
            # status was never observed.  Keep that evidence explicitly unknown.
            _record_exception_host(state, run_id, exceptional, exc)
    try:
        if host_failure is None and outcome.kind == protocol.DONE:
            specs = _planner_specs(outcome, agent["project_id"])
            state.add_proposal(agent["project_id"], agent["id"], outcome.summary, specs,
                               revision["original_id"] if revision else None)
        elif host_failure is None and outcome.kind == protocol.ASK:
            if outcome.to != "owner" or not outcome.question.strip():
                raise ValueError("planner ASK requires a question addressed to owner")
            state.send(protocol.QUESTION, agent["id"], "owner",
                       {"question": outcome.question, "summary": outcome.summary})
        elif host_failure is None and outcome.kind == protocol.YIELD:
            raise ValueError("planner must record one proposal or ask the owner a question")
    except (ValueError, TypeError) as exc:
        outcome = protocol.Outcome(kind=protocol.FAIL, summary=f"Planner protocol failure: {exc}")
    if host_failure is None and outcome.kind in (protocol.DONE, protocol.ASK):
        state.mark_delivered(inbox_ids)
    state.end_run(run_id, outcome.kind, outcome.summary, tokens)
    # Do not erase a wake arriving while this session was running.
    deferred = bool(host_failure and host_failure.deferred)
    state.x(
        "UPDATE agent SET turns=turns+?, memo=?,"
        " state=CASE WHEN updated_at=? THEN ? ELSE state END WHERE id=?",
        # A parsed file from a failed host session is diagnostic evidence only;
        # it must not replace the planner context needed by a retry.
        (0 if deferred else 1,
         agent["memo"] if host_failure is not None else outcome.memo or agent["memo"],
         agent["updated_at"],
         "runnable" if deferred else "blocked" if host_failure is not None else
         ("runnable" if state.pending_revision(agent["id"]) else "done")
         if outcome.kind == protocol.DONE else "blocked", agent["id"]),
    )
    return host_failure or outcome


def run_turn(state: State, cfg: Config, adapter: Adapter, agent: sqlite3.Row,
             cwd: Path, branch: str, checks: str = "") -> protocol.Outcome:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_dir = cfg.runs_dir / f"{agent['id']}_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    outcome_path = run_dir / "outcome.json"
    log_path = run_dir / "session.log"

    brief, inbox_ids = build_brief(state, cfg, agent, cwd, branch, outcome_path, checks)
    (run_dir / "brief.md").write_text(brief)

    model = agent["model"]
    run_id = state.start_run(agent["id"], agent["task_id"], agent["role"], model, str(log_path))
    tokens = None
    result_recorded = False
    outcome_read = False
    assessment: HostAssessment | None = None
    try:
        with adapter_ownership(lambda pid: state.record_adapter_owner(run_id, pid)):
            result = adapter.run(brief, cwd, model, log_path, cfg.turn_timeout_s)
        tokens = result.tokens
        assessment = assess_session(result, adapter.name)
        _record_host(state, run_id, result, assessment)
        result_recorded = True
        outcome = protocol.read_outcome(outcome_path)
        outcome_read = True
        if assessment.failed:
            # Persist what the agent wrote separately, then return a host
            # failure so scheduler handlers cannot apply it.
            state.end_run(run_id, outcome.kind, outcome.summary, tokens)
            failure = _host_failure(agent["role"], assessment)
            # Provider deferral is not an agent turn.  The run remains the
            # durable evidence, while memo/inbox/eligibility are untouched.
            if not failure.deferred:
                state.set_agent(agent["id"], turns=agent["turns"] + 1,
                                memo=agent["memo"])
            return failure
    except Exception as exc:
        logging.getLogger(__name__).exception("%s session failed", agent["role"])
        saved_failure = (_host_failure(agent["role"], assessment)
                         if result_recorded and assessment is not None and assessment.failed
                         and assessment.category in {
                             "subscription_limit", "throttled", "overloaded", "transient",
                         }
                         else None)
        exceptional = _exception_assessment(exc)
        outcome = (_no_outcome(agent["role"], exc) if saved_failure is not None else
                   _host_failure(agent["role"], exceptional) if exceptional else
                   _no_outcome(agent["role"], exc))
        if saved_failure is not None and saved_failure.deferred:
            # Keep the no-outcome run record, but defer using the provider
            # evidence already persisted above.  This must not consume a turn
            # or acknowledge inbox feedback.
            state.end_run(run_id, outcome.kind, outcome.summary, tokens)
            return saved_failure
        if result_recorded and not outcome_read and saved_failure is None:
            _record_outcome_read_failure(state, run_id, result, exc)
        elif not result_recorded:
            # No SessionResult was returned: do not invent a completed timeout state.
            _record_exception_host(state, run_id, exceptional, exc)
    # Session exceptions are evidence too.  Do not deliver inbox messages: a
    # retry must retain feedback/questions that were never successfully used.
    state.end_run(run_id, outcome.kind, outcome.summary, tokens)
    if outcome.kind in (protocol.DONE, protocol.ASK, protocol.YIELD):
        state.mark_delivered(inbox_ids)
    state.set_agent(agent["id"], turns=agent["turns"] + (0 if outcome.deferred else 1),
                    memo=outcome.memo or agent["memo"])
    return outcome


def build_plan_critic_brief(state: State, proposal: sqlite3.Row,
                            outcome_path: Path) -> str:
    project = state.one("SELECT * FROM project WHERE id=?", (proposal["project_id"],))
    repo = Path(project["repo_path"])
    # Explicit field selection keeps planner rationale, memos and messages out.
    tasks = [dict(row) for row in state.q(
        "SELECT id, title, objective, acceptance, boundaries, depends_on, status"
        " FROM task WHERE project_id=? ORDER BY id", (project["id"],),
    )]
    return roles.render(
        roles.PLAN_CRITIC, repo=str(repo), head=arbiter.git(repo, "rev-parse", "HEAD"),
        status=arbiter.git(repo, "status", "--short"), layout=arbiter.git(repo, "ls-files"),
        spec=proposal["spec"], tasks=json.dumps(tasks), outcome_path=str(outcome_path),
    )


def run_plan_critic_turn(state: State, cfg: Config, proposal: sqlite3.Row,
                         adapter: Adapter) -> protocol.Outcome:
    # A review is one logical `(proposal, spec)` item.  Only terminal provider
    # evidence releases its claim; malformed advice retains the old failed
    # policy and completed advice is never replayed.
    # The scheduler selection is only a hint: feedback can supersede a
    # proposal after selection and before this call obtains its review claim.
    # Never revive advice for a nonpending or changed proposal.
    model = cfg.model_for("plan_critic")
    # The ID is stable for the logical review.  Claiming also creates it only
    # once, so a retry adds another run/attempt rather than another agent.
    review_id = state.claim_plan_review(proposal["id"], proposal["spec"], proposal["project_id"],
                                        model)
    if review_id is None:
        return protocol.Outcome(kind=protocol.DONE, summary="Proposal is no longer reviewable")
    agent_id = f"plan-critic-{review_id}"
    run_dir = cfg.runs_dir / f"{agent_id}_{time.time_ns()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "session.log"
    run_id = state.start_run(agent_id, None, "plan_critic", model, str(log_path))
    state.x("INSERT INTO plan_review_attempt(review_id,run_id,status,created_at) VALUES(?,?,?,?)",
            (review_id, run_id, "running", time.time()))
    tokens = None
    result_recorded = False
    outcome_read = False
    assessment: HostAssessment | None = None
    saved_failure: protocol.Outcome | None = None
    exceptional: HostAssessment | None = None
    try:
        outcome_path = run_dir / "outcome.json"
        brief = build_plan_critic_brief(state, proposal, outcome_path)
        (run_dir / "brief.md").write_text(brief)
        # No unrestricted fallback: this role requires the restricted adapter path.
        with adapter_ownership(lambda pid: state.record_adapter_owner(run_id, pid)):
            result = adapter.run_planner(brief, run_dir, model, log_path, cfg.turn_timeout_s)
        tokens = result.tokens
        assessment = assess_session(result, adapter.name)
        _record_host(state, run_id, result, assessment)
        result_recorded = True
        outcome = protocol.read_outcome(outcome_path)
        outcome_read = True
        if assessment.failed:
            failure = _host_failure("Plan review", assessment)
            status = "retryable" if failure.deferred else "failed"
            state.x("UPDATE plan_review SET status=?, recommendation=? WHERE id=?",
                    (status, f"host session failed: {assessment.category}", review_id))
            state.x("UPDATE plan_review_attempt SET status=? WHERE run_id=?",
                    (status, run_id))
            state.end_run(run_id, outcome.kind, outcome.summary, tokens)
            # This advisory agent has completed its one attempt even though
            # the review itself must remain failed and unapplied.
            state.set_agent(agent_id, state="done", turns=0 if failure.deferred else 1)
            return failure
        recommendation = outcome.raw.get("recommendation")
        if outcome.kind != protocol.DONE or not isinstance(recommendation, str):
            raise ValueError("plan critic requires DONE with a recommendation")
        findings = outcome.raw.get("findings")
        if not isinstance(findings, list) or any(not isinstance(f, str) for f in findings):
            raise ValueError("plan critic requires a list of findings")
        if not state.complete_plan_review(review_id, proposal["id"], proposal["spec"], run_id,
                                          json.dumps(findings), recommendation):
            outcome = protocol.Outcome(kind=protocol.DONE,
                                       summary="Proposal changed while review was running")
    except Exception as exc:
        logging.getLogger(__name__).exception("Plan review %s failed", review_id)
        saved_failure = (_host_failure("Plan review", assessment)
                         if result_recorded and assessment is not None and assessment.failed
                         and assessment.category in {
                             "subscription_limit", "throttled", "overloaded", "transient",
                         }
                         else None)
        exceptional = _exception_assessment(exc)
        # Validation happens after a parsed agent file; preserve it as a
        # protocol FAIL rather than misreporting an absent host outcome.
        outcome = (_no_outcome("Plan review", exc) if saved_failure is not None else
                   _host_failure("Plan review", exceptional) if exceptional else protocol.Outcome(kind=protocol.FAIL,
                                    summary=f"Plan review protocol failure: {exc}")
                   if outcome_read else _no_outcome("Plan review", exc))
        if result_recorded and not outcome_read and saved_failure is None:
            _record_outcome_read_failure(state, run_id, result, exc)
        elif not result_recorded:
            # A launcher failure has no timeout observation.
            _record_exception_host(state, run_id, exceptional, exc)
        status = "retryable" if (saved_failure and saved_failure.deferred) or exceptional else "failed"
        state.x("UPDATE plan_review SET status=?, recommendation=? WHERE id=?",
                (status, (saved_failure or outcome).summary, review_id))
        state.x("UPDATE plan_review_attempt SET status=? WHERE run_id=?", (status, run_id))
    state.end_run(run_id, outcome.kind, outcome.summary, tokens)
    # A typed launch exception is the same temporary provider evidence as a
    # terminal SessionResult failure.  It has no saved SessionResult, but it
    # still represents an interrupted host attempt rather than advice from
    # this stable logical reviewer, so it must not spend its turn budget.
    deferred = bool((saved_failure and saved_failure.deferred) or exceptional)
    state.set_agent(agent_id, state="done", turns=0 if deferred else 1)
    return saved_failure or outcome
