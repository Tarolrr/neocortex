"""Running a single agent turn: assemble brief, run one CLI session, read outcome."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from . import arbiter, protocol, roles
from .adapters import Adapter
from .config import Config
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
