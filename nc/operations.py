"""Structured owner read/write models shared by the CLI and the browser UI.

Both `nc.cli` and `nc.ui` call into this module instead of duplicating SQL or
parsing each other's output. Every function here takes a `State` (and an
`arbiter`/`Config` where repository or filesystem coordination is needed) and
returns plain dicts/lists or raises one of two errors so callers can format
them however they like:

- `LookupError` for "no such id" (renders as 404 in the UI, exit 1 in the CLI)
- `ValueError` for a rejected but well-formed request (400 in the UI, exit 1
  in the CLI)

Nothing here shells out or re-parses `nc` output; it calls `State` methods and
`arbiter` helpers directly, the same as any other in-process caller.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import arbiter
from .config import Config
from .state import State


def age(ts: float) -> str:
    """Human-friendly age used by both the CLI inbox and the UI inbox page."""
    delta = int(time.time() - ts)
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


# --- projects -------------------------------------------------------------

def projects(state: State) -> list[dict]:
    return [dict(row) for row in state.q("SELECT * FROM project ORDER BY id")]


def get_project(state: State, project_id: str) -> dict:
    row = state.one("SELECT * FROM project WHERE id=?", (project_id,))
    if row is None:
        raise LookupError(f"unknown project: {project_id}")
    return dict(row)


# --- tasks ------------------------------------------------------------------

def tasks(state: State, project: str | None = None, include_cancelled: bool = False) -> list[dict]:
    sql = "SELECT * FROM task WHERE 1=1"
    params: tuple = ()
    if project:
        sql += " AND project_id=?"
        params = (project,)
    if not include_cancelled:
        sql += " AND status != 'cancelled'"
    rows = [dict(row) for row in state.q(sql + " ORDER BY priority, created_at", params)]
    for row in rows:
        row["unmet_dependencies"] = state.unmet_dependencies(row["id"])
    return rows


def create_task(state: State, project_id: str, title: str, objective: str,
                acceptance: list[str], boundaries: list[str] | None = None,
                priority: int = 100, budget_turns: int = 6,
                depends_on: list[str] | None = None) -> str:
    if not project_id:
        raise ValueError("project is required")
    if get_project(state, project_id) is None:  # pragma: no cover - get_project raises first
        raise LookupError(f"unknown project: {project_id}")
    if not title or not title.strip():
        raise ValueError("title is required")
    if not objective or not objective.strip():
        raise ValueError("objective is required")
    return state.add_task(project_id, title.strip(), objective, acceptance,
                          boundaries, priority, budget_turns, depends_on)


def import_tasks(state: State, specs: list[dict] | dict) -> list[str]:
    """Same normalization as `nc task --file`: a bare object is one task."""
    items = specs if isinstance(specs, list) else [specs]
    return [state.add_task_spec(spec) for spec in items]


def task_detail(state: State, cfg: Config, task_id: str) -> dict:
    """Everything `nc why` prints, as data: task, dependants, runs, messages, check output."""
    task = state.one("SELECT * FROM task WHERE id=?", (task_id,))
    if task is None:
        raise LookupError(f"unknown task: {task_id}")
    detail = dict(task)
    detail["depends_on"] = json.loads(task["depends_on"] or "[]")
    detail["unmet_dependencies"] = state.unmet_dependencies(task_id)
    detail["cancelled_dependencies"] = [
        dep for dep in detail["depends_on"]
        if (row := state.one("SELECT status FROM task WHERE id=?", (dep,))) is not None
        and row["status"] == "cancelled"
    ]
    detail["acceptance"] = json.loads(task["acceptance"])
    detail["boundaries"] = json.loads(task["boundaries"] or "[]")
    detail["runs"] = [dict(r) for r in state.q(
        "SELECT * FROM run WHERE task_id=? ORDER BY started_at, id", (task_id,),
    )]
    detail["messages"] = [dict(m) for m in state.q(
        "SELECT * FROM message WHERE task_id=? ORDER BY id", (task_id,),
    )]
    check_path = cfg.home / "checks" / f"{task_id}.txt"
    try:
        detail["check_output"] = check_path.read_text()
    except FileNotFoundError:
        detail["check_output"] = None
    detail["check_path"] = str(check_path)
    return detail


def cancel_task(state: State, task_id: str, reason: str) -> bool:
    return state.cancel_task(task_id, reason)


def requeue_task(cfg: Config, state: State, task_id: str, fresh: bool = False,
                 budget: int | None = None, reason: str | None = None) -> dict:
    """Put a task back in the queue, optionally discarding its branch and worktree."""
    task = state.one("SELECT * FROM task WHERE id=?", (task_id,))
    if task is None:
        raise LookupError(f"unknown task: {task_id}")
    if task["status"] == "done":
        raise ValueError(f"{task_id} is already accepted; use rollback instead")
    project = get_project(state, task["project_id"])
    repo = Path(project["repo_path"])
    if fresh:
        arbiter.remove_worktree(repo, cfg.work_dir / task["id"])
        arbiter.git(repo, "worktree", "prune", check=False)
        arbiter.git(repo, "branch", "-D", f"nc/{task['id']}", check=False)
    # Agents keep their history but restart with a fresh turn budget.
    state.x("UPDATE agent SET state='blocked', turns=0 WHERE task_id=?", (task["id"],))
    state.x("UPDATE message SET delivered=1 WHERE task_id=? AND recipient='owner'", (task["id"],))
    fields: dict[str, Any] = {"status": "queued", "attempts": 0,
                              "result": reason or "requeued by the owner"}
    if budget:
        fields["budget_turns"] = budget
    state.set_task(task["id"], **fields)
    return {"task_id": task_id, "fresh": fresh, "budget": budget}


def rollback_task(state: State, task_id: str) -> dict:
    task = state.one("SELECT * FROM task WHERE id=?", (task_id,))
    if task is None or not task["merge_commit"]:
        raise LookupError(f"{task_id} has no recorded merge commit")
    project = get_project(state, task["project_id"])
    repo = Path(project["repo_path"])
    commit = arbiter.revert(repo, task["merge_commit"])
    mirror_error = arbiter.mirror(repo, project["mirror"])
    state.set_task(task_id, status="blocked", result=f"reverted by the owner in {commit}")
    state.incident("rollback", f"{task_id} reverted in {commit}")
    return {"task_id": task_id, "reverted_commit": task["merge_commit"], "commit": commit,
            "mirror_error": mirror_error}


# --- proposals ----------------------------------------------------------

def proposals(state: State) -> list[dict]:
    rows = []
    for row in state.q("SELECT * FROM proposal ORDER BY id"):
        detail = dict(row)
        detail["spec"] = json.loads(detail["spec"])
        detail["findings"] = json.loads(detail["findings"])
        rows.append(detail)
    return rows


def proposal_detail(state: State, proposal_id: int) -> dict:
    row = state.one("SELECT * FROM proposal WHERE id=?", (proposal_id,))
    if row is None:
        raise LookupError(f"unknown proposal: {proposal_id}")
    detail = dict(row)
    detail["spec"] = json.loads(detail["spec"])
    detail["findings"] = json.loads(detail["findings"])
    review = state.one(
        "SELECT status, findings, recommendation FROM plan_review"
        " WHERE proposal_id=? AND spec=?", (row["id"], row["spec"]),
    )
    detail["revisions"] = [
        {**dict(r), "feedback": json.loads(r["feedback"])}
        for r in state.q(
            "SELECT r.*, m.payload AS feedback FROM proposal_revision r"
            " JOIN message m ON m.id=r.feedback_id"
            " WHERE r.original_id=? OR r.replacement_id=? ORDER BY r.original_id",
            (row["id"], row["id"]),
        )
    ]
    detail["plan_review"] = dict(review) if review else None
    if review:
        detail["plan_review"]["findings"] = json.loads(review["findings"])
    return detail


def approve_proposal(state: State, proposal_id: int, force: bool = False) -> dict:
    ids = state.approve_proposal(proposal_id, force=force)
    row = state.one("SELECT findings FROM proposal WHERE id=?", (proposal_id,))
    return {"task_ids": ids, "overridden_findings": json.loads(row["findings"])}


def reject_proposal(state: State, proposal_id: int, reason: str) -> None:
    state.reject_proposal(proposal_id, reason)


# --- feedback / planning --------------------------------------------------

def submit_feedback(state: State, cfg: Config, project: str | None, text: str,
                    task: str | None = None, proposal: int | None = None) -> tuple[str, int]:
    if not text or not text.strip():
        raise ValueError("feedback text is required")
    return state.planner_feedback(project, text, cfg.model_for("planner"), task, proposal)


# --- inbox / answers -------------------------------------------------------

def inbox(state: State, include_delivered: bool = False) -> list[dict]:
    rows = []
    for row in state.inbox("owner", undelivered_only=not include_delivered):
        item = dict(row)
        payload = json.loads(item["payload"])
        item["text"] = payload.get("question") or payload.get("reason") or json.dumps(payload)
        rows.append(item)
    return rows


def answer_message(state: State, message_id: int, text: str) -> dict:
    if not text or not text.strip():
        raise ValueError("an answer is required")
    with state.db:
        state.db.execute("BEGIN IMMEDIATE")
        question = state.one("SELECT * FROM message WHERE id=?", (message_id,))
        if question is None:
            raise LookupError(f"no message #{message_id}")
        if state.one(
            "SELECT 1 FROM task WHERE status='cancelled' AND"
            " (id=? OR id=(SELECT task_id FROM agent WHERE id=?))",
            (question["task_id"], question["sender"]),
        ):
            raise ValueError("task is cancelled; use requeue explicitly to restore it")
        agent_id = question["sender"]
        now = time.time()
        from . import protocol
        state.db.execute(
            "INSERT INTO message(kind,sender,recipient,payload,task_id,in_reply_to,created_at)"
            " VALUES(?,'owner',?,?,?,?,?)",
            (protocol.ANSWER, agent_id, json.dumps({"answer": text}),
             question["task_id"], question["id"], now),
        )
        state.db.execute("UPDATE message SET delivered=1 WHERE id=?", (question["id"],))
        state.db.execute("UPDATE agent SET state='runnable', updated_at=? WHERE id=?",
                         (now, agent_id))
        if question["task_id"]:
            state.db.execute("UPDATE task SET status='in_progress', updated_at=? WHERE id=?",
                             (now, question["task_id"]))
    return {"agent_id": agent_id, "message_id": message_id}
