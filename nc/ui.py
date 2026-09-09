"""Server-rendered owner browser console.

Phase one deliberately stays small:

- No frontend build step. Pages are rendered by plain Python string templates
  (this module) and the only packaged asset is a static stylesheet
  (`nc/static/style.css`), served straight off disk.
- Every GET is read-only. Every mutation is a POST, guarded by a per-session
  CSRF token and Host/Origin checks, so a page loaded from another origin (or
  an image/link embedded on some other site) cannot drive a change here.
- Every request opens and closes its own `State` (its own SQLite connection);
  nothing is held across requests. See `docs/ui-access.md` for local and
  SSH-tunnel and explicitly configured trusted-network access, and
  `docs/follow-ups.md` for what phase one defers
  (scheduler administration, incidents, project administration).

This module never shells out and never starts an agent turn or session: it
only calls `nc.operations`, `nc.state.State` and `nc.arbiter`, the same
building blocks the CLI uses.
"""

from __future__ import annotations

import html
import http.server
import ipaddress
import json
import logging
import re
import secrets
import socket
import sqlite3
import subprocess
import urllib.parse
from collections.abc import Callable
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from pathlib import Path
from typing import Any

from . import operations
from .config import Config
from .state import State

log = logging.getLogger("nc.ui")

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "nc_session"
REQUEST_TIMEOUT_S = 5.0  # request-scoped connection's SQLite busy timeout


# --- rendering helpers -------------------------------------------------------

def _e(value: Any) -> str:
    """Escape any value that may contain owner- or agent-authored text."""
    return html.escape("" if value is None else str(value), quote=True)


def _segment(value: str) -> str:
    """Encode an identifier as one URL path segment, including reserved slashes."""
    return urllib.parse.quote(value, safe="")


# TODO(FU-001, FU-002): scheduler/incident administration remains CLI-only;
# see docs/follow-ups.md before adding navigation and mutation routes.
def _nav(active: str) -> str:
    items = [
        ("projects", "/projects", "Projects"),
        ("inbox", "/inbox", "Inbox"),
        ("runs", "/runs", "Unfinished runs"),
    ]
    current = ' aria-current="page"'
    links = "".join(
        f'<a href="{href}"{current if key == active else ""}>{label}</a>'
        for key, href, label in items
    )
    return f'<nav aria-label="Main">{links}</nav>'


def _page(title: str, active: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} - Neocortex</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
<header>
<h1><a href="/projects">Neocortex</a></h1>
{_nav(active)}
</header>
<main>
{body}
</main>
</body>
</html>"""


def _flash(kind: str, message: str) -> str:
    if not message:
        return ""
    return f'<p class="flash flash-{_e(kind)}" role="status">{_e(message)}</p>'


def _error_page(status: HTTPStatus, message: str) -> bytes:
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{status.value} - Neocortex</title>
<link rel="stylesheet" href="/static/style.css"></head>
<body><main><h1>{status.value} {_e(status.phrase)}</h1><p role="alert">{_e(message)}</p></main></body></html>"""
    return body.encode("utf-8")


def _csrf_field(token: str) -> str:
    return f'<input type="hidden" name="csrf_token" value="{_e(token)}">'


# --- project navigation ------------------------------------------------------

# TODO(FU-003): project registration/configuration is deferred; see docs/follow-ups.md.
def _project_list_page(state: State) -> str:
    rows = operations.projects(state)
    if not rows:
        body = "<p>No projects are registered yet. Run <code>nc project</code> from the CLI.</p>"
    else:
        items = "".join(
            f'<li><a href="/p/{_segment(row["id"])}/tasks">{_e(row["title"])}</a> '
            f'<span class="muted">({_e(row["id"])})</span></li>'
            for row in rows
        )
        body = f"<h2>Projects</h2><ul class=\"list\">{items}</ul>"
    return _page("Projects", "projects", body)


# --- tasks --------------------------------------------------------------

def _task_row(row: dict) -> str:
    waiting = ""
    if row["unmet_dependencies"]:
        deps = ", ".join(_e(d) for d in row["unmet_dependencies"])
        waiting = f'<br><span class="muted">waits for {deps}</span>'
    return (
        f'<tr><td><a href="/t/{_segment(row["id"])}">{_e(row["id"])}</a></td>'
        f'<td>{_e(row["status"])}</td><td>{row["attempts"]}</td>'
        f'<td>{_e(row["title"])}{waiting}</td></tr>'
    )


def _task_list_page(state: State, project: dict, include_cancelled: bool, flash: str, error: str) -> str:
    rows = operations.tasks(state, project["id"], include_cancelled)
    table = "".join(_task_row(r) for r in rows) or '<tr><td colspan="4">(no tasks)</td></tr>'
    toggle_href = f'/p/{_segment(project["id"])}/tasks' + ("" if include_cancelled else "?all=1")
    toggle_label = "Hide cancelled tasks" if include_cancelled else "Show cancelled tasks"
    body = f"""
{_flash('ok', flash)}{_flash('error', error)}
<h2>{_e(project["title"])} tasks</h2>
<p>
<a href="/p/{_segment(project["id"])}/tasks/new">New task</a> ·
<a href="/p/{_segment(project["id"])}/tasks/import">Import JSON</a> ·
<a href="/p/{_segment(project["id"])}/proposals">Proposals</a> ·
<a href="/p/{_segment(project["id"])}/feedback">Feedback / plan</a> ·
<a href="{toggle_href}">{toggle_label}</a>
</p>
<table>
<caption class="sr-only">Tasks for {_e(project["title"])}</caption>
<thead><tr><th scope="col">id</th><th scope="col">status</th>
<th scope="col">attempts</th><th scope="col">title</th></tr></thead>
<tbody>{table}</tbody>
</table>"""
    return _page(f"Tasks - {project['title']}", "projects", body)


def _task_new_page(project: dict, csrf: str, error: str, values: dict) -> str:
    def v(name: str) -> str:
        return _e(values.get(name, ""))

    body = f"""
{_flash('error', error)}
<h2>New task in {_e(project["title"])}</h2>
<form method="post" action="/p/{_segment(project["id"])}/tasks/new">
{_csrf_field(csrf)}
<div class="field"><label for="title">Title</label>
<input id="title" name="title" required value="{v('title')}"></div>
<div class="field"><label for="objective">Objective</label>
<textarea id="objective" name="objective" required rows="4">{v('objective')}</textarea></div>
<div class="field"><label for="acceptance">Acceptance criteria (one per line)</label>
<textarea id="acceptance" name="acceptance" rows="4">{v('acceptance')}</textarea></div>
<div class="field"><label for="boundaries">Boundaries (one per line)</label>
<textarea id="boundaries" name="boundaries" rows="3">{v('boundaries')}</textarea></div>
<div class="field"><label for="priority">Priority</label>
<input id="priority" name="priority" type="number" value="{v('priority') or 100}"></div>
<div class="field"><label for="budget_turns">Turn budget</label>
<input id="budget_turns" name="budget_turns" type="number" value="{v('budget_turns') or 6}"></div>
<div class="field"><label for="after">Depends on (task ids, one per line)</label>
<textarea id="after" name="after" rows="2">{v('after')}</textarea></div>
<button type="submit">Create task</button>
</form>"""
    return _page("New task", "projects", body)


def _task_import_page(project: dict, csrf: str, error: str, raw: str) -> str:
    body = f"""
{_flash('error', error)}
<h2>Import task JSON into {_e(project["title"])}</h2>
<p>Paste one task spec object, or a JSON list of task specs (the same shape as
<code>nc task --file</code>).</p>
<form method="post" enctype="multipart/form-data" action="/p/{_segment(project["id"])}/tasks/import">
{_csrf_field(csrf)}
<div class="field"><label for="spec">Task spec JSON</label>
<textarea id="spec" name="spec" rows="12">{_e(raw)}</textarea></div>
<div class="field"><label for="upload">Or upload UTF-8 JSON (1 MiB form limit)</label>
<input id="upload" name="upload" type="file" accept=".json,application/json"></div>
<button type="submit">Import</button>
</form>"""
    return _page("Import tasks", "projects", body)


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _task_detail_page(state: State, cfg: Config, task: dict, csrf: str,
                      flash: str, error: str) -> str:
    unmet = task["unmet_dependencies"]
    depends = ""
    if task["depends_on"]:
        status = " (all accepted)" if not unmet else f" (waiting for {_e(', '.join(unmet))})"
        depends = f"<p>Depends on: {_e(', '.join(task['depends_on']))}{status}</p>"
    for dep in task["cancelled_dependencies"]:
        depends += (f'<p role="alert">{_e(dep)}: cancelled; dependency remains unmet '
                    f'(<a href="/t/{_segment(dep)}">inspect</a>)</p>')

    criteria = "".join(f"<li><pre>{_e(c)}</pre></li>" for c in task["acceptance"]) or "<li>(none)</li>"
    runs = "".join(
        f"<li>#{r['id']} agent={_e(r['agent_id'])} role={_e(r['role'])} "
        f"outcome={_e(r['outcome'] or 'running')} log={_e(r['log_path'] or '(none)')}</li>"
        for r in task["runs"]
    ) or "<li>(none)</li>"
    messages = "".join(
        f"<li>#{m['id']} [{_e(m['kind'])}] {_e(m['sender'])} -&gt; {_e(m['recipient'])}: "
        f"<pre>{_e(m['payload'])}</pre></li>"
        for m in task["messages"]
    ) or "<li>(none)</li>"
    check_output = (f"<pre>{_e(task['check_output'])}</pre>" if task["check_output"] is not None
                    else "<p>(no stored check output)</p>")

    actions = []
    if task["status"] not in ("done", "cancelled"):
        actions.append(f"""
<details><summary>Cancel this task</summary>
<form method="post" action="/t/{_segment(task["id"])}/cancel">
{_csrf_field(csrf)}
<div class="field"><label for="cancel_reason">Reason</label>
<input id="cancel_reason" name="reason" required></div>
<button type="submit">Cancel task</button>
</form></details>""")
    if task["status"] != "done":
        try:
            preview = operations.discard_preview(cfg, state, task["id"])
            discard = (f'<pre>{_e(json.dumps(preview, indent=2))}</pre>'
                       f'<input type="hidden" name="expected_discard" value="{preview["token"]}">')
        except ValueError as exc:
            discard = f"<p>{_e(str(exc))}</p>"
        actions.append(f"""
<details><summary>Requeue this task</summary>
<form method="post" action="/t/{_segment(task["id"])}/requeue">
{_csrf_field(csrf)}
<div class="field"><label for="requeue_reason">Reason (optional)</label>
<input id="requeue_reason" name="reason"></div>
<div class="field"><label for="budget">New turn budget (optional)</label>
<input id="budget" name="budget" type="number" min="1"></div>
<div class="field checkbox"><input id="fresh" name="fresh" type="checkbox" value="1">
<label for="fresh">Confirm discarding the branch and worktree shown below</label></div>
{discard}
<button type="submit">Requeue</button>
</form></details>""")
    if task["status"] == "done" and task["merge_commit"]:
        actions.append(f"""
<details><summary>Roll back this accepted task</summary>
<form method="post" action="/t/{_segment(task["id"])}/rollback">
{_csrf_field(csrf)}
<input type="hidden" name="expected_commit" value="{_e(task["merge_commit"])}">
<p>Reverts merge commit <code>{_e(task["merge_commit"])}</code> and opens an incident.</p>
<button type="submit">Roll back</button>
</form></details>""")

    body = f"""
{_flash('ok', flash)}{_flash('error', error)}
<h2>{_e(task["id"])}: {_e(task["title"])}</h2>
<p>status: <strong>{_e(task["status"])}</strong> ·
<a href="/p/{_segment(task["project_id"])}/tasks">back to tasks</a></p>
{depends}
<h3>Objective</h3>
<pre>{_e(task["objective"])}</pre>
<h3>Acceptance criteria</h3>
<ul>{criteria}</ul>
<h3>Boundaries</h3>
<pre>{_e(chr(10).join(task["boundaries"]))}</pre>
<h3>Result</h3>
<pre>{_e(task["result"] or "(none)")}</pre>
<h3>Runs</h3>
<ul>{runs}</ul>
<h3>Messages</h3>
<ul>{messages}</ul>
<h3>Acceptance check output</h3>
{check_output}
<h3>Actions</h3>
{''.join(actions) or '<p>No actions available for a cancelled task.</p>'}"""
    return _page(f"{task['id']}", "projects", body)


def _runs_page(rows: list[dict], csrf: str, flash: str, error: str) -> str:
    entries = "".join(
        f'<li><label><input type="checkbox" name="run_id_{r["id"]}" value="1"> '
        f'#{r["id"]} agent={_e(r["agent_id"])} task={_e(r["task_or_role"])} '
        f'role={_e(r["role"])} started={_e(r["started_at"])} '
        f'ownership: {_e(r["ownership"])} '
        f'<a href="/runs/{r["id"]}">recover individually</a></label></li>' for r in rows
    ) or "<li>(none)</li>"
    body = f"""{_flash('ok', flash)}{_flash('error', error)}
<h2>Unfinished run records</h2><p>An unfinished record is not proof of a live process. Verify scheduler and adapter descendants before recovering uncertain legacy ownership; inactive systemd alone is insufficient.</p>
<form method="post" action="/runs/recover">{_csrf_field(csrf)}
<ul>{entries}</ul>
<div class="field"><label for="reason">Recovery reason</label><input id="reason" name="reason" required></div>
<div class="field checkbox"><input id="quiescent" name="acknowledge_quiescence" value="1" type="checkbox"><label for="quiescent">I verified scheduler and adapter processes are quiescent</label></div>
<button type="submit">Record interruption for selected runs</button></form>"""
    return _page("Unfinished runs", "runs", body)


def _recover_run_page(row: dict, csrf: str, flash: str, error: str) -> str:
    body = f"""{_flash('ok', flash)}{_flash('error', error)}<h2>Recover run #{row['id']}</h2>
<p>agent={_e(row['agent_id'])}; task={_e(row['task_or_role'])}; ownership: {_e(row['ownership'])}</p>
<form method="post" action="/runs/{row['id']}/recover">{_csrf_field(csrf)}
<div class="field"><label for="reason">Recovery reason</label><input id="reason" name="reason" required></div>
<div class="field checkbox"><input id="quiescent" name="acknowledge_quiescence" value="1" type="checkbox"><label for="quiescent">I verified scheduler and adapter processes are quiescent</label></div><button type="submit">Record interruption</button></form>"""
    return _page("Recover run", "runs", body)


# --- proposals ----------------------------------------------------------

def _proposal_list_page(state: State, project: dict, flash: str, error: str) -> str:
    rows = [p for p in operations.proposals(state) if p["project_id"] == project["id"]]
    items = "".join(
        f'<li><a href="/proposals/{p["id"]}">#{p["id"]}</a> {_e(p["status"])} '
        f'tasks={len(p["spec"])} source={_e(p["source"])} — {_e(p["rationale"])}'
        + ("".join(f'<br><span class="muted">finding: {_e(f)}</span>' for f in p["findings"]))
        + "</li>"
        for p in rows
    ) or "<li>(no proposals)</li>"
    body = f"""
{_flash('ok', flash)}{_flash('error', error)}
<h2>{_e(project["title"])} proposals</h2>
<p><a href="/p/{_segment(project["id"])}/tasks">back to tasks</a></p>
<ul class="list">{items}</ul>"""
    return _page("Proposals", "projects", body)


def _proposal_detail_page(state: State, detail: dict, csrf: str, error: str, flash: str = "") -> str:
    spec_json = json.dumps(detail["spec"], indent=2, ensure_ascii=False)
    findings = "".join(f"<li>{_e(f)}</li>" for f in detail["findings"])
    revisions = "".join(
        f"<li>revision via feedback: {_e(r['feedback'].get('text', ''))}"
        + (f' (replaced by <a href="/proposals/{r["replacement_id"]}">#{r["replacement_id"]}</a>)</li>' if r["replacement_id"]
           else " (pending)</li>")
        for r in detail["revisions"]
    )
    review = ""
    if detail["plan_review"]:
        pr = detail["plan_review"]
        review = (f"<h3>Advisory plan review</h3><p>status: {_e(pr['status'])}, "
                  f"recommendation: {_e(pr['recommendation'])}</p>"
                  + "".join(f"<li>{_e(f)}</li>" for f in pr["findings"]))
    actions = ""
    if detail["status"] == "pending":
        actions = f"""
<details><summary>Approve</summary>
<form method="post" action="/proposals/{detail['id']}/approve">
{_csrf_field(csrf)}
<div class="field checkbox"><input id="force" name="force" type="checkbox" value="1">
<label for="force">Override findings (force)</label></div>
<button type="submit">Approve proposal</button>
</form></details>
<details><summary>Reject</summary>
<form method="post" action="/proposals/{detail['id']}/reject">
{_csrf_field(csrf)}
<div class="field"><label for="reject_reason">Reason</label>
<input id="reject_reason" name="reason" required></div>
<button type="submit">Reject proposal</button>
</form></details>"""
    body = f"""
{_flash('ok', flash)}{_flash('error', error)}
<h2>Proposal #{detail['id']} ({_e(detail['status'])})</h2>
<p><a href="/p/{_segment(detail['project_id'])}/proposals">back to proposals</a></p>
<p>{_e(detail['rationale'])}</p>
<h3>Findings</h3>
<ul>{findings or '<li>(none)</li>'}</ul>
{review}
<h3>Task specs</h3>
<pre>{_e(spec_json)}</pre>
<h3>Revisions</h3>
<ul>{revisions or '<li>(none)</li>'}</ul>
<h3>Decide</h3>
{actions or '<p>Already decided.</p>'}"""
    return _page(f"Proposal #{detail['id']}", "projects", body)


# --- feedback -------------------------------------------------------------

def _feedback_target(project: dict, item: dict) -> str:
    target = item["target"]
    if target["kind"] == "task":
        return f'<a href="/t/{_segment(target["id"])}">task {_e(target["id"])}</a>'
    if target["kind"] == "proposal":
        link = f'<a href="/proposals/{target["id"]}">proposal #{target["id"]}</a>'
        if target.get("replacement_id"):
            link += f' → <a href="/proposals/{target["replacement_id"]}">replacement #{target["replacement_id"]}</a>'
        return link
    return f'<a href="/p/{_segment(project["id"])}/tasks">project {_e(project["id"])}</a>'


def _project_question_item(project: dict, item: dict, csrf: str) -> str:
    if item["task_id"]:
        source = _feedback_target(project, {"target": {"kind": "task", "id": item["task_id"]}})
    else:
        source = "project planner"
    action = _answer_form(item, csrf) if item["answerable"] else '<span class="muted">answered</span>'
    return (f'<li>#{item["id"]} from {_e(item["sender"])} ({source})'
            f'<p>{_e(item["text"])}</p>{action}</li>')


def _feedback_page(state: State, project: dict, csrf: str, flash: str, error: str) -> str:
    history = operations.feedback_history(state, project["id"])
    feedback_items = "".join(
        f'<li>#{item["id"]} {"plan request" if item["payload"].get("request") == "plan" else "feedback"} '
        f'to {_feedback_target(project, item)} — '
        f'<strong>{"delivered to planner" if item["delivered"] else "waiting for planner"}</strong>'
        f'<pre>{_e(item["payload"].get("text", ""))}</pre></li>'
        for item in history
    ) or "<li>(no feedback yet)</li>"
    questions = operations.project_owner_questions(state, project["id"])
    question_items = "".join(_project_question_item(project, item, csrf) for item in questions)
    question_items = question_items or "<li>(no owner questions)</li>"
    body = f"""
{_flash('ok', flash)}{_flash('error', error)}
<h2>Feedback / plan for {_e(project["title"])}</h2>
<p><a href="/p/{_segment(project["id"])}/tasks">back to tasks</a></p>
<p>Delivery means the planner received the message; it does not mean work was implemented.
Planning remains a proposal for owner approval.</p>
<form method="post" action="/p/{_segment(project["id"])}/feedback">
{_csrf_field(csrf)}
<div class="field"><label for="text">Message to the project planner</label>
<textarea id="text" name="text" required rows="5"></textarea></div>
<div class="field"><label for="task">Attach to task id (optional)</label>
<input id="task" name="task"></div>
<div class="field"><label for="proposal">Attach to proposal id (optional, revises it)</label>
<input id="proposal" name="proposal" type="number"></div>
<p class="muted">Submitting feedback for a pending proposal immediately supersedes it before
the planner prepares a replacement. Inspect it first; original and replacement details remain linked.</p>
<button type="submit">Send feedback</button>
</form>
<h3>Request a planning pass</h3>
<form method="post" action="/p/{_segment(project["id"])}/plan">
{_csrf_field(csrf)}
<div class="field"><label for="note">Planning note (optional)</label>
<textarea id="note" name="note" rows="3"></textarea></div>
<button type="submit">Request planning</button>
</form>
<h3>Feedback history</h3><ul class="list">{feedback_items}</ul>
<h3>Owner questions for this project</h3><ul class="list">{question_items}</ul>"""
    return _page("Feedback", "projects", body)


# --- inbox ------------------------------------------------------------------

def _answer_form(m: dict, csrf: str) -> str:
    return f"""<details><summary>Answer</summary>
<form method="post" action="/messages/{m['id']}/answer">
{_csrf_field(csrf)}
<div class="field"><label for="answer-{m['id']}">Answer</label>
<textarea id="answer-{m['id']}" name="text" required rows="3"></textarea></div>
<button type="submit">Send answer</button>
</form></details>"""


def _inbox_page(state: State, csrf: str, include_delivered: bool, flash: str, error: str) -> str:
    rows = operations.inbox(state, include_delivered)
    items = "".join(f"""
<li>#{m['id']} [{_e(m['kind'])}] from {_e(m['sender'])}
({_e(m['task_id'] or '-')}, {_e(operations.age(m['created_at']))} ago)
<p>{_e(m['text'])}</p>
{_answer_form(m, csrf) if m["answerable"] else ""}</li>"""
        for m in rows) or "<li>(no pending messages)</li>"
    toggle_href = "/inbox" + ("" if include_delivered else "?all=1")
    toggle_label = "Hide answered messages" if include_delivered else "Show answered messages"
    body = f"""
{_flash('ok', flash)}{_flash('error', error)}
<h2>Inbox</h2>
<p><a href="{toggle_href}">{toggle_label}</a></p>
<ul class="list">{items}</ul>"""
    return _page("Inbox", "inbox", body)


# --- request handling ---------------------------------------------------

class ContentionError(Exception):
    """The request-scoped connection could not get a lock in time."""


def _is_contention(exc: sqlite3.OperationalError) -> bool:
    return "locked" in str(exc).lower() or "busy" in str(exc).lower()


_HOSTNAME_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)


def _concrete_host(value: str, option: str) -> str:
    """Validate a browser Host name, deliberately excluding IPv6 for now."""
    if not value or value != value.strip() or "://" in value or any(c in value for c in "/@?#"):
        raise ValueError(f"{option} must be a hostname or IPv4 address without scheme, path, or port")
    if any(c in value for c in ":[]"):
        raise ValueError(f"{option}: IPv6 is unsupported")
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        if not _HOSTNAME_RE.fullmatch(value):
            raise ValueError(f"{option} must be a hostname or IPv4 address without scheme, path, or port")
        return value
    if parsed.version != 4:
        raise ValueError(f"{option}: IPv6 is unsupported")
    if parsed.is_unspecified:
        raise ValueError(f"{option} must be a concrete browser hostname or IPv4 address")
    return value


def _validate_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be an integer from 0 through 65535")
    return port


def _allowed_hosts(port: int, configured: tuple[str, ...] = ()) -> set[str]:
    return {f"{host}:{port}" for host in configured}


Route = tuple[str, re.Pattern, Callable]
ROUTES: list[Route] = []


def route(method: str, pattern: str):
    compiled = re.compile(pattern)

    def register(func):
        ROUTES.append((method, compiled, func))
        return func
    return register


@route("GET", r"^/$")
def _root(h: Handler, state, params, query):
    h.redirect("/projects")


@route("GET", r"^/projects$")
def _view_projects(h: Handler, state, params, query):
    h.send_html(HTTPStatus.OK, _project_list_page(state))


def _require_project(state: State, project_id: str) -> dict:
    return operations.get_project(state, project_id)


@route("GET", r"^/p/(?P<project>[^/]+)/tasks$")
def _view_tasks(h: Handler, state, params, query):
    project = _require_project(state, params["project"])
    include_cancelled = query.get("all") == "1"
    h.send_html(HTTPStatus.OK, _task_list_page(
        state, project, include_cancelled, query.get("ok", ""), query.get("error", ""),
    ))


@route("GET", r"^/p/(?P<project>[^/]+)/tasks/new$")
def _view_task_new(h: Handler, state, params, query):
    project = _require_project(state, params["project"])
    h.send_html(HTTPStatus.OK, _task_new_page(project, h.csrf_token, "", {}))


@route("POST", r"^/p/(?P<project>[^/]+)/tasks/new$")
def _post_task_new(h: Handler, state, params, query, form):
    project = _require_project(state, params["project"])
    acceptance = _lines(form.get("acceptance", ""))
    boundaries = _lines(form.get("boundaries", ""))
    after = _lines(form.get("after", ""))
    try:
        priority = int(form.get("priority") or 100)
        budget_turns = int(form.get("budget_turns") or 6)
    except ValueError:
        h.send_html(HTTPStatus.BAD_REQUEST, _task_new_page(
            project, h.csrf_token, "priority and turn budget must be numbers", form,
        ))
        return
    try:
        if not form.get("title", "").strip() or not form.get("objective", "").strip():
            raise ValueError("title and objective are required")
        if budget_turns < 1:
            raise ValueError("turn budget must be greater than zero")
        tid = operations.create_task(state, project["id"], form.get("title", ""),
                                     form.get("objective", ""), acceptance, boundaries,
                                     priority, budget_turns, after)
    except ValueError as exc:
        h.send_html(HTTPStatus.BAD_REQUEST, _task_new_page(
            project, h.csrf_token, str(exc), form,
        ))
        return
    h.redirect(f"/t/{_segment(tid)}")


@route("GET", r"^/p/(?P<project>[^/]+)/tasks/import$")
def _view_task_import(h: Handler, state, params, query):
    project = _require_project(state, params["project"])
    h.send_html(HTTPStatus.OK, _task_import_page(project, h.csrf_token, "", ""))


@route("POST", r"^/p/(?P<project>[^/]+)/tasks/import$")
def _post_task_import(h: Handler, state, params, query, form):
    project = _require_project(state, params["project"])
    raw = form.get("spec", "")
    uploaded = form.get("upload", "")
    if raw.strip() and uploaded.strip():
        h.send_html(HTTPStatus.BAD_REQUEST, _task_import_page(
            project, h.csrf_token, "Choose pasted JSON or an upload, not both", raw,
        ))
        return
    raw = uploaded or raw
    try:
        specs = json.loads(raw)
    except json.JSONDecodeError as exc:
        h.send_html(HTTPStatus.BAD_REQUEST, _task_import_page(
            project, h.csrf_token, f"invalid JSON: {exc}", raw,
        ))
        return
    try:
        ids = operations.import_tasks(state, specs, project=project["id"])
    except (KeyError, TypeError, ValueError) as exc:
        h.send_html(HTTPStatus.BAD_REQUEST, _task_import_page(
            project, h.csrf_token, f"invalid task spec: {exc}", raw,
        ))
        return
    h.redirect(f"/p/{_segment(project['id'])}/tasks",
              ok=f"imported {len(ids)} task(s): {', '.join(ids)}")


@route("GET", r"^/t/(?P<task_id>[^/]+)$")
def _view_task(h: Handler, state, params, query):
    task = operations.task_detail(state, h.cfg, params["task_id"])
    h.send_html(HTTPStatus.OK, _task_detail_page(
        state, h.cfg, task, h.csrf_token, query.get("ok", ""), query.get("error", ""),
    ))


@route("GET", r"^/runs$")
def _view_runs(h: Handler, state, params, query):
    h.send_html(HTTPStatus.OK, _runs_page(operations.unfinished_runs(state), h.csrf_token,
                                           query.get("ok", ""), query.get("error", "")))


@route("GET", r"^/runs/(?P<run_id>[0-9]+)$")
def _view_recover_run(h: Handler, state, params, query):
    row = next((r for r in operations.unfinished_runs(state) if r["id"] == int(params["run_id"])), None)
    if row is None:
        raise LookupError(f"unknown or finished run: {params['run_id']}")
    h.send_html(HTTPStatus.OK, _recover_run_page(row, h.csrf_token, query.get("ok", ""), query.get("error", "")))


@route("POST", r"^/runs/(?P<run_id>[0-9]+)/recover$")
def _post_recover_run(h: Handler, state, params, query, form):
    run_id = int(params["run_id"])
    try:
        operations.recover_runs(state, [run_id], form.get("reason", ""),
                                form.get("acknowledge_quiescence") == "1")
    except (ValueError, LookupError) as exc:
        h.redirect(f"/runs/{run_id}", error=str(exc))
        return
    h.redirect("/runs", ok=f"recovered run ID: {run_id}")


def _selected_run_ids(form: dict[str, str]) -> list[int]:
    """Read the selected rows from the list form without weakening its validation.

    Named checkboxes retain every selected ID despite the deliberately simple
    one-value form parser.  ``run_ids`` is also accepted as a comma-separated
    value for scripted browser submissions; it makes duplicate/stale requests
    reach the same shared operation and validation as the CLI.
    """
    selected = []
    for name, value in form.items():
        match = re.fullmatch(r"run_id_([1-9][0-9]*)", name)
        if match and value == "1":
            selected.append(int(match.group(1)))
    supplied = form.get("run_ids", "")
    if supplied:
        try:
            selected.extend(int(value) for value in supplied.split(","))
        except ValueError as exc:
            raise ValueError("run IDs must be comma-separated positive integers") from exc
    return selected


@route("POST", r"^/runs/recover$")
def _post_recover_selected_runs(h: Handler, state, params, query, form):
    try:
        rows = operations.recover_runs(
            state, _selected_run_ids(form), form.get("reason", ""),
            form.get("acknowledge_quiescence") == "1",
        )
    except (ValueError, LookupError) as exc:
        h.redirect("/runs", error=str(exc))
        return
    h.redirect("/runs", ok="recovered interrupted run IDs: " +
               ", ".join(str(row["id"]) for row in rows))


@route("POST", r"^/t/(?P<task_id>[^/]+)/cancel$")
def _post_cancel(h: Handler, state, params, query, form):
    task_id = params["task_id"]
    try:
        operations.cancel_task(state, task_id, form.get("reason", ""))
    except (ValueError, LookupError) as exc:
        h.redirect(f"/t/{_segment(task_id)}", error=str(exc))
        return
    h.redirect(f"/t/{_segment(task_id)}", ok="task cancelled")


@route("POST", r"^/t/(?P<task_id>[^/]+)/requeue$")
def _post_requeue(h: Handler, state, params, query, form):
    task_id = params["task_id"]
    budget = form.get("budget") or ""
    try:
        budget_value = int(budget) if budget.strip() else None
    except ValueError:
        h.redirect(f"/t/{_segment(task_id)}", error="turn budget must be a number")
        return
    try:
        result = operations.requeue_task(h.cfg, state, task_id, form.get("fresh") == "1",
                                         budget_value, form.get("reason") or None,
                                         form.get("expected_discard"))
    except (ValueError, LookupError) as exc:
        h.redirect(f"/t/{_segment(task_id)}", error=str(exc))
        return
    ok = "queued again" + (" from a fresh branch" if result["fresh"] else "")
    h.redirect(f"/t/{_segment(task_id)}", ok=ok)


@route("POST", r"^/t/(?P<task_id>[^/]+)/rollback$")
def _post_rollback(h: Handler, state, params, query, form):
    task_id = params["task_id"]
    try:
        result = operations.rollback_task(state, task_id, form.get("expected_commit"))
    except (LookupError, ValueError) as exc:
        h.redirect(f"/t/{_segment(task_id)}", error=str(exc))
        return
    ok = f"reverted {result['reverted_commit']} in {result['commit']}"
    if result["mirror_error"]:
        ok += f"; mirror push failed: {result['mirror_error']}"
    h.redirect(f"/t/{_segment(task_id)}", ok=ok)


@route("GET", r"^/p/(?P<project>[^/]+)/proposals$")
def _view_proposals(h: Handler, state, params, query):
    project = _require_project(state, params["project"])
    h.send_html(HTTPStatus.OK, _proposal_list_page(
        state, project, query.get("ok", ""), query.get("error", ""),
    ))


@route("GET", r"^/proposals/(?P<proposal_id>\d+)$")
def _view_proposal(h: Handler, state, params, query):
    detail = operations.proposal_detail(state, int(params["proposal_id"]))
    h.send_html(HTTPStatus.OK, _proposal_detail_page(
        state, detail, h.csrf_token, query.get("error", ""), query.get("ok", ""),
    ))


@route("POST", r"^/proposals/(?P<proposal_id>\d+)/approve$")
def _post_approve(h: Handler, state, params, query, form):
    proposal_id = int(params["proposal_id"])
    try:
        result = operations.approve_proposal(state, proposal_id, form.get("force") == "1")
    except (ValueError, KeyError, TypeError, LookupError) as exc:
        h.redirect(f"/proposals/{proposal_id}", error=str(exc))
        return
    h.redirect(f"/proposals/{proposal_id}", ok=f"approved: {', '.join(result['task_ids'])}")


@route("POST", r"^/proposals/(?P<proposal_id>\d+)/reject$")
def _post_reject(h: Handler, state, params, query, form):
    proposal_id = int(params["proposal_id"])
    try:
        operations.reject_proposal(state, proposal_id, form.get("reason", ""))
    except (ValueError, LookupError) as exc:
        h.redirect(f"/proposals/{proposal_id}", error=str(exc))
        return
    h.redirect(f"/proposals/{proposal_id}", ok="rejected")


@route("GET", r"^/p/(?P<project>[^/]+)/feedback$")
def _view_feedback(h: Handler, state, params, query):
    project = _require_project(state, params["project"])
    h.send_html(HTTPStatus.OK, _feedback_page(
        state, project, h.csrf_token, query.get("ok", ""), query.get("error", ""),
    ))


@route("POST", r"^/p/(?P<project>[^/]+)/feedback$")
def _post_feedback(h: Handler, state, params, query, form):
    project = _require_project(state, params["project"])
    task = (form.get("task") or "").strip() or None
    proposal_raw = (form.get("proposal") or "").strip()
    try:
        proposal = int(proposal_raw) if proposal_raw else None
    except ValueError:
        h.redirect(f"/p/{_segment(project['id'])}/feedback",
                  error="proposal id must be a number")
        return
    try:
        agent_id, message_id = operations.submit_feedback(
            state, h.cfg, project["id"], form.get("text", ""), task, proposal,
        )
    except (ValueError, LookupError) as exc:
        h.redirect(f"/p/{_segment(project['id'])}/feedback", error=str(exc))
        return
    h.redirect(f"/p/{_segment(project['id'])}/feedback",
              ok=f"queued feedback #{message_id} for {agent_id}")


@route("POST", r"^/p/(?P<project>[^/]+)/plan$")
def _post_plan(h: Handler, state, params, query, form):
    project = _require_project(state, params["project"])
    try:
        agent_id, message_id = operations.request_plan(state, h.cfg, project["id"],
                                                       form.get("note") or None)
    except (ValueError, LookupError) as exc:
        h.redirect(f"/p/{_segment(project['id'])}/feedback", error=str(exc))
        return
    h.redirect(f"/p/{_segment(project['id'])}/feedback",
              ok=f"queued planning request #{message_id} for {agent_id}")


@route("GET", r"^/inbox$")
def _view_inbox(h: Handler, state, params, query):
    h.send_html(HTTPStatus.OK, _inbox_page(
        state, h.csrf_token, query.get("all") == "1", query.get("ok", ""), query.get("error", ""),
    ))


@route("POST", r"^/messages/(?P<message_id>\d+)/answer$")
def _post_answer(h: Handler, state, params, query, form):
    message_id = int(params["message_id"])
    try:
        operations.answer_message(state, message_id, form.get("text", ""))
    except (ValueError, LookupError) as exc:
        h.redirect("/inbox", error=str(exc))
        return
    h.redirect("/inbox", ok="answered")


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "neocortex-ui/1"
    # StreamRequestHandler.setup applies this before reading the request line
    # or headers, so idle browser preconnections cannot stall the server forever.
    timeout = 5.0

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)

    @property
    def cfg(self) -> Config:
        return self.server.cfg  # type: ignore[attr-defined]

    # -- dispatch -----------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        self.session_is_new = False
        parsed = urllib.parse.urlsplit(self.path)
        # Match encoded segments before decoding identifiers that may contain '/'.
        path = parsed.path

        if not self._valid_host():
            self.send_response(HTTPStatus.BAD_REQUEST)
            self._end(_error_page(HTTPStatus.BAD_REQUEST, "invalid or missing Host header"))
            return

        if method == "GET" and path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
            return

        query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        match = None
        view = None
        for candidate_method, pattern, candidate_view in ROUTES:
            if candidate_method != method:
                continue
            m = pattern.match(path)
            if m:
                match, view = m, candidate_view
                break
        if view is None:
            self.send_response(HTTPStatus.NOT_FOUND)
            self._end(_error_page(HTTPStatus.NOT_FOUND, f"no such page: {path}"))
            return

        self.session_id, self.session_is_new = self._session()
        self.csrf_token = self.server.sessions[self.session_id]  # type: ignore[attr-defined]

        form: dict[str, str] = {}
        if method == "POST":
            if not self._valid_origin():
                self.send_response(HTTPStatus.FORBIDDEN)
                self._end(_error_page(HTTPStatus.FORBIDDEN, "invalid or missing Origin header"))
                return
            try:
                form = self._read_form()
            except (ValueError, UnicodeError) as exc:
                self.send_response(HTTPStatus.BAD_REQUEST)
                self._end(_error_page(HTTPStatus.BAD_REQUEST, str(exc)))
                return
            if form.get("csrf_token") != self.csrf_token:
                self.send_response(HTTPStatus.FORBIDDEN)
                self._end(_error_page(
                    HTTPStatus.FORBIDDEN, "missing or invalid CSRF token; reload the page and retry",
                ))
                return

        if not self.cfg.db_path.exists():
            self.send_response(HTTPStatus.BAD_REQUEST)
            self._end(_error_page(HTTPStatus.BAD_REQUEST, "run `nc init` first"))
            return

        try:
            state = State(self.cfg.db_path, initialize=False, timeout=self.server.db_timeout)  # type: ignore[attr-defined]
        except sqlite3.OperationalError as exc:
            self._contention(exc)
            return

        try:
            params = {key: urllib.parse.unquote(value) for key, value in match.groupdict().items()}
            args = (self, state, params, query)
            if method == "POST":
                args = (*args, form)
            view(*args)
        except LookupError as exc:
            self.send_response(HTTPStatus.NOT_FOUND)
            self._end(_error_page(HTTPStatus.NOT_FOUND, str(exc)))
        except sqlite3.OperationalError as exc:
            self._contention(exc)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            self.send_response(HTTPStatus.CONFLICT)
            self._end(_error_page(HTTPStatus.CONFLICT, str(exc)))
        except ValueError as exc:
            self.send_response(HTTPStatus.BAD_REQUEST)
            self._end(_error_page(HTTPStatus.BAD_REQUEST, str(exc)))
        finally:
            state.db.close()

    def _contention(self, exc: sqlite3.OperationalError) -> None:
        if _is_contention(exc):
            self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
            self.send_header("Retry-After", "1")
            self._end(_error_page(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "the database is busy with another request; try again in a moment",
            ))
        else:
            self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
            self._end(_error_page(HTTPStatus.INTERNAL_SERVER_ERROR, "database error"))

    # -- responses ------------------------------------------------------

    def send_html(self, status: HTTPStatus, body: str) -> None:
        self.send_response(status)
        self._end(body.encode("utf-8"))

    def redirect(self, location: str, ok: str = "", error: str = "") -> None:
        qs = {}
        if ok:
            qs["ok"] = ok
        if error:
            qs["error"] = error
        if qs:
            location += "?" + urllib.parse.urlencode(qs)
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self._end(b"")

    def _end(self, body: bytes) -> None:
        if body:
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if getattr(self, "session_is_new", False):
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={self.session_id}; Path=/; HttpOnly; SameSite=Strict",
            )
        self.end_headers()
        if body:
            self.wfile.write(body)

    # -- security checks --------------------------------------------------

    def _valid_host(self) -> bool:
        if len(self.headers.get_all("Host", [])) != 1:
            return False
        host = self.headers.get("Host", "")
        return host in self.server.allowed_hosts  # type: ignore[attr-defined]

    def _valid_origin(self) -> bool:
        if len(self.headers.get_all("Origin", [])) != 1:
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return False
        try:
            parsed = urllib.parse.urlsplit(origin)
        except ValueError:
            return False
        if parsed.scheme != "http":
            return False
        return origin == "http://" + self.headers.get("Host", "")

    def _session(self) -> tuple[str, bool]:
        cookie_header = self.headers.get("Cookie", "")
        session_id = None
        for part in cookie_header.split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE and value:
                session_id = value
                break
        sessions: dict[str, str] = self.server.sessions  # type: ignore[attr-defined]
        if session_id and session_id in sessions:
            return session_id, False
        new_id = secrets.token_urlsafe(24)
        sessions[new_id] = secrets.token_urlsafe(24)
        return new_id, True

    def _read_form(self) -> dict[str, str]:
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("transfer encoding is unsupported")
        if len(self.headers.get_all("Content-Length", [])) != 1:
            raise ValueError("one Content-Length header is required")
        content_type = self.headers.get_content_type()
        if content_type not in ("application/x-www-form-urlencoded", "multipart/form-data"):
            raise ValueError("expected a URL-encoded or multipart form")
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 <= length <= 1024 * 1024:
            raise ValueError("form exceeds the 1 MiB limit")
        raw = self.rfile.read(length) if length else b""
        if content_type == "multipart/form-data":
            message = BytesParser(policy=policy.default).parsebytes(
                ("Content-Type: " + self.headers["Content-Type"] + "\r\n\r\n").encode()
                + raw,
            )
            if not message.is_multipart() or message.defects:
                raise ValueError("invalid multipart form")
            form = {}
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if not name or name in form or part.is_multipart() or part.defects:
                    raise ValueError("invalid or duplicate multipart field")
                # Filenames are untrusted metadata, never filesystem paths.
                form[name] = (part.get_payload(decode=True) or b"").decode("utf-8")
            return form
        parsed = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        return {k: v[-1] for k, v in parsed.items()}

    def _serve_static(self, name: str) -> None:
        safe = Path(name).name
        path = STATIC_DIR / safe
        if safe != name or not path.is_file():
            self.send_response(HTTPStatus.NOT_FOUND)
            self._end(_error_page(HTTPStatus.NOT_FOUND, "no such asset"))
            return
        content_type = "text/css" if path.suffix == ".css" else "application/octet-stream"
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


class Server(http.server.HTTPServer):
    cfg: Config
    sessions: dict[str, str]
    db_timeout: float
    allowed_hosts: set[str]


def make_server(
    cfg: Config, port: int, db_timeout: float = REQUEST_TIMEOUT_S,
    host: str = "127.0.0.1", allowed_hosts: tuple[str, ...] = (),
) -> Server:
    """Create an IPv4 server; network exposure is explicit and host-restricted.

    ``db_timeout`` remains the third positional argument for existing callers.
    IPv6 is rejected rather than falling through to an unintended address family.
    """
    port = _validate_port(port)
    bind_host = _concrete_host(host, "--host") if host != "0.0.0.0" else host
    configured = tuple(_concrete_host(value, "--allowed-host") for value in allowed_hosts)
    if bind_host == "0.0.0.0" and not configured:
        raise ValueError("--host 0.0.0.0 requires at least one --allowed-host")
    # Resolve before constructing HTTPServer so hostname failures are concise and
    # never allow IPv6 to be selected by the platform resolver.
    try:
        resolved = socket.getaddrinfo(bind_host, port, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OSError(f"cannot resolve UI host {bind_host!r}: {exc}") from exc
    if not resolved:
        raise OSError(f"cannot resolve UI host {bind_host!r}")
    server = Server((resolved[0][4][0], port), Handler)
    server.cfg = cfg
    server.sessions = {}
    server.db_timeout = db_timeout
    access_hosts = (("127.0.0.1", "localhost", *configured) if bind_host == "127.0.0.1"
                    else configured + (() if bind_host == "0.0.0.0" else (bind_host,)))
    server.allowed_hosts = _allowed_hosts(server.server_port, access_hosts)
    return server


def serve(
    cfg: Config, port: int, host: str = "127.0.0.1", allowed_hosts: tuple[str, ...] = (),
) -> None:
    # Keep the historic two-argument construction path intact for embedders
    # which wrap ``make_server`` to run a short-lived local server in tests.
    httpd = (make_server(cfg, port) if host == "127.0.0.1" and not allowed_hosts
             else make_server(cfg, port, host=host, allowed_hosts=allowed_hosts))
    destinations = sorted(http_host.rsplit(":", 1)[0] for http_host in httpd.allowed_hosts)
    print(
        f"serving on {host}:{httpd.server_port}; browser Host values: "
        f"{', '.join(destinations)} (Ctrl+C to stop)",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
