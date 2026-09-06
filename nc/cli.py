"""Command line interface: `nc <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from . import arbiter, protocol
from .config import Config
from .scheduler import Scheduler
from .state import State


def _open(args) -> tuple[Config, State]:
    cfg = Config.load(args.home)
    cfg.home.mkdir(parents=True, exist_ok=True)
    return cfg, State(cfg.db_path)


def _age(ts: float) -> str:
    delta = int(time.time() - ts)
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


def cmd_init(args) -> int:
    cfg = Config.load(args.home)
    cfg.save()
    State(cfg.db_path)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    print(f"initialized {cfg.home}")
    return 0


def cmd_project(args) -> int:
    _, state = _open(args)
    state.add_project(args.id, args.title or args.id, str(Path(args.repo).resolve()),
                      args.test_cmd, args.quota, args.mirror)
    print(f"project {args.id} -> {args.repo}")
    return 0


def cmd_task(args) -> int:
    _, state = _open(args)
    acceptance = args.accept or []
    if args.file:
        specs = json.loads(Path(args.file).read_text())
        for spec in specs if isinstance(specs, list) else [specs]:
            print(state.add_task_spec(spec))
        return 0
    tid = state.add_task(args.project, args.title, args.objective, acceptance,
                         args.boundary or [], args.priority, args.budget,
                         args.after or [])
    print(tid)
    return 0


def cmd_cancel(args) -> int:
    _, state = _open(args)
    try:
        changed = state.cancel_task(args.task_id, args.reason)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{args.task_id} " + ("cancelled" if changed else "already cancelled"))
    return 0


def cmd_requeue(args) -> int:
    """Put a task back in the queue, optionally from a clean branch off the base."""
    cfg, state = _open(args)
    task = state.one("SELECT * FROM task WHERE id=?", (args.task_id,))
    if task is None:
        print(f"unknown task: {args.task_id}", file=sys.stderr)
        return 1
    if task["status"] == "done":
        print(f"{args.task_id} is already accepted; use rollback instead", file=sys.stderr)
        return 1
    project = state.one("SELECT * FROM project WHERE id=?", (task["project_id"],))
    repo = Path(project["repo_path"])
    if args.fresh:
        arbiter.remove_worktree(repo, cfg.work_dir / task["id"])
        arbiter.git(repo, "worktree", "prune", check=False)
        arbiter.git(repo, "branch", "-D", f"nc/{task['id']}", check=False)
    # Agents keep their history but restart with a fresh turn budget.
    state.x("UPDATE agent SET state='blocked', turns=0 WHERE task_id=?", (task["id"],))
    state.x("UPDATE message SET delivered=1 WHERE task_id=? AND recipient='owner'",
            (task["id"],))
    fields = {"status": "queued", "attempts": 0,
              "result": args.reason or "requeued by the owner"}
    if args.budget:
        fields["budget_turns"] = args.budget
    state.set_task(task["id"], **fields)
    print(f"{task['id']} queued again" + (" from a fresh branch" if args.fresh else "")
          + (f", budget {args.budget} turns" if args.budget else ""))
    return 0


def cmd_proposals(args) -> int:
    _, state = _open(args)
    rows = state.q("SELECT * FROM proposal ORDER BY id")
    for row in rows:
        print(f"{row['id']} {row['project_id']} {row['status']} "
              f"tasks={len(json.loads(row['spec']))} "
              f"source={row['source']} {row['rationale']}")
        print(f"  inspect: nc proposal {row['id']}")
        for finding in json.loads(row["findings"]):
            print(f"  finding: {finding}")
    if not rows:
        print("(no proposals)")
    return 0


def cmd_proposal(args) -> int:
    _, state = _open(args)
    row = state.one("SELECT * FROM proposal WHERE id=?", (args.proposal_id,))
    if row is None:
        print(f"unknown proposal: {args.proposal_id}", file=sys.stderr)
        return 1
    detail = dict(row)
    detail["spec"] = json.loads(detail["spec"])
    detail["findings"] = json.loads(detail["findings"])
    review = state.one(
        "SELECT status, findings, recommendation FROM plan_review"
        " WHERE proposal_id=? AND spec=?", (row["id"], row["spec"]),
    )
    detail["plan_review"] = dict(review) if review else None
    if review:
        detail["plan_review"]["findings"] = json.loads(review["findings"])
    print(json.dumps(detail, indent=2, ensure_ascii=False))
    return 0


def cmd_decide_proposal(args) -> int:
    _, state = _open(args)
    try:
        if args.cmd == "approve":
            ids = state.approve_proposal(args.proposal_id, force=args.force)
            row = state.one("SELECT findings FROM proposal WHERE id=?", (args.proposal_id,))
            for finding in json.loads(row["findings"]):
                print(f"overriding finding: {finding}", file=sys.stderr)
            for task_id in ids:
                print(task_id)
        else:
            state.reject_proposal(args.proposal_id, args.reason)
            print(f"rejected proposal {args.proposal_id}")
    except (ValueError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def cmd_tasks(args) -> int:
    _, state = _open(args)
    sql = "SELECT * FROM task WHERE 1=1"
    params: tuple = ()
    if args.project:
        sql += " AND project_id=?"
        params = (args.project,)
    if not args.all:
        sql += " AND status != 'cancelled'"
    for row in state.q(sql + " ORDER BY priority, created_at", params):
        unmet = state.unmet_dependencies(row["id"])
        waiting = f" waits-for={','.join(unmet)}" if unmet else ""
        print(f"{row['id']:<20} {row['status']:<12} att={row['attempts']} "
              f"{row['title'][:60]}{waiting}")
    return 0


def cmd_why(args) -> int:
    cfg, state = _open(args)
    task = state.one("SELECT * FROM task WHERE id=?", (args.task_id,))
    if task is None:
        print(f"unknown task: {args.task_id}", file=sys.stderr)
        return 1
    print(f"{task['id']}: {task['title']}\nstatus: {task['status']}")
    depends_on = json.loads(task["depends_on"] or "[]")
    if depends_on:
        unmet = state.unmet_dependencies(task["id"])
        print(f"depends on: {', '.join(depends_on)}"
              + (f" (waiting for {', '.join(unmet)})" if unmet else " (all accepted)"))
    for dep in depends_on:
        other = state.one("SELECT status FROM task WHERE id=?", (dep,))
        if other is not None and other["status"] == "cancelled":
            print(f"  {dep}: cancelled; dependency remains unmet (inspect with nc why {dep})")
    print(f"\nobjective:\n{task['objective']}")
    print("\nacceptance criteria:")
    criteria = json.loads(task["acceptance"])
    for criterion in criteria:
        print(f"  - {criterion}")
    if not criteria:
        print("  (none)")

    print("\nruns:")
    runs = state.q("SELECT * FROM run WHERE task_id=? ORDER BY started_at, id", (task["id"],))
    now = time.time()
    for run in runs:
        end = run["ended_at"] if run["ended_at"] is not None else now
        duration = f"{end - run['started_at']:.1f}s"
        if run["ended_at"] is None:
            duration += " elapsed"
        print(f"  #{run['id']} agent={run['agent_id']} role={run['role']} "
              f"outcome={run['outcome'] or 'running'} duration={duration} "
              f"log={run['log_path'] or '(none)'}")
    if not runs:
        print("  (none)")

    print("\nmessages:")
    messages = state.q("SELECT * FROM message WHERE task_id=? ORDER BY id", (task["id"],))
    for message in messages:
        print(f"  #{message['id']} [{message['kind']}] "
              f"{message['sender']} -> {message['recipient']}: {message['payload']}")
    if not messages:
        print("  (none)")

    check_path = cfg.home / "checks" / f"{task['id']}.txt"
    print(f"\nacceptance check output ({check_path}):")
    try:
        output = check_path.read_text()
    except FileNotFoundError:
        print("  (no stored check output)")
    else:
        print(output, end="" if output.endswith("\n") else "\n")
    return 0


def cmd_costs(args) -> int:
    _, state = _open(args)
    now = time.time()
    print("Wall time sums run durations, including elapsed time for running turns.")
    for column, label in (("task_id", "task"), ("role", "role")):
        print(f"\nby {label}:")
        rows = state.q(
            f"SELECT {column} AS name, COUNT(*) AS runs, SUM(tokens) AS tokens,"
            " COUNT(*) - COUNT(tokens) AS unknown,"
            " SUM(COALESCE(ended_at, ?) - started_at) AS seconds"
            f" FROM run GROUP BY {column} ORDER BY {column}",
            (now,),
        )
        for row in rows:
            tokens = row["tokens"] if row["tokens"] is not None else "unknown"
            print(f"  {row['name'] or '(none)'} runs={row['runs']} tokens={tokens} "
                  f"unknown_runs={row['unknown']} wall={row['seconds']:.1f}s")
        if not rows:
            print("  (no runs)")
    return 0


def cmd_agents(args) -> int:
    _, state = _open(args)
    for row in state.q("SELECT * FROM agent ORDER BY created_at"):
        print(f"{row['id']:<28} {row['role']:<7} {row['state']:<9} turns={row['turns']} "
              f"{row['task_id'] or ''}")
    return 0


def cmd_gc(args) -> int:
    cfg, state = _open(args)
    work_root = cfg.work_dir.resolve()
    result = 0
    repos = set()
    # Keep task statuses stable while removing their worktrees.
    with state.db:
        state.db.execute("BEGIN IMMEDIATE")
        tasks = state.q(
            "SELECT task.id, project.repo_path FROM task"
            " JOIN project ON project.id=task.project_id"
            " WHERE task.status IN ('done', 'blocked') ORDER BY task.id"
        )
        for task in tasks:
            path = work_root / task["id"]
            if path.is_symlink() or path.resolve().parent != work_root:
                print(f"refusing worktree outside work directory: {path}", file=sys.stderr)
                result = 1
                continue
            repo = Path(task["repo_path"])
            repos.add(repo)
            if not path.exists():
                continue
            try:
                arbiter.git(repo, "worktree", "remove", "--force", str(path))
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                result = 1
            else:
                print(f"removed {path}")
        for repo in sorted(repos):
            try:
                arbiter.git(repo, "worktree", "prune")
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                result = 1
    return result


def cmd_inbox(args) -> int:
    _, state = _open(args)
    rows = state.inbox("owner", undelivered_only=not args.all)
    for row in rows:
        payload = json.loads(row["payload"])
        text = payload.get("question") or payload.get("reason") or json.dumps(payload)
        print(f"#{row['id']} [{row['kind']}] from {row['sender']} "
              f"({row['task_id'] or '-'}, {_age(row['created_at'])} ago)\n    {text}\n")
    if not rows:
        print("(no pending messages)")
    return 0


def cmd_answer(args) -> int:
    _, state = _open(args)
    with state.db:
        state.db.execute("BEGIN IMMEDIATE")
        question = state.one("SELECT * FROM message WHERE id=?", (args.message_id,))
        if question is None:
            print(f"no message #{args.message_id}", file=sys.stderr)
            return 1
        if state.one(
            "SELECT 1 FROM task WHERE status='cancelled' AND"
            " (id=? OR id=(SELECT task_id FROM agent WHERE id=?))",
            (question["task_id"], question["sender"]),
        ):
            print("task is cancelled; use nc requeue explicitly to restore it", file=sys.stderr)
            return 1
        agent_id = question["sender"]
        now = time.time()
        state.db.execute(
            "INSERT INTO message(kind,sender,recipient,payload,task_id,in_reply_to,created_at)"
            " VALUES(?,'owner',?,?,?,?,?)",
            (protocol.ANSWER, agent_id, json.dumps({"answer": args.text}),
             question["task_id"], question["id"], now),
        )
        state.db.execute("UPDATE message SET delivered=1 WHERE id=?", (question["id"],))
        state.db.execute("UPDATE agent SET state='runnable', updated_at=? WHERE id=?",
                         (now, agent_id))
        if question["task_id"]:
            state.db.execute("UPDATE task SET status='in_progress', updated_at=? WHERE id=?",
                             (now, question["task_id"]))
    print(f"answered {agent_id}; it is runnable again")
    return 0


def cmd_feedback(args) -> int:
    cfg, state = _open(args)
    text = args.text if args.cmd == "feedback" else (args.note or "Request a planning pass.")
    try:
        agent_id, message_id = state.planner_feedback(
            args.project, text, cfg.model_for("planner"), getattr(args, "task", None),
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"queued feedback #{message_id} for {agent_id}; planner is runnable")
    return 0


def cmd_incidents(args) -> int:
    _, state = _open(args)
    rows = state.q("SELECT * FROM incident ORDER BY id") if args.all else state.open_incidents()
    for row in rows:
        print(f"#{row['id']} [{row['kind']}] {_age(row['created_at'])} ago: {row['detail']}")
        if row["resolved"]:
            timestamp = (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(row["resolved_at"]))
                if row["resolved_at"] is not None else "unknown"
            )
            print(f"  resolved at {timestamp}: {row['resolution_note'] or '(no recorded note)'}")
    return 0


def cmd_resolve(args) -> int:
    if not args.reason.strip():
        print("resolution reason must not be empty", file=sys.stderr)
        return 1
    _, state = _open(args)
    try:
        changed = state.resolve_incident(args.incident_id, args.reason)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"incident #{args.incident_id}: {'resolved' if changed else 'already resolved'}")
    return 0


def cmd_preflight(args) -> int:
    cfg, state = _open(args)
    ok, detail = Scheduler(cfg, state).preflight()
    print(("ok: " if ok else "FAILED: ") + detail)
    return 0 if ok else 1


def cmd_health(args) -> int:
    cfg, state = _open(args)
    print(f"database: {cfg.db_path}")
    counts = dict.fromkeys(
        ("queued", "in_progress", "in_review", "done", "failed", "blocked"), 0,
    )
    for row in state.q("SELECT status, COUNT(*) AS c FROM task GROUP BY status"):
        counts[row["status"]] = row["c"]
    for status, count in counts.items():
        print(f"tasks {status}: {count}")
    runnable = state.one("SELECT COUNT(*) AS c FROM agent WHERE state='runnable'")["c"]
    incidents = state.one("SELECT COUNT(*) AS c FROM incident WHERE resolved=0")["c"]
    print(f"runnable agents: {runnable}")
    print(f"open incidents: {incidents}")
    return 0


def cmd_step(args) -> int:
    cfg, state = _open(args)
    print(Scheduler(cfg, state).step())
    return 0


def cmd_run(args) -> int:
    cfg, state = _open(args)
    Scheduler(cfg, state).run(max_turns=args.max_turns)
    return 0


def cmd_rollback(args) -> int:
    _, state = _open(args)
    task = state.one("SELECT * FROM task WHERE id=?", (args.task_id,))
    if task is None or not task["merge_commit"]:
        print(f"{args.task_id} has no recorded merge commit", file=sys.stderr)
        return 1
    project = state.one("SELECT * FROM project WHERE id=?", (task["project_id"],))
    repo = Path(project["repo_path"])
    commit = arbiter.revert(repo, task["merge_commit"])
    error = arbiter.mirror(repo, project["mirror"])
    state.set_task(args.task_id, status="blocked",
                   result=f"reverted by the owner in {commit}")
    state.incident("rollback", f"{args.task_id} reverted in {commit}")
    print(f"reverted {task['merge_commit']} in {commit}"
          + (f"; mirror push failed: {error}" if error else ""))
    return 0


def cmd_stop(args) -> int:
    cfg, _ = _open(args)
    (cfg.home / "STOP").write_text(args.reason or "stopped by the owner\n")
    print(f"wrote {cfg.home / 'STOP'}; `nc run` will exit immediately until `nc resume`")
    return 0


def cmd_resume(args) -> int:
    cfg, state = _open(args)
    stop = cfg.home / "STOP"
    if stop.exists():
        print(f"removing {stop}: {stop.read_text().strip()}")
        stop.unlink()
    state.resolve_open_incidents("Closed by nc resume")
    if args.retry:
        for row in state.q("SELECT * FROM task WHERE status='blocked'"):
            # Selection may be stale: serialize the guarded transition and
            # worker update with cancellation, which retires both together.
            with state.db:
                changed = state.db.execute(
                    "UPDATE task SET status='in_progress', attempts=0, updated_at=?"
                    " WHERE id=? AND status='blocked'", (time.time(), row["id"]),
                ).rowcount
                if not changed:
                    continue
                state.db.execute(
                    "UPDATE agent SET state='runnable', updated_at=? WHERE id=?",
                    (time.time(), f"worker-{row['id']}"),
                )
            print(f"unblocked {row['id']}")
    print("resumed")
    return 0


def cmd_status(args) -> int:
    _, state = _open(args)
    for row in state.q("SELECT project_id, status, COUNT(*) AS c FROM task"
                       " GROUP BY project_id, status ORDER BY project_id"):
        print(f"{row['project_id']:<12} {row['status']:<12} {row['c']}")
    feedback = state.q(
        "SELECT m.*, a.project_id FROM message m JOIN agent a ON a.id=m.recipient"
        " WHERE m.kind=? AND m.delivered=0 ORDER BY m.id", (protocol.FEEDBACK,),
    )
    if feedback:
        print("\npending feedback:")
        for row in feedback:
            print(f"  #{row['id']} {row['project_id']} ({row['task_id'] or '-'}): "
                  f"{json.loads(row['payload'])['text']}")
    runs = state.q("SELECT * FROM run ORDER BY id DESC LIMIT ?", (args.limit,))
    if runs:
        print("\nlast runs:")
        for row in runs:
            print(f"  {row['agent_id']:<28} {row['outcome'] or 'running':<10} "
                  f"{_age(row['started_at'])} ago  {(row['detail'] or '')[:60]}")
    incidents = state.open_incidents()
    if incidents:
        print(f"\nopen incidents: {len(incidents)} (see `nc incidents`)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nc", description="Neocortex multi-agent runner")
    p.add_argument("--home", type=Path, default=None, help="state directory (default $NC_HOME)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)

    sp = sub.add_parser("project", help="register or update a project")
    sp.add_argument("id")
    sp.add_argument("repo")
    sp.add_argument("--title")
    sp.add_argument("--test-cmd", dest="test_cmd")
    sp.add_argument("--quota", type=float, default=1.0)
    sp.add_argument("--mirror", help="git remote to push accepted work to")
    sp.set_defaults(func=cmd_project)

    sp = sub.add_parser("task", help="add a task")
    sp.add_argument("--file", help="JSON task spec")
    sp.add_argument("--project")
    sp.add_argument("--title")
    sp.add_argument("--objective")
    sp.add_argument("--accept", action="append", help="acceptance criterion ('$ cmd' = shell check)")
    sp.add_argument("--boundary", action="append")
    sp.add_argument("--priority", type=int, default=100)
    sp.add_argument("--budget", type=int, default=6)
    sp.add_argument("--after", action="append",
                    help="task id that must be accepted first; repeatable")
    sp.set_defaults(func=cmd_task)

    sp = sub.add_parser("requeue", help="put a blocked or failed task back in the queue")
    sp.add_argument("task_id")
    sp.add_argument("--fresh", action="store_true",
                    help="discard its branch and worktree and start from the base branch")
    sp.add_argument("--reason")
    sp.add_argument("--budget", type=int,
                    help="new turn budget for the task, for work that needs more room")
    sp.set_defaults(func=cmd_requeue)

    sub.add_parser(
        "proposals", help="list proposals, task counts and inspection commands",
        description="List proposals and task counts. Preview with nc proposal ID before nc approve ID.",
    ).set_defaults(
        func=cmd_proposals,
    )
    sp = sub.add_parser(
        "proposal", help="preview full task specs as JSON before approval",
        description="Read-only full proposal JSON: titles, objectives, acceptance, boundaries, "
        "dependencies, findings and advisory plan review. Run nc proposals, then nc proposal ID "
        "to preview before nc approve ID. Spec IDs are proposal-local dependency references; "
        "only owner approval creates queued tasks with task IDs for nc why.",
    )
    sp.add_argument("proposal_id", type=int)
    sp.set_defaults(func=cmd_proposal)
    for command in ("approve", "reject"):
        sp = sub.add_parser(command, help=f"{command} a pending proposal")
        sp.add_argument("proposal_id", type=int)
        if command == "approve":
            sp.add_argument("--force", action="store_true", help="override proposal findings")
        if command == "reject":
            sp.add_argument("reason")
        sp.set_defaults(func=cmd_decide_proposal)

    sp = sub.add_parser("cancel", help="retire a superseded task while preserving history")
    sp.add_argument("task_id")
    sp.add_argument("--reason", required=True)
    sp.set_defaults(func=cmd_cancel)

    sp = sub.add_parser("tasks")
    sp.add_argument("--all", action="store_true", help="include cancelled tasks")
    sp.add_argument("--project")
    sp.set_defaults(func=cmd_tasks)

    sp = sub.add_parser(
        "why", help="show a task's status and review evidence",
        description="Inspect an existing task by its task ID after task creation. "
        "For pending proposal specs and proposal-local IDs, use nc proposal ID before approval.",
    )
    sp.add_argument("task_id")
    sp.set_defaults(func=cmd_why)

    sub.add_parser("costs", help="show tokens and wall time by task and role").set_defaults(
        func=cmd_costs,
    )
    sub.add_parser("agents").set_defaults(func=cmd_agents)
    sub.add_parser("gc", help="remove done and blocked task worktrees").set_defaults(func=cmd_gc)

    sp = sub.add_parser("inbox", help="questions and incidents addressed to you")
    sp.add_argument("--all", action="store_true")
    sp.set_defaults(func=cmd_inbox)

    sp = sub.add_parser("answer", help="answer a question and wake the agent")
    sp.add_argument("message_id", type=int)
    sp.add_argument("text")
    sp.set_defaults(func=cmd_answer)

    sp = sub.add_parser("feedback", help="queue owner feedback and wake the project planner")
    sp.add_argument("text")
    sp.add_argument("--project")
    sp.add_argument("--task")
    sp.set_defaults(func=cmd_feedback)

    sp = sub.add_parser("plan", help="request a planning pass on the next timer tick")
    sp.add_argument("project")
    sp.add_argument("--note")
    sp.set_defaults(func=cmd_feedback)

    sp = sub.add_parser("incidents", help="list open incidents")
    sp.add_argument("--all", action="store_true", help="include closed incidents and resolution details")
    sp.set_defaults(func=cmd_incidents)

    sp = sub.add_parser("resolve", help="acknowledge only the selected incident; leave STOP unchanged",
                        description="Close one incident without asserting repository repair or "
                        "changing STOP, tasks, or agents.")
    sp.add_argument("incident_id", type=int)
    sp.add_argument("--reason", required=True, help="owner resolution note")
    sp.set_defaults(func=cmd_resolve)

    sp = sub.add_parser("rollback", help="revert an accepted task")
    sp.add_argument("task_id")
    sp.set_defaults(func=cmd_rollback)

    sp = sub.add_parser("stop", help="stop the loop after the current turn")
    sp.add_argument("reason", nargs="?")
    sp.set_defaults(func=cmd_stop)

    sp = sub.add_parser("resume", help="clear STOP and close all open incidents")
    sp.add_argument("--retry", action="store_true", help="also requeue blocked tasks")
    sp.set_defaults(func=cmd_resume)

    sub.add_parser("preflight").set_defaults(func=cmd_preflight)
    sub.add_parser("health", help="show state database and counts").set_defaults(func=cmd_health)
    sub.add_parser("step", help="run exactly one agent turn").set_defaults(func=cmd_step)

    sp = sub.add_parser("run", help="run turns until idle or a stop condition")
    sp.add_argument("--max-turns", type=int, default=0)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("status")
    sp.add_argument("--limit", type=int, default=10)
    sp.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
