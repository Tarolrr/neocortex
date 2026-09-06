import http.client
import re
import threading
import urllib.parse

import pytest

from nc.config import Config
from nc.state import State
from nc.ui import make_server


@pytest.fixture
def browser(tmp_path, monkeypatch):
    monkeypatch.setenv("NC_HOME", str(tmp_path / "isolated"))
    cfg = Config.load()
    state = State(cfg.db_path)
    state.add_project("one", "<script>alert(1)</script>", str(tmp_path), None)
    state.add_project("two", "Second", str(tmp_path), None)
    tid = state.add_task("one", "<img src=x onerror=alert(1)>", "objective", [])
    server = make_server(cfg, 0, db_timeout=0.01)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path, method="GET", form=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        body = urllib.parse.urlencode(form or {}) if method == "POST" else None
        merged = {"Connection": "close"}
        if method == "POST":
            merged["Content-Type"] = "application/x-www-form-urlencoded"
        merged.update(headers or {})
        conn.request(method, path, body=body, headers=merged)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode()
        conn.close()
        return result

    yield cfg, state, tid, server, request
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)
    assert not thread.is_alive()
    state.db.close()


def test_read_pages_isolated_read_only_and_assets(browser):
    cfg, state, tid, server, request = browser
    assert cfg.home.name == "isolated"
    assert server.server_address[0] == "127.0.0.1"
    before = list(state.db.iterdump())
    for path in ("/projects", "/p/one/tasks", "/p/two/tasks", f"/t/{tid}",
                 "/p/one/tasks/new", "/p/one/tasks/import", "/p/one/feedback",
                 "/p/one/proposals", "/inbox"):
        status, _, body = request(path)
        assert status == 200, body
        assert "<script>alert" not in body
        assert "<img src=x" not in body
    assert tid not in request("/p/two/tasks")[2]
    assert "&lt;script&gt;" in request("/projects")[2]
    status, headers, body = request("/static/style.css")
    assert status == 200 and headers["Content-Type"] == "text/css" and body
    assert request("/static/../ui.py")[0] == 404
    assert request("/")[0] == 303
    assert list(state.db.iterdump()) == before


def test_post_security_and_success(browser):
    _, state, tid, server, request = browser
    _, headers, body = request(f"/t/{tid}")
    token = re.search(r'name="csrf_token" value="([^"]+)"', body)[1]
    valid = {"Cookie": headers["Set-Cookie"].split(";")[0],
             "Origin": f"http://127.0.0.1:{server.server_port}"}
    form = {"csrf_token": token, "reason": "owner cancelled"}
    before = list(state.db.iterdump())
    assert request(f"/t/{tid}/cancel", "POST", form, {**valid, "Host": "evil.test"})[0] == 400
    for origin in ("http://evil.test", "null", valid["Origin"] + "/extra"):
        assert request(f"/t/{tid}/cancel", "POST", form, {**valid, "Origin": origin})[0] == 403
    assert request(f"/t/{tid}/cancel", "POST", form)[0] == 403
    assert request(f"/t/{tid}/cancel", "POST", {"reason": "x"}, valid)[0] == 403
    assert list(state.db.iterdump()) == before
    assert request(f"/t/{tid}/cancel", "POST", form, valid)[0] == 303
    assert state.one("SELECT status FROM task WHERE id=?", (tid,))["status"] == "cancelled"


def test_busy_database_returns_retryable_error(browser):
    _, state, tid, server, request = browser
    _, headers, body = request(f"/t/{tid}")
    token = re.search(r'name="csrf_token" value="([^"]+)"', body)[1]
    state.db.execute("BEGIN IMMEDIATE")
    try:
        status, headers, body = request(f"/t/{tid}/cancel", "POST",
            {"csrf_token": token, "reason": "cancel"},
            {"Cookie": headers["Set-Cookie"].split(";")[0],
             "Origin": f"http://127.0.0.1:{server.server_port}"})
        assert status == 503
        assert headers["Retry-After"] == "1"
        assert "database is busy" in body
    finally:
        state.db.rollback()
    assert state.one("SELECT status FROM task WHERE id=?", (tid,))["status"] == "queued"


@pytest.mark.parametrize("invalid", [None, 1, "text", [],
    {"project": "two", "title": "other", "objective": "x", "acceptance": []},
    {"project": "one", "title": "bad", "objective": "x", "acceptance": "oops"},
    {"project": "one", "title": "bad", "objective": "x", "acceptance": [],
     "budget_turns": False},
])
def test_import_rejects_invalid_batch_without_writes(browser, invalid):
    import json

    _, state, _, server, request = browser
    path = "/p/one/tasks/import"
    _, headers, body = request(path)
    token = re.search(r'name="csrf_token" value="([^"]+)"', body)[1]
    good = {"project": "one", "title": "valid", "objective": "x", "acceptance": []}
    before = list(state.db.iterdump())
    status, _, body = request(path, "POST",
        {"csrf_token": token, "spec": json.dumps([good, invalid])},
        {"Cookie": headers["Set-Cookie"].split(";")[0],
         "Origin": f"http://127.0.0.1:{server.server_port}"})
    assert status == 400, body
    assert list(state.db.iterdump()) == before


def test_import_valid_batch(browser):
    import json

    _, state, _, server, request = browser
    path = "/p/one/tasks/import"
    _, headers, body = request(path)
    token = re.search(r'name="csrf_token" value="([^"]+)"', body)[1]
    specs = [{"project": "one", "title": title, "objective": "x", "acceptance": []}
             for title in ("first", "second")]
    status, _, body = request(path, "POST",
        {"csrf_token": token, "spec": json.dumps(specs)},
        {"Cookie": headers["Set-Cookie"].split(";")[0],
         "Origin": f"http://127.0.0.1:{server.server_port}"})
    assert status == 303, body
    assert [r["title"] for r in state.q("SELECT title FROM task ORDER BY id")][-2:] == [
        "first", "second"]


def test_lifecycle_busy_rejects_mutations_without_writes(browser):
    from nc import operations
    from nc.lifecycle import LifecycleBusy, lifecycle_lock
    from nc.scheduler import Scheduler

    cfg, state, tid, server, request = browser
    _, headers, body = request(f"/t/{tid}")
    token = re.search(r'name="csrf_token" value="([^"]+)"', body)[1]
    before = list(state.db.iterdump())
    with lifecycle_lock(state):
        for operation in (
            lambda: operations.cancel_task(state, tid, "cancel"),
            lambda: operations.requeue_task(cfg, state, tid, fresh=True),
            lambda: operations.rollback_task(state, tid),
        ):
            with pytest.raises(LifecycleBusy):
                operation()
        assert Scheduler(cfg, state).step() == "idle"
        status, response_headers, body = request(f"/t/{tid}/cancel", "POST",
                                  {"csrf_token": token, "reason": "cancel"},
                                  {"Cookie": headers["Set-Cookie"].split(";")[0],
                                   "Origin": f"http://127.0.0.1:{server.server_port}"})
        assert status == 303
        assert "retry after" in request(response_headers["Location"])[2]
    assert list(state.db.iterdump()) == before


def test_fresh_cleanup_failure_preserves_database(browser, monkeypatch):
    from nc import arbiter, operations

    cfg, state, tid, _, _ = browser
    before = list(state.db.iterdump())

    def fail(*args):
        raise RuntimeError("worktree removal failed")

    monkeypatch.setattr(arbiter, "remove_worktree", fail)
    with pytest.raises(RuntimeError, match="worktree removal failed"):
        operations.requeue_task(cfg, state, tid, fresh=True)
    assert list(state.db.iterdump()) == before


def test_scheduler_lock_covers_selection_and_outcome(browser, monkeypatch):
    from nc import operations
    from nc.lifecycle import LifecycleBusy
    from nc.scheduler import Scheduler

    cfg, state, tid, _, _ = browser
    scheduler = Scheduler(cfg, state)

    def step():
        # No run record exists yet (or it has already ended): still protected.
        assert not state.one("SELECT 1 FROM run WHERE ended_at IS NULL")
        with pytest.raises(LifecycleBusy):
            operations.requeue_task(cfg, state, tid)
        return "idle"

    monkeypatch.setattr(scheduler, "_step_locked", step)
    assert scheduler.step() == "idle"
    operations.requeue_task(cfg, state, tid, budget=10)
    assert state.one("SELECT budget_turns FROM task WHERE id=?", (tid,))[0] == 10


@pytest.mark.parametrize("action", ["fresh", "rollback"])
def test_database_contention_precedes_repository_changes(browser, monkeypatch, action):
    import sqlite3

    from nc import arbiter, operations

    cfg, state, tid, _, _ = browser
    if action == "rollback":
        state.set_task(tid, status="done", merge_commit="abc123")
    calls = []
    monkeypatch.setattr(arbiter, "remove_worktree", lambda *args: calls.append(args))
    monkeypatch.setattr(arbiter, "revert", lambda *args: calls.append(args))
    other = State(cfg.db_path, initialize=False, timeout=0.01)
    before = list(state.db.iterdump())
    state.db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            if action == "fresh":
                operations.requeue_task(cfg, other, tid, fresh=True)
            else:
                operations.rollback_task(other, tid)
    finally:
        state.db.rollback()
        other.db.close()
    assert calls == []
    assert list(state.db.iterdump()) == before


def test_rollback_records_once(browser, monkeypatch):
    from nc import arbiter, operations

    _, state, tid, _, _ = browser
    state.set_task(tid, status="done", merge_commit="abc123")
    calls = []

    def revert(*args):
        calls.append(args)
        return "def456"

    monkeypatch.setattr(arbiter, "revert", revert)
    result = operations.rollback_task(state, tid)
    assert result["commit"] == "def456"
    assert state.one("SELECT status FROM task WHERE id=?", (tid,))[0] == "blocked"
    assert len(state.q("SELECT * FROM incident WHERE kind='rollback'")) == 1
    with pytest.raises(ValueError, match="not accepted"):
        operations.rollback_task(state, tid)
    assert len(calls) == 1


def test_feedback_proposal_decisions_and_answers(browser):
    _, state, tid, server, request = browser

    def post(page, action, form):
        _, headers, body = request(page)
        token = re.search(r'name="csrf_token" value="([^"]+)"', body)[1]
        return request(action, "POST", {"csrf_token": token, **form},
                       {"Cookie": headers["Set-Cookie"].split(";")[0],
                        "Origin": f"http://127.0.0.1:{server.server_port}"})

    feedback = "/p/one/feedback"
    assert post(feedback, feedback, {"text": "Plan <script>x</script>"})[0] == 303
    assert not state.q("SELECT * FROM run")
    spec = [{"project": "one", "title": "Proposed", "objective": "x", "acceptance": []}]
    pid = state.add_proposal("one", "planner", "<script>rationale</script>", spec)
    page = f"/proposals/{pid}"
    before = list(state.db.iterdump())
    assert "<script>rationale" not in request(page)[2]
    assert list(state.db.iterdump()) == before
    status, headers, _ = post(page, page + "/approve", {"force": "1"})
    assert status == 303
    assert "approved:" in request(headers["Location"])[2]
    assert state.one("SELECT status FROM proposal WHERE id=?", (pid,))[0] == "approved"
    pid = state.add_proposal("one", "planner", "reject this", spec)
    page = f"/proposals/{pid}"
    assert post(page, page + "/reject", {"reason": "No thanks"})[0] == 303
    assert state.one("SELECT status FROM proposal WHERE id=?", (pid,))[0] == "rejected"
    state.add_agent("worker-test", "worker", "one", tid, "model")
    mid = state.send("ASK", "worker-test", "owner", {"question": "<script>q</script>"}, tid)
    assert "<script>q" not in request("/inbox")[2]
    assert post("/inbox", f"/messages/{mid}/answer", {"text": "Continue"})[0] == 303
    assert state.one("SELECT delivered FROM message WHERE id=?", (mid,))[0] == 1


def test_cli_ui_explicit_home_port_and_shutdown(tmp_path, monkeypatch):
    from nc import cli, ui

    monkeypatch.setenv("NC_HOME", str(tmp_path / "unused"))
    home = tmp_path / "explicit"
    servers = []
    original = ui.make_server

    def make(cfg, port):
        assert cfg.home == home
        assert port == 0
        server = original(cfg, port)
        servers.append(server)

        def stop():
            raise KeyboardInterrupt

        server.serve_forever = stop
        return server

    monkeypatch.setattr(ui, "make_server", make)
    assert cli.main(["ui", "--home", str(home), "--port", "0"]) == 0
    assert servers[0].socket.fileno() == -1
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize("action", ["fresh", "rollback"])
def test_active_run_prevents_repository_changes(browser, monkeypatch, action):
    from nc import arbiter, operations

    cfg, state, tid, _, _ = browser
    if action == "rollback":
        state.set_task(tid, status="done", merge_commit="abc123")
    state.add_agent("active", "worker", "one", tid, "model")
    state.start_run("active", tid, "worker", "model", "unused")
    calls = []
    monkeypatch.setattr(arbiter, "remove_worktree", lambda *args: calls.append(args))
    monkeypatch.setattr(arbiter, "revert", lambda *args: calls.append(args))
    before = list(state.db.iterdump())
    with pytest.raises(ValueError, match="active run"):
        if action == "fresh":
            operations.requeue_task(cfg, state, tid, fresh=True)
        else:
            operations.rollback_task(state, tid)
    assert calls == []
    assert list(state.db.iterdump()) == before


def test_real_git_revert_failure_preserves_database(browser, tmp_path):
    from nc import arbiter, operations

    _, state, tid, _, _ = browser
    repo = tmp_path / "repo"
    repo.mkdir()
    arbiter.git(repo, "init", "-b", "main")
    arbiter.git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.test",
                "commit", "--allow-empty", "-m", "initial")
    state.x("UPDATE project SET repo_path=? WHERE id='one'", (str(repo),))
    state.set_task(tid, status="done", merge_commit="0000000000000000000000000000000000000000")
    before = list(state.db.iterdump())
    head = arbiter.git(repo, "rev-parse", "HEAD")
    with pytest.raises(RuntimeError):
        operations.rollback_task(state, tid)
    assert list(state.db.iterdump()) == before
    assert arbiter.git(repo, "rev-parse", "HEAD") == head
    assert not state.db.in_transaction
