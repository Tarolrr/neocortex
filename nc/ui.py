"""Local browser UI: a loopback-only, server-rendered owner console.

Phase one deliberately stays small:

- No frontend build step. Pages are rendered by plain Python string templates
  (this module) and the only packaged asset is a static stylesheet
  (`nc/static/style.css`), served straight off disk.
- Every GET is read-only. Every mutation is a POST, guarded by a per-session
  CSRF token and Host/Origin checks, so a page loaded from another origin (or
  an image/link embedded on some other site) cannot drive a change here.
- Every request opens and closes its own `State` (its own SQLite connection);
  nothing is held across requests. See `docs/ui-access.md` for local and
  SSH-tunnel access, and `docs/follow-ups.md` for what phase one defers
  (scheduler administration, incidents, project administration).

This module never shells out and never starts an agent turn or session: it
only calls `nc.operations`, `nc.state.State` and `nc.arbiter`, the same
building blocks the CLI uses.
"""

from __future__ import annotations

import html
import http.server
import json
import logging
import re
import secrets
import sqlite3
import urllib.parse
from collections.abc import Callable
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


# TODO(FU-001, FU-002): scheduler/incident administration remains CLI-only;
# see docs/follow-ups.md before adding navigation and mutation routes.
def _nav(active: str) -> str:
    items = [
        ("projects", "/projects", "Projects"),
        ("inbox", "/inbox", "Inbox"),
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
            f'<li><a href="/p/{_e(row["id"])}/tasks">{_e(row["title"])}</a> '
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
        f'<tr><td><a href="/t/{_e(row["id"])}">{_e(row["id"])}</a></td>'
        f'<td>{_e(row["status"])}</td><td>{row["attempts"]}</td>'
        f'<td>{_e(row["title"])}{waiting}</td></tr>'
    )


def _task_list_page(state: State, project: dict, include_cancelled: bool, flash: str, error: str) -> str:
    rows = operations.tasks(state, project["id"], include_cancelled)
    table = "".join(_task_row(r) for r in rows) or '<tr><td colspan="4">(no tasks)</td></tr>'
    toggle_href = f'/p/{_e(project["id"])}/tasks' + ("" if include_cancelled else "?all=1")
    toggle_label = "Hide cancelled tasks" if include_cancelled else "Show cancelled tasks"
    body = f"""
{_flash('ok', flash)}{_flash('error', error)}
<h2>{_e(project["title"])} tasks</h2>
<p>
<a href="/p/{_e(project["id"])}/tasks/new">New task</a> ·
<a href="/p/{_e(project["id"])}/tasks/import">Import JSON</a> ·
<a href="/p/{_e(project["id"])}/proposals">Proposals</a> ·
<a href="/p/{_e(project["id"])}/feedback">Feedback / plan</a> ·
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
<form method="post" action="/p/{_e(project["id"])}/tasks/new">
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
<form method="post" action="/p/{_e(project["id"])}/tasks/import">
{_csrf_field(csrf)}
<div class="field"><label for="spec">Task spec JSON</label>
<textarea id="spec" name="spec" required rows="12">{_e(raw)}</textarea></div>
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
                    f'(<a href="/t/{_e(dep)}">inspect</a>)</p>')

    criteria = "".join(f"<li>{_e(c)}</li>" for c in task["acceptance"]) or "<li>(none)</li>"
    runs = "".join(
        f"<li>#{r['id']} agent={_e(r['agent_id'])} role={_e(r['role'])} "
        f"outcome={_e(r['outcome'] or 'running')} log={_e(r['log_path'] or '(none)')}</li>"
        for r in task["runs"]
    ) or "<li>(none)</li>"
    messages = "".join(
        f"<li>#{m['id']} [{_e(m['kind'])}] {_e(m['sender'])} -&gt; {_e(m['recipient'])}: "
        f"{_e(m['payload'])}</li>"
        for m in task["messages"]
    ) or "<li>(none)</li>"
    check_output = (f"<pre>{_e(task['check_output'])}</pre>" if task["check_output"] is not None
                    else "<p>(no stored check output)</p>")

    actions = []
    if task["status"] not in ("done", "cancelled"):
        actions.append(f"""
<details><summary>Cancel this task</summary>
<form method="post" action="/t/{_e(task["id"])}/cancel">
{_csrf_field(csrf)}
<div class="field"><label for="cancel_reason">Reason</label>
<input id="cancel_reason" name="reason" required></div>
<button type="submit">Cancel task</button>
</form></details>""")
    if task["status"] != "done":
        actions.append(f"""
<details><summary>Requeue this task</summary>
<form method="post" action="/t/{_e(task["id"])}/requeue">
{_csrf_field(csrf)}
<div class="field"><label for="requeue_reason">Reason (optional)</label>
<input id="requeue_reason" name="reason"></div>
<div class="field"><label for="budget">New turn budget (optional)</label>
<input id="budget" name="budget" type="number"></div>
<div class="field checkbox"><input id="fresh" name="fresh" type="checkbox" value="1">
<label for="fresh">Start from a fresh branch (discard worktree and branch)</label></div>
<button type="submit">Requeue</button>
</form></details>""")
    if task["status"] == "done" and task["merge_commit"]:
        actions.append(f"""
<details><summary>Roll back this accepted task</summary>
<form method="post" action="/t/{_e(task["id"])}/rollback">
{_csrf_field(csrf)}
<p>Reverts merge commit <code>{_e(task["merge_commit"])}</code> and opens an incident.</p>
<button type="submit">Roll back</button>
</form></details>""")

    body = f"""
{_flash('ok', flash)}{_flash('error', error)}
<h2>{_e(task["id"])}: {_e(task["title"])}</h2>
<p>status: <strong>{_e(task["status"])}</strong> ·
<a href="/p/{_e(task["project_id"])}/tasks">back to tasks</a></p>
{depends}
<h3>Objective</h3>
<pre>{_e(task["objective"])}</pre>
<h3>Acceptance criteria</h3>
<ul>{criteria}</ul>
<h3>Runs</h3>
<ul>{runs}</ul>
<h3>Messages</h3>
<ul>{messages}</ul>
<h3>Acceptance check output</h3>
{check_output}
<h3>Actions</h3>
{''.join(actions) or '<p>No actions available for a cancelled task.</p>'}"""
    return _page(f"{task['id']}", "projects", body)


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
<p><a href="/p/{_e(project["id"])}/tasks">back to tasks</a></p>
<ul class="list">{items}</ul>"""
    return _page("Proposals", "projects", body)


def _proposal_detail_page(state: State, detail: dict, csrf: str, error: str) -> str:
    spec_json = json.dumps(detail["spec"], indent=2, ensure_ascii=False)
    findings = "".join(f"<li>{_e(f)}</li>" for f in detail["findings"])
    revisions = "".join(
        f"<li>revision via feedback: {_e(r['feedback'].get('text', ''))}"
        + (f" (replaced by #{r['replacement_id']})</li>" if r["replacement_id"]
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
{_flash('error', error)}
<h2>Proposal #{detail['id']} ({_e(detail['status'])})</h2>
<p><a href="/p/{_e(detail['project_id'])}/proposals">back to proposals</a></p>
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

def _feedback_page(project: dict, csrf: str, flash: str, error: str) -> str:
    body = f"""
{_flash('ok', flash)}{_flash('error', error)}
<h2>Feedback / plan for {_e(project["title"])}</h2>
<p><a href="/p/{_e(project["id"])}/tasks">back to tasks</a></p>
<form method="post" action="/p/{_e(project["id"])}/feedback">
{_csrf_field(csrf)}
<div class="field"><label for="text">Message to the project planner</label>
<textarea id="text" name="text" required rows="5"></textarea></div>
<div class="field"><label for="task">Attach to task id (optional)</label>
<input id="task" name="task"></div>
<div class="field"><label for="proposal">Attach to proposal id (optional, revises it)</label>
<input id="proposal" name="proposal" type="number"></div>
<button type="submit">Send feedback</button>
</form>"""
    return _page("Feedback", "projects", body)


# --- inbox ------------------------------------------------------------------

def _inbox_page(state: State, csrf: str, include_delivered: bool, flash: str, error: str) -> str:
    rows = operations.inbox(state, include_delivered)
    items = "".join(f"""
<li>#{m['id']} [{_e(m['kind'])}] from {_e(m['sender'])}
({_e(m['task_id'] or '-')}, {_e(operations.age(m['created_at']))} ago)
<p>{_e(m['text'])}</p>
<details><summary>Answer</summary>
<form method="post" action="/messages/{m['id']}/answer">
{_csrf_field(csrf)}
<div class="field"><label for="answer-{m['id']}">Answer</label>
<textarea id="answer-{m['id']}" name="text" required rows="3"></textarea></div>
<button type="submit">Send answer</button>
</form></details></li>"""
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


def _allowed_hosts(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}"}


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


@route("GET", r"^/p/(?P<project>[\w.-]+)/tasks$")
def _view_tasks(h: Handler, state, params, query):
    project = _require_project(state, params["project"])
    include_cancelled = query.get("all") == "1"
    h.send_html(HTTPStatus.OK, _task_list_page(
        state, project, include_cancelled, query.get("ok", ""), query.get("error", ""),
    ))


@route("GET", r"^/p/(?P<project>[\w.-]+)/tasks/new$")
def _view_task_new(h: Handler, state, params, query):
    project = _require_project(state, params["project"])
    h.send_html(HTTPStatus.OK, _task_new_page(project, h.csrf_token, "", {}))


@route("POST", r"^/p/(?P<project>[\w.-]+)/tasks/new$")
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
    h.redirect(f"/t/{urllib.parse.quote(tid)}")


@route("GET", r"^/p/(?P<project>[\w.-]+)/tasks/import$")
def _view_task_import(h: Handler, state, params, query):
    project = _require_project(state, params["project"])
    h.send_html(HTTPStatus.OK, _task_import_page(project, h.csrf_token, "", ""))


@route("POST", r"^/p/(?P<project>[\w.-]+)/tasks/import$")
def _post_task_import(h: Handler, state, params, query, form):
    project = _require_project(state, params["project"])
    raw = form.get("spec", "")
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
    h.redirect(f"/p/{urllib.parse.quote(project['id'])}/tasks",
              ok=f"imported {len(ids)} task(s): {', '.join(ids)}")


@route("GET", r"^/t/(?P<task_id>[\w.-]+)$")
def _view_task(h: Handler, state, params, query):
    task = operations.task_detail(state, h.cfg, params["task_id"])
    h.send_html(HTTPStatus.OK, _task_detail_page(
        state, h.cfg, task, h.csrf_token, query.get("ok", ""), query.get("error", ""),
    ))


@route("POST", r"^/t/(?P<task_id>[\w.-]+)/cancel$")
def _post_cancel(h: Handler, state, params, query, form):
    task_id = params["task_id"]
    try:
        operations.cancel_task(state, task_id, form.get("reason", ""))
    except (ValueError, LookupError) as exc:
        h.redirect(f"/t/{urllib.parse.quote(task_id)}", error=str(exc))
        return
    h.redirect(f"/t/{urllib.parse.quote(task_id)}", ok="task cancelled")


@route("POST", r"^/t/(?P<task_id>[\w.-]+)/requeue$")
def _post_requeue(h: Handler, state, params, query, form):
    task_id = params["task_id"]
    budget = form.get("budget") or ""
    try:
        budget_value = int(budget) if budget.strip() else None
    except ValueError:
        h.redirect(f"/t/{urllib.parse.quote(task_id)}", error="turn budget must be a number")
        return
    try:
        result = operations.requeue_task(h.cfg, state, task_id, form.get("fresh") == "1",
                                         budget_value, form.get("reason") or None)
    except (ValueError, LookupError) as exc:
        h.redirect(f"/t/{urllib.parse.quote(task_id)}", error=str(exc))
        return
    ok = "queued again" + (" from a fresh branch" if result["fresh"] else "")
    h.redirect(f"/t/{urllib.parse.quote(task_id)}", ok=ok)


@route("POST", r"^/t/(?P<task_id>[\w.-]+)/rollback$")
def _post_rollback(h: Handler, state, params, query, form):
    task_id = params["task_id"]
    try:
        result = operations.rollback_task(state, task_id)
    except LookupError as exc:
        h.redirect(f"/t/{urllib.parse.quote(task_id)}", error=str(exc))
        return
    ok = f"reverted {result['reverted_commit']} in {result['commit']}"
    if result["mirror_error"]:
        ok += f"; mirror push failed: {result['mirror_error']}"
    h.redirect(f"/t/{urllib.parse.quote(task_id)}", ok=ok)


@route("GET", r"^/p/(?P<project>[\w.-]+)/proposals$")
def _view_proposals(h: Handler, state, params, query):
    project = _require_project(state, params["project"])
    h.send_html(HTTPStatus.OK, _proposal_list_page(
        state, project, query.get("ok", ""), query.get("error", ""),
    ))


@route("GET", r"^/proposals/(?P<proposal_id>\d+)$")
def _view_proposal(h: Handler, state, params, query):
    detail = operations.proposal_detail(state, int(params["proposal_id"]))
    h.send_html(HTTPStatus.OK, _proposal_detail_page(
        state, detail, h.csrf_token, query.get("error", ""),
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


@route("GET", r"^/p/(?P<project>[\w.-]+)/feedback$")
def _view_feedback(h: Handler, state, params, query):
    project = _require_project(state, params["project"])
    h.send_html(HTTPStatus.OK, _feedback_page(
        project, h.csrf_token, query.get("ok", ""), query.get("error", ""),
    ))


@route("POST", r"^/p/(?P<project>[\w.-]+)/feedback$")
def _post_feedback(h: Handler, state, params, query, form):
    project = _require_project(state, params["project"])
    task = (form.get("task") or "").strip() or None
    proposal_raw = (form.get("proposal") or "").strip()
    try:
        proposal = int(proposal_raw) if proposal_raw else None
    except ValueError:
        h.redirect(f"/p/{urllib.parse.quote(project['id'])}/feedback",
                  error="proposal id must be a number")
        return
    try:
        agent_id, message_id = operations.submit_feedback(
            state, h.cfg, project["id"], form.get("text", ""), task, proposal,
        )
    except (ValueError, LookupError) as exc:
        h.redirect(f"/p/{urllib.parse.quote(project['id'])}/feedback", error=str(exc))
        return
    h.redirect(f"/p/{urllib.parse.quote(project['id'])}/feedback",
              ok=f"queued feedback #{message_id} for {agent_id}")


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
        self.connection.settimeout(5)
        self.session_is_new = False
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)

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
            args = (self, state, match.groupdict(), query)
            if method == "POST":
                args = (*args, form)
            view(*args)
        except LookupError as exc:
            self.send_response(HTTPStatus.NOT_FOUND)
            self._end(_error_page(HTTPStatus.NOT_FOUND, str(exc)))
        except sqlite3.OperationalError as exc:
            self._contention(exc)
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
        return host in _allowed_hosts(self.server.server_port)  # type: ignore[attr-defined]

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
        if self.headers.get_content_type() != "application/x-www-form-urlencoded":
            raise ValueError("expected a URL-encoded form")
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 <= length <= 1024 * 1024:
            raise ValueError("form exceeds the 1 MiB limit")
        raw = self.rfile.read(length) if length else b""
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


def make_server(cfg: Config, port: int, db_timeout: float = REQUEST_TIMEOUT_S) -> Server:
    """Bind loopback-only; nothing here ever listens on a non-loopback address."""
    server = Server(("127.0.0.1", port), Handler)
    server.cfg = cfg
    server.sessions = {}
    server.db_timeout = db_timeout
    return server


def serve(cfg: Config, port: int) -> None:
    httpd = make_server(cfg, port)
    print(f"serving http://127.0.0.1:{httpd.server_port} (loopback only; Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
