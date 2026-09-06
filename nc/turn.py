"""Running a single agent turn: assemble brief, run one CLI session, read outcome."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from . import arbiter, protocol, roles
from .adapters import Adapter
from .config import Config
from .proposals import check_proposal
from .state import State


def _inbox_lines(state: State, agent_id: str) -> tuple[list[str], list[int]]:
    lines, ids = [], []
    for msg in state.inbox(agent_id):
        payload = json.loads(msg["payload"])
        if msg["kind"] == protocol.ANSWER:
            lines.append(f"answer from {msg['sender']}: {payload.get('answer', '')}")
        elif msg["kind"] == protocol.REVIEW_VERDICT:
            findings = "; ".join(payload.get("findings", []))
            lines.append(
                f"review verdict `{payload.get('verdict')}` — {payload.get('summary', '')}"
                + (f" Findings: {findings}" if findings else "")
            )
        else:
            lines.append(f"{msg['kind']} from {msg['sender']}: "
                         f"{json.dumps(payload, ensure_ascii=False)[:800]}")
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
    return roles.render(
        roles.PLANNER, project_id=project["id"], repo=str(repo), layout=layout,
        feedback=json.dumps([dict(m) for m in messages], ensure_ascii=False),
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


def run_planner_turn(state: State, cfg: Config, agent: sqlite3.Row,
                     adapter: Adapter) -> protocol.Outcome:
    """Run a project session; only the host records the pending proposal."""
    run_dir = cfg.runs_dir / f"{agent['id']}_{time.time_ns()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    outcome_path = run_dir / "outcome.json"
    log_path = run_dir / "session.log"
    brief, inbox_ids = build_planner_brief(state, agent, outcome_path)
    (run_dir / "brief.md").write_text(brief)
    model = cfg.model_for("planner")
    run_id = state.start_run(agent["id"], None, "planner", model, str(log_path))
    run_session = getattr(adapter, "run_planner", adapter.run)
    result = run_session(brief, run_dir, model, log_path, cfg.turn_timeout_s)
    outcome = protocol.read_outcome(outcome_path)
    try:
        if outcome.kind == protocol.DONE:
            specs = _planner_specs(outcome, agent["project_id"])
            state.add_proposal(agent["project_id"], agent["id"], outcome.summary, specs)
        elif outcome.kind == protocol.ASK:
            if outcome.to != "owner" or not outcome.question.strip():
                raise ValueError("planner ASK requires a question addressed to owner")
            state.send(protocol.QUESTION, agent["id"], "owner",
                       {"question": outcome.question, "summary": outcome.summary})
        elif outcome.kind == protocol.YIELD:
            raise ValueError("planner must record one proposal or ask the owner a question")
    except (ValueError, TypeError) as exc:
        outcome = protocol.Outcome(kind=protocol.FAIL, summary=f"Planner protocol failure: {exc}")
    if outcome.kind in (protocol.DONE, protocol.ASK):
        state.mark_delivered(inbox_ids)
    state.end_run(run_id, outcome.kind, outcome.summary, result.tokens)
    # Do not erase a wake arriving while this session was running.
    state.x(
        "UPDATE agent SET turns=turns+1, memo=?,"
        " state=CASE WHEN updated_at=? THEN ? ELSE state END WHERE id=?",
        (outcome.memo or agent["memo"], agent["updated_at"],
         "done" if outcome.kind == protocol.DONE else "blocked", agent["id"]),
    )
    return outcome


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
    result = adapter.run(brief, cwd, model, log_path, cfg.turn_timeout_s)
    outcome = protocol.read_outcome(outcome_path)

    if outcome.kind == protocol.NO_OUTCOME and result.timed_out:
        outcome.summary = f"turn timed out after {cfg.turn_timeout_s}s without an outcome file"

    state.end_run(run_id, outcome.kind, outcome.summary, result.tokens)
    state.mark_delivered(inbox_ids)
    state.set_agent(agent["id"], turns=agent["turns"] + 1,
                    memo=outcome.memo or agent["memo"])
    return outcome
