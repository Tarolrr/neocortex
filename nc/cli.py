"""Command line interface: `nc <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from . import protocol
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
                      args.test_cmd, args.quota)
    print(f"project {args.id} -> {args.repo}")
    return 0


def cmd_task(args) -> int:
    _, state = _open(args)
    acceptance = args.accept or []
    if args.file:
        spec = json.loads(Path(args.file).read_text())
        tid = state.add_task(spec["project"], spec["title"], spec["objective"],
                             spec["acceptance"], spec.get("boundaries"),
                             spec.get("priority", 100), spec.get("budget_turns", 6))
    else:
        tid = state.add_task(args.project, args.title, args.objective, acceptance,
                             args.boundary or [], args.priority, args.budget)
    print(tid)
    return 0


def cmd_tasks(args) -> int:
    _, state = _open(args)
    sql = "SELECT * FROM task"
    params: tuple = ()
    if args.project:
        sql += " WHERE project_id=?"
        params = (args.project,)
    for row in state.q(sql + " ORDER BY priority, created_at", params):
        print(f"{row['id']:<20} {row['status']:<12} att={row['attempts']} "
              f"{row['title'][:60]}")
    return 0


def cmd_agents(args) -> int:
    _, state = _open(args)
    for row in state.q("SELECT * FROM agent ORDER BY created_at"):
        print(f"{row['id']:<28} {row['role']:<7} {row['state']:<9} turns={row['turns']} "
              f"{row['task_id'] or ''}")
    return 0


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
    question = state.one("SELECT * FROM message WHERE id=?", (args.message_id,))
    if question is None:
        print(f"no message #{args.message_id}", file=sys.stderr)
        return 1
    agent_id = question["sender"]
    state.send(protocol.ANSWER, "owner", agent_id, {"answer": args.text},
               task_id=question["task_id"], in_reply_to=question["id"])
    state.mark_delivered([question["id"]])
    state.set_agent(agent_id, state="runnable")
    if question["task_id"]:
        state.set_task(question["task_id"], status="in_progress")
    print(f"answered {agent_id}; it is runnable again")
    return 0


def cmd_incidents(args) -> int:
    _, state = _open(args)
    for row in state.open_incidents():
        print(f"#{row['id']} [{row['kind']}] {_age(row['created_at'])} ago: {row['detail']}")
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


def cmd_status(args) -> int:
    _, state = _open(args)
    for row in state.q("SELECT project_id, status, COUNT(*) AS c FROM task"
                       " GROUP BY project_id, status ORDER BY project_id"):
        print(f"{row['project_id']:<12} {row['status']:<12} {row['c']}")
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
    sp.set_defaults(func=cmd_task)

    sp = sub.add_parser("tasks")
    sp.add_argument("--project")
    sp.set_defaults(func=cmd_tasks)

    sub.add_parser("agents").set_defaults(func=cmd_agents)

    sp = sub.add_parser("inbox", help="questions and incidents addressed to you")
    sp.add_argument("--all", action="store_true")
    sp.set_defaults(func=cmd_inbox)

    sp = sub.add_parser("answer", help="answer a question and wake the agent")
    sp.add_argument("message_id", type=int)
    sp.add_argument("text")
    sp.set_defaults(func=cmd_answer)

    sub.add_parser("incidents").set_defaults(func=cmd_incidents)
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
