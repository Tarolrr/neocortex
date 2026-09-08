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

import hashlib
import json
import os
import stat
import subprocess
import time
from pathlib import Path

from . import arbiter, protocol
from .config import Config
from .lifecycle import lifecycle_lock, repository_lock
from .state import State


def age(ts: float) -> str:
    """Human-friendly age used by both the CLI inbox and the UI inbox page."""
    delta = int(time.time() - ts)
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


def _owner_status(row: dict) -> str:
    """Return evidence, not a guess based on task/service state."""
    pid, started = row.get("owner_pid"), row.get("owner_start")
    if pid is None or not started:
        return "uncertain (legacy ownership evidence is absent)"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        owner_gone = "owner process is gone"
    except PermissionError:
        return "uncertain (owner process cannot be inspected)"
    else:
        current = State._process_start(int(pid))
        if current is None:
            return "uncertain (owner process identity cannot be inspected)"
        if current == started:
            return "live scheduler owner process"
        owner_gone = "owner PID was reused; recorded owner is gone"
    cgroup = row.get("adapter_cgroup")
    if row.get("ownership_version", 0) >= 2:
        if not cgroup:
            return "uncertain (adapter cgroup ownership evidence is absent; " + owner_gone + ")"
        members = _adapter_cgroup_members(str(cgroup))
        if members is None:
            return "uncertain (adapter cgroup cannot be inspected)"
        if members:
            return ("live adapter process or descendant "
                    f"(cgroup {cgroup}: {', '.join(map(str, members))})")
        return "scheduler and recorded adapter cgroup are gone"
    pgid = row.get("adapter_pgid")
    if pgid is None:
        # A scheduler exit does not prove that its separately-sessioned adapter
        # exited. Rows from before adapter ownership recording need the
        # documented, explicit quiescence acknowledgement.
        return "uncertain (adapter ownership evidence is absent; " + owner_gone + ")"
    members = _adapter_group_members(int(pgid))
    if members is None:
        return "uncertain (adapter process group cannot be inspected)"
    if members:
        return f"live adapter process or descendant (pgrp {pgid}: {', '.join(map(str, members))})"
    return "scheduler and recorded adapter process group are gone"


def _adapter_cgroup_members(cgroup: str) -> list[int] | None:
    """Read a dedicated adapter cgroup; empty is the only absence evidence."""
    if not cgroup.startswith("/") or Path(cgroup).name.startswith("neocortex-run-") is False:
        return None
    try:
        pids = (Path("/sys/fs/cgroup") / cgroup.lstrip("/") / "cgroup.procs").read_text().split()
        members = []
        for text_pid in pids:
            pid = int(text_pid)
            try:
                # A zombie is still briefly listed by cgroup v2, but cannot
                # execute or own adapter work.  Its eventual reaping is not a
                # reason to block a safe recovery indefinitely.
                state = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[0]
            except FileNotFoundError:
                continue
            except (PermissionError, OSError, IndexError):
                return None
            if state != "Z":
                members.append(pid)
        return sorted(members)
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        # A removed empty cgroup is absence; failures inspecting an extant one
        # are uncertainty.  cgroup directories are removed only once empty.
        path = Path("/sys/fs/cgroup") / cgroup.lstrip("/")
        try:
            return [] if not path.exists() else None
        except OSError:
            return None


def _adapter_group_members(pgid: int) -> list[int] | None:
    """Return live members of an adapter's isolated POSIX process group.

    ``start_new_session`` makes the adapter PID its group leader.  Its children
    normally inherit that group, so this catches adapter descendants after a
    scheduler crash.  An uninspectable /proc is uncertainty, never absence.
    """
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    members: list[int] = []
    try:
        entries = list(proc.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            bits = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
            if int(bits[2]) == pgid:  # field 5 / pgrp
                members.append(int(entry.name))
        except FileNotFoundError:
            # A process exiting during the scan cannot remain a live member.
            continue
        except (PermissionError, OSError):
            # Denied or failed inspection makes absence unknowable.
            return None
        except (IndexError, ValueError):
            # Malformed proc data is also not evidence that the group is empty.
            return None
    return sorted(members)


def unfinished_blockers(state: State) -> str:
    """Precise, cross-project lifecycle diagnostic shared by owner actions."""
    rows = unfinished_runs(state)
    return "; ".join(
        f"#{r['id']} agent={r['agent_id']} task={r['task_or_role']} role={r['role']} "
        f"started_at={r['started_at']:.6f} ownership={r['ownership']}" for r in rows
    )


def unfinished_runs(state: State) -> list[dict]:
    """Read-only cross-project inspection of records that block owner actions."""
    rows = state.q(
        "SELECT r.*, a.project_id, a.task_id AS agent_task_id FROM run r "
        "JOIN agent a ON a.id=r.agent_id WHERE r.ended_at IS NULL ORDER BY r.id"
    )
    result = []
    for row in rows:
        item = dict(row)
        item["ownership"] = _owner_status(item)
        item["task_or_role"] = item["task_id"] or f"taskless {item['role']}"
        result.append(item)
    return result


def recover_runs(state: State, run_ids: list[int], reason: str,
                 acknowledge_quiescence: bool = False) -> list[dict]:
    """Explicitly close selected interrupted records, without replaying an outcome.

    The lifecycle lock excludes scheduler registration and final outcome writes.
    All selection checks happen again under one immediate transaction, so a
    duplicate/stale form cannot partially recover a mixed selection.
    """
    if not run_ids:
        raise ValueError("at least one run ID is required")
    if len(set(run_ids)) != len(run_ids) or any(type(i) is not int or i < 1 for i in run_ids):
        raise ValueError("run IDs must be distinct positive integers")
    if not reason or not reason.strip():
        raise ValueError("a recovery reason is required")
    with lifecycle_lock(state), state.db:
        state.db.execute("BEGIN IMMEDIATE")
        marks = ",".join("?" for _ in run_ids)
        rows = [dict(r) for r in state.q(
            f"SELECT * FROM run WHERE id IN ({marks}) ORDER BY id", run_ids)]
        found = {r["id"] for r in rows}
        missing = sorted(set(run_ids) - found)
        if missing:
            raise LookupError("unknown run IDs: " + ", ".join(map(str, missing)))
        finished = [r["id"] for r in rows if r["ended_at"] is not None]
        if finished:
            raise ValueError("runs are already finished: " + ", ".join(map(str, finished)))
        legacy_uncertain, ambiguous, live = [], [], []
        for row in rows:
            ownership = _owner_status(row)
            if ownership.startswith("live "):
                live.append(row["id"])
            elif ownership.startswith("uncertain"):
                # Only pre-ownership-schema rows are legacy.  A current row
                # that lost its owner or adapter evidence may have started an
                # untracked process and must never be overridden by an
                # acknowledgement.
                if row.get("ownership_version", 0) == 0:
                    legacy_uncertain.append(row["id"])
                else:
                    ambiguous.append(row["id"])
        if live:
            raise ValueError("refusing recovery; live ownership for run IDs: " +
                             ", ".join(map(str, live)))
        if ambiguous:
            raise ValueError("refusing recovery; ambiguous new ownership for run IDs: " +
                             ", ".join(map(str, ambiguous)))
        if legacy_uncertain and not acknowledge_quiescence:
            raise ValueError("legacy ownership is uncertain for run IDs: " + ", ".join(map(str, legacy_uncertain)) +
                             "; verify scheduler and adapter descendants are quiescent, then acknowledge")
        now = time.time()
        for row in rows:
            # ``detail`` is the session's contemporaneous evidence.  In
            # particular it may be a deliberately long adapter traceback.
            # Recovery is an owner audit event, not a new session result, so
            # keep that evidence byte-for-byte and put the recovery facts in
            # their additive columns rather than squeezing them into detail.
            detail = row["detail"]
            changed = state.db.execute(
                "UPDATE run SET outcome='INTERRUPTED', detail=?, ended_at=?, interrupted_at=?, "
                "recovered_at=?, recovery_reason=? WHERE id=? AND ended_at IS NULL",
                (detail, now, now, now, reason.strip(), row["id"]),
            ).rowcount
            if not changed:
                raise ValueError(f"run {row['id']} changed before recovery; retry inspection")
            state.db.execute("UPDATE agent SET state='blocked', updated_at=? WHERE id=?",
                             (now, row["agent_id"]))
        return rows


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
    return state.add_task(project_id, title, objective, acceptance,
                          boundaries, priority, budget_turns, depends_on)


def import_tasks(state: State, specs: list[dict] | dict,
                 project: str | None = None) -> list[str]:
    """Import browser batches atomically within the selected project.

    CLI callers omit project and retain the historical per-item import semantics.
    """
    items = specs if isinstance(specs, list) else [specs]
    if project is None:
        return [state.add_task_spec(spec) for spec in items]
    get_project(state, project)
    for spec in items:
        if not isinstance(spec, dict):
            raise TypeError("each task must be a JSON object")
        if spec.get("project") != project:
            raise ValueError("each task must belong to the selected project")
        for key in ("title", "objective"):
            if not isinstance(spec.get(key), str) or not spec[key].strip():
                raise ValueError(f"{key} is required and must be text")
        for key in ("acceptance", "boundaries", "depends_on"):
            value = spec.get(key, [] if key != "acceptance" else None)
            if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                raise ValueError(f"{key} must be a list of strings")
        for key, default in (("priority", 100), ("budget_turns", 6)):
            value = spec.get(key, default)
            if type(value) is not int or (key == "budget_turns" and value < 1):
                raise ValueError(f"{key} must be an integer" +
                                 (" greater than zero" if key == "budget_turns" else ""))
        # Validate references before opening the import transaction.  _add_task
        # repeats this under that transaction to close races with task deletion.
        state._validate_task_fields(project, spec["title"], spec["objective"],
                                    spec["acceptance"], spec.get("boundaries"),
                                    spec.get("priority", 100), spec.get("budget_turns", 6),
                                    spec.get("depends_on"))
    with state.db:
        state.db.execute("BEGIN IMMEDIATE")
        return [state._add_task_spec(spec) for spec in items]


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
    detail["check_output"] = None
    # Pin the directory and reject symlinks: evidence is never a browser path.
    if Path(task_id).name == task_id and task_id not in (".", ".."):
        try:
            directory = os.open(cfg.home / "checks", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                fd = os.open(f"{task_id}.txt", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
                with os.fdopen(fd) as evidence:
                    if stat.S_ISREG(os.fstat(evidence.fileno()).st_mode):
                        detail["check_output"] = evidence.read()
            finally:
                os.close(directory)
        except (OSError, UnicodeError):
            pass  # Missing or unsafe evidence is displayed as absent.
    detail["check_path"] = str(check_path)
    return detail


def cancel_task(state: State, task_id: str, reason: str) -> bool:
    with lifecycle_lock(state):
        return state.cancel_task(task_id, reason)


def retry_blocked_tasks(state: State) -> list[str]:
    """Atomically make currently blocked task workers runnable again.

    ``nc resume --retry`` is a lifecycle operation, not merely an incident
    acknowledgement: scheduler ownership must exclude it from selection through
    outcome application.
    """
    with lifecycle_lock(state), state.db:
        state.db.execute("BEGIN IMMEDIATE")
        rows = state.q("SELECT id FROM task WHERE status='blocked'")
        now = time.time()
        ids = []
        for row in rows:
            changed = state.db.execute(
                "UPDATE task SET status='in_progress', attempts=0, updated_at=?"
                " WHERE id=? AND status='blocked'", (now, row["id"]),
            ).rowcount
            if changed:
                state.db.execute(
                    "UPDATE agent SET state='runnable', updated_at=? WHERE id=?",
                    (now, f"worker-{row['id']}"),
                )
                ids.append(row["id"])
        return ids



def _discard_preview(cfg: Config, state: State, task) -> dict:
    """Fingerprint the task revision, branch and all discarded worktree content."""
    repo = Path(get_project(state, task["project_id"])["repo_path"])
    branch = f"nc/{task['id']}"
    commit = arbiter.git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
                         check=False)
    worktree = cfg.work_dir / task["id"]
    digest = hashlib.sha256()
    files = 0

    def scan(path):
        nonlocal files
        info = path.lstat()
        digest.update(json.dumps([str(path.relative_to(worktree)), info.st_mode]).encode())
        if path.is_symlink():
            digest.update(os.readlink(path).encode())
        elif path.is_dir():
            for child in sorted(path.iterdir()):
                scan(child)
        elif path.is_file():
            files += 1
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            raise ValueError("Cannot confirm special files in worktree")
    if worktree.exists() or worktree.is_symlink():
        scan(worktree)
    snapshot = {"task_id": task["id"], "status": task["status"],
                "updated_at": task["updated_at"], "budget": task["budget_turns"],
                "branch": branch, "commit": commit or None, "worktree": str(worktree),
                "files": files, "content": digest.hexdigest()}
    snapshot["token"] = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    return snapshot


def discard_preview(cfg: Config, state: State, task_id: str) -> dict:
    with lifecycle_lock(state):
        task = state.one("SELECT * FROM task WHERE id=?", (task_id,))
        if task is None:
            raise LookupError(f"unknown task: {task_id}")
        repo = Path(get_project(state, task["project_id"])["repo_path"])
        with repository_lock(repo):
            return _discard_preview(cfg, state, task)


def requeue_task(cfg: Config, state: State, task_id: str, fresh: bool = False,
                 budget: int | None = None, reason: str | None = None,
                 expected_discard: str | None = None) -> dict:
    """Put a task back in the queue, optionally discarding its branch and worktree."""
    if budget is not None and budget < 1:
        raise ValueError("turn budget must be greater than zero")
    with lifecycle_lock(state):
        task = state.one("SELECT * FROM task WHERE id=?", (task_id,))
        if task is None:
            raise LookupError(f"unknown task: {task_id}")
        if task["status"] == "done":
            raise ValueError(f"{task_id} is already accepted; use rollback instead")
        if state.one("SELECT 1 FROM run WHERE ended_at IS NULL"):
            raise ValueError("unfinished run records block task lifecycle (not proof of active run): " +
                             unfinished_blockers(state))
        project = get_project(state, task["project_id"])
        repo = Path(project["repo_path"])
        with repository_lock(repo):
            # The rows read before taking repository ownership are only a
            # routing hint.  Revalidate all lifecycle inputs while both
            # exclusions are owned, immediately before any Git/state write.
            task = state.one("SELECT * FROM task WHERE id=?", (task_id,))
            if task is None:
                raise LookupError(f"unknown task: {task_id}")
            if task["status"] == "done":
                raise ValueError(f"{task_id} is already accepted; use rollback instead")
            if state.one("SELECT 1 FROM run WHERE ended_at IS NULL"):
                raise ValueError("An active run must finish before changing task lifecycle")
            if fresh and (not expected_discard or
                          expected_discard != _discard_preview(cfg, state, task)["token"]):
                raise ValueError("Confirm the current discarded work before fresh requeue; "
                                 "reload the task or use --preview-discard and retry")
            with state.db:
                state.db.execute("BEGIN IMMEDIATE")
                if fresh:
                    arbiter.remove_worktree(repo, cfg.work_dir / task["id"])
                    arbiter.git(repo, "worktree", "prune")
                    branch = f"nc/{task['id']}"
                    if arbiter.git(repo, "branch", "--list", branch):
                        arbiter.git(repo, "branch", "-D", branch)
                state.db.execute("UPDATE agent SET state='blocked', turns=0 WHERE task_id=?", (task_id,))
                state.db.execute("UPDATE message SET delivered=1 WHERE task_id=? AND recipient='owner'",
                                 (task_id,))
                state.db.execute(
                    "UPDATE task SET status='queued', attempts=0, result=?, budget_turns=?, updated_at=?"
                    " WHERE id=?", (reason or "requeued by the owner", budget if budget is not None else task["budget_turns"],
                                     time.time(), task_id),
                )
            return {"task_id": task_id, "fresh": fresh, "budget": budget}


def rollback_task(state: State, task_id: str, expected_commit: str | None = None) -> dict:
    with lifecycle_lock(state):
        task = state.one("SELECT * FROM task WHERE id=?", (task_id,))
        if task is None or not task["merge_commit"]:
            raise LookupError(f"{task_id} has no recorded merge commit")
        if state.one("SELECT 1 FROM run WHERE ended_at IS NULL"):
            raise ValueError("unfinished run records block task lifecycle (not proof of active run): " +
                             unfinished_blockers(state))
        project = get_project(state, task["project_id"])
        repo = Path(project["repo_path"])
        if task["status"] != "done":
            raise ValueError(f"{task_id} is not accepted; rollback requires a done task")
        with repository_lock(repo):
            # As above, do not act on the pre-lock snapshot.  This also makes
            # a confirmation stale if an earlier lifecycle operation changed
            # the task while a caller was waiting to acquire repository scope.
            task = state.one("SELECT * FROM task WHERE id=?", (task_id,))
            if task is None or not task["merge_commit"]:
                raise LookupError(f"{task_id} has no recorded merge commit")
            if task["status"] != "done":
                raise ValueError(f"{task_id} is not accepted; rollback requires a done task")
            if state.one("SELECT 1 FROM run WHERE ended_at IS NULL"):
                raise ValueError("An active run must finish before changing task lifecycle")
            with state.db:
                state.db.execute("BEGIN IMMEDIATE")
                if not expected_commit or expected_commit != task["merge_commit"]:
                    raise ValueError("Confirm the current merge commit before rollback; reload and retry")
                commit = arbiter.revert(repo, task["merge_commit"])
                state.db.execute(
                    "UPDATE task SET status='blocked', result=?, updated_at=? WHERE id=?",
                    (f"reverted by the owner in {commit}", time.time(), task_id),
                )
                state.db.execute(
                    "INSERT INTO incident(kind,detail,created_at) VALUES('rollback',?,?)",
                    (f"{task_id} reverted in {commit}", time.time()),
                )
            try:
                mirror_error = arbiter.mirror(repo, project["mirror"])
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                # The revert and its lifecycle transition are already durable.
                mirror_error = str(exc)
            if mirror_error:
                state.incident("mirror_push", f"{task_id}: {mirror_error}")
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


def request_plan(state: State, cfg: Config, project: str, note: str | None = None) -> tuple[str, int]:
    """Queue an explicit planning request without running a planner session."""
    return state.planner_feedback(project, note or "Request a planning pass.",
                                  cfg.model_for("planner"), plan_request=True)


def feedback_history(state: State, project_id: str) -> list[dict]:
    """Full owner-feedback history for a project, including consumed messages.

    ``delivered`` only records that the planner consumed the message.  It is
    deliberately not inferred from proposal or task state: a planner may ask a
    question, propose work, or decide no implementation is appropriate.
    """
    get_project(state, project_id)
    rows = state.q(
        "SELECT m.* FROM message m JOIN agent a ON a.id=m.recipient"
        " WHERE m.kind=? AND a.project_id=? ORDER BY m.id",
        (protocol.FEEDBACK, project_id),
    )
    history = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        revision = state.one("SELECT original_id, replacement_id FROM proposal_revision"
                             " WHERE feedback_id=?", (item["id"],))
        if revision:
            item["target"] = {"kind": "proposal", "id": revision["original_id"],
                              "replacement_id": revision["replacement_id"]}
        elif item["task_id"]:
            item["target"] = {"kind": "task", "id": item["task_id"]}
        else:
            item["target"] = {"kind": "project", "id": project_id}
        history.append(item)
    return history


def project_owner_questions(state: State, project_id: str,
                            include_delivered: bool = True) -> list[dict]:
    """Questions to the owner from this project's workers and taskless planner."""
    get_project(state, project_id)
    sql = ("SELECT m.* FROM message m JOIN agent a ON a.id=m.sender"
           " WHERE m.kind=? AND m.recipient='owner' AND a.project_id=?")
    if not include_delivered:
        sql += " AND m.delivered=0"
    result = []
    for row in state.q(sql + " ORDER BY m.id", (protocol.QUESTION, project_id)):
        item = dict(row)
        item["text"] = json.loads(item["payload"]).get("question", "")
        item["answerable"] = answerable_question(state, item)
        result.append(item)
    return result


# --- inbox / answers -------------------------------------------------------

def inbox(state: State, include_delivered: bool = False) -> list[dict]:
    rows = []
    for row in state.inbox("owner", undelivered_only=not include_delivered):
        item = dict(row)
        payload = json.loads(item["payload"])
        item["text"] = payload.get("question") or payload.get("reason") or json.dumps(payload)
        item["answerable"] = answerable_question(state, item)
        rows.append(item)
    return rows


def answerable_question(state: State, question) -> bool:
    if question["kind"] != protocol.QUESTION or question["recipient"] != "owner" or question["delivered"]:
        return False
    agent = state.one("SELECT * FROM agent WHERE id=?", (question["sender"],))
    if agent is None or agent["task_id"] != question["task_id"]:
        return False
    if question["task_id"]:
        task = state.one("SELECT * FROM task WHERE id=?", (question["task_id"],))
        # merge_commit survives rollback as history; status tracks current acceptance.
        # Requeue retires old owner messages before starting the next lifecycle.
        if task is None or task["status"] in ("done", "cancelled"):
            return False
    return True


def answer_message(state: State, message_id: int, text: str) -> dict:
    if not text or not text.strip():
        raise ValueError("an answer is required")
    with lifecycle_lock(state), state.db:
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
        if not answerable_question(state, question):
            raise ValueError("message is not a currently answerable owner question")
        agent_id = question["sender"]
        now = time.time()
        # Claim the question before inserting its answer.  The predicate makes
        # duplicate submits fail even if a caller holds a stale question row.
        if not state.db.execute(
            "UPDATE message SET delivered=1 WHERE id=? AND delivered=0", (question["id"],),
        ).rowcount:
            raise ValueError("message is not a currently answerable owner question")
        from . import protocol
        state.db.execute(
            "INSERT INTO message(kind,sender,recipient,payload,task_id,in_reply_to,created_at)"
            " VALUES(?,'owner',?,?,?,?,?)",
            (protocol.ANSWER, agent_id, json.dumps({"answer": text}),
             question["task_id"], question["id"], now),
        )
        state.db.execute("UPDATE agent SET state='runnable', updated_at=? WHERE id=?",
                         (now, agent_id))
        if question["task_id"]:
            state.db.execute("UPDATE task SET status='in_progress', updated_at=? WHERE id=?",
                             (now, question["task_id"]))
    return {"agent_id": agent_id, "message_id": message_id}
