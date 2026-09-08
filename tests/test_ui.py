import http.client
import re
import socket
import subprocess
import threading
import urllib.parse

import pytest

from nc import protocol
from nc.config import Config
from nc.state import State
from nc.ui import Handler, make_server


@pytest.fixture
def browser(tmp_path, monkeypatch):
    monkeypatch.setenv("NC_HOME", str(tmp_path / "isolated"))
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
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


@pytest.mark.parametrize("partial", [b"", b"GET / HTTP/1.1\r\nHost:"])
@pytest.mark.parametrize("shutdown", [False, True])
def test_incomplete_request_times_out_before_dispatch(browser, monkeypatch, partial, shutdown):
    _, state, _, server, request = browser
    before = list(state.db.iterdump())
    assert Handler.timeout == 5.0
    monkeypatch.setattr(Handler, "timeout", 0.1)
    accepted = threading.Event()
    get_request = server.get_request

    def accept():
        connection = get_request()
        accepted.set()
        return connection

    monkeypatch.setattr(server, "get_request", accept)
    with socket.create_connection(server.server_address, timeout=3) as idle:
        if partial:
            idle.sendall(partial)
        assert accepted.wait(timeout=3)
        if shutdown:
            stopping = threading.Thread(target=server.shutdown, daemon=True)
            stopping.start()
            stopping.join(timeout=3)
            assert not stopping.is_alive(), "incomplete request blocked shutdown"
        else:
            assert request("/static/style.css")[0] == 200
        assert idle.recv(1) == b""
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


@pytest.mark.parametrize("dependency, error", [
    ("missing", "unknown dependency"),
    ("two-T001", "another project"),
])
def test_browser_creation_and_import_reject_invalid_dependencies(browser, dependency, error):
    import json

    _, state, _, server, request = browser
    foreign = state.add_task("two", "foreign", "objective", [])
    dependency = foreign if dependency == "two-T001" else dependency
    before = list(state.db.iterdump())

    _, headers, body = request("/p/one/tasks/new")
    token = re.search(r'name="csrf_token" value="([^"]+)"', body)[1]
    status, _, body = request("/p/one/tasks/new", "POST", {
        "csrf_token": token, "title": "bad", "objective": "objective",
        "acceptance": "", "boundaries": "", "after": dependency,
        "priority": "100", "budget_turns": "6",
    }, {"Cookie": headers["Set-Cookie"].split(";")[0],
        "Origin": f"http://127.0.0.1:{server.server_port}"})
    assert status == 400 and error in body
    assert list(state.db.iterdump()) == before

    _, headers, body = request("/p/one/tasks/import")
    token = re.search(r'name="csrf_token" value="([^"]+)"', body)[1]
    spec = {"project": "one", "title": "bad import", "objective": "objective",
            "acceptance": [], "depends_on": [dependency]}
    status, _, body = request("/p/one/tasks/import", "POST", {
        "csrf_token": token, "spec": json.dumps(spec),
    }, {"Cookie": headers["Set-Cookie"].split(";")[0],
        "Origin": f"http://127.0.0.1:{server.server_port}"})
    assert status == 400 and error in body
    assert list(state.db.iterdump()) == before


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
    token = operations.discard_preview(cfg, state, tid)["token"]
    with pytest.raises(RuntimeError, match="worktree removal failed"):
        operations.requeue_task(cfg, state, tid, fresh=True, expected_discard=token)
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
    discard = operations.discard_preview(cfg, other, tid)["token"] if action == "fresh" else None
    before = list(state.db.iterdump())
    state.db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            if action == "fresh":
                operations.requeue_task(cfg, other, tid, fresh=True, expected_discard=discard)
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
    result = operations.rollback_task(state, tid, "abc123")
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
    mid = state.send(protocol.QUESTION, "worker-test", "owner", {"question": "<script>q</script>"}, tid)
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
        operations.rollback_task(state, tid, "0" * 40)
    assert list(state.db.iterdump()) == before
    assert arbiter.git(repo, "rev-parse", "HEAD") == head
    assert not state.db.in_transaction


@pytest.mark.parametrize("budget", ["0", "-2"])
def test_invalid_requeue_budget_has_no_effect(browser, monkeypatch, budget):
    from nc import arbiter

    _, state, tid, server, request = browser
    _, headers, body = request(f"/t/{tid}")
    token = re.search(r'name="csrf_token" value="([^"]+)"', body)[1]
    calls = []
    monkeypatch.setattr(arbiter, "remove_worktree", lambda *args: calls.append(args))
    before = list(state.db.iterdump())
    status, headers, _ = request(
        f"/t/{tid}/requeue", "POST",
        {"csrf_token": token, "budget": budget, "fresh": "on"},
        {"Cookie": headers["Set-Cookie"].split(";")[0],
         "Origin": f"http://127.0.0.1:{server.server_port}"},
    )
    assert status == 303
    assert "greater than zero" in urllib.parse.unquote_plus(headers["Location"])
    assert list(state.db.iterdump()) == before
    assert calls == []


@pytest.mark.parametrize("invalid", ["done", "delivered", "kind", "recipient"])
def test_historical_or_nonquestion_answer_rejected(browser, invalid):
    _, state, tid, server, request = browser
    state.add_agent("questioner", "worker", "one", tid, "model")
    mid = state.send(protocol.QUESTION, "questioner", "owner", {"question": "Continue?"}, tid)
    if invalid == "done":
        state.set_task(tid, status="done", merge_commit="accepted")
    elif invalid == "delivered":
        state.x("UPDATE message SET delivered=1 WHERE id=?", (mid,))
    elif invalid == "kind":
        state.x("UPDATE message SET kind='NOTICE' WHERE id=?", (mid,))
    else:
        state.x("UPDATE message SET recipient='someone' WHERE id=?", (mid,))
    _, headers, body = request(f"/t/{tid}")
    token = re.search(r'name="csrf_token" value="([^"]+)"', body)[1]
    before = list(state.db.iterdump())
    assert f'/messages/{mid}/answer' not in request("/inbox?all=1")[2]
    status, headers, _ = request(
        f"/messages/{mid}/answer", "POST", {"csrf_token": token, "text": "Go"},
        {"Cookie": headers["Set-Cookie"].split(";")[0],
         "Origin": f"http://127.0.0.1:{server.server_port}"},
    )
    assert status == 303
    assert "not a currently answerable" in urllib.parse.unquote_plus(headers["Location"])
    assert list(state.db.iterdump()) == before


def test_conflicting_rollback_cleans_own_revert(browser, tmp_path):
    from nc import arbiter, operations

    _, state, tid, _, _ = browser
    repo = tmp_path / "conflict"
    repo.mkdir()
    arbiter.git(repo, "init", "-b", "main")
    arbiter.git(repo, "config", "user.name", "Test")
    arbiter.git(repo, "config", "user.email", "test@example.test")
    commits = []
    for content in ("original", "accepted", "later"):
        (repo / "f").write_text(content + "\n")
        arbiter.git(repo, "add", "f")
        arbiter.git(repo, "commit", "-m", content)
        commits.append(arbiter.git(repo, "rev-parse", "HEAD"))
    state.x("UPDATE project SET repo_path=? WHERE id='one'", (str(repo),))
    state.set_task(tid, status="done", merge_commit=commits[1])
    before = list(state.db.iterdump())
    with pytest.raises(RuntimeError):
        operations.rollback_task(state, tid, commits[1])
    assert list(state.db.iterdump()) == before
    assert arbiter.git(repo, "status", "--porcelain") == ""
    assert arbiter.git(repo, "rev-parse", "HEAD") == commits[2]
    assert not (repo / ".git" / "REVERT_HEAD").exists()
    assert (repo / "f").read_text() == "later\n"


@pytest.mark.parametrize("project_id", ["demo+tools", "demo?#&\"<tools>", "demo/tools%2F café"])
def test_identifier_urls_round_trip(browser, project_id):
    cfg, state, _, server, request = browser
    state.add_project(project_id, "Special project", str(cfg.home), None)
    tid = state.add_task(project_id, "Special task", "objective", [])
    project_path = "/p/" + urllib.parse.quote(project_id, safe="")
    task_path = "/t/" + urllib.parse.quote(tid, safe="")
    before = list(state.db.iterdump())
    assert f'href="{project_path}/tasks"' in request("/projects")[2]
    for suffix in ("tasks", "tasks/new", "tasks/import", "feedback", "proposals"):
        status, _, body = request(f"{project_path}/{suffix}")
        assert status == 200, body
        if suffix in ("tasks/new", "tasks/import", "feedback"):
            assert f'action="{project_path}/{suffix}"' in body
    assert f'href="{task_path}"' in request(project_path + "/tasks")[2]
    status, headers, body = request(task_path)
    assert status == 200, body
    assert f'href="{project_path}/tasks"' in body
    assert f'action="{task_path}/cancel"' in body
    assert list(state.db.iterdump()) == before
    token = re.search(r'name="csrf_token" value="([^"]+)"', body)[1]
    valid = {"Cookie": headers["Set-Cookie"].split(";")[0],
             "Origin": f"http://127.0.0.1:{server.server_port}"}
    status, headers, _ = request(task_path + "/cancel", "POST",
        {"csrf_token": token, "reason": "owner cancelled"}, valid)
    assert status == 303
    assert headers["Location"].split("?")[0] == task_path
    assert request(headers["Location"])[0] == 200
    assert state.one("SELECT status FROM task WHERE id=?", (tid,))["status"] == "cancelled"
    status, headers, body = request(project_path + "/tasks/new", "POST",
        {"csrf_token": token, "title": "Created in UI", "objective": "objective"}, valid)
    assert status == 303, body
    assert headers["Location"] == "/t/" + urllib.parse.quote(project_id + "-T002", safe="")
    assert request(headers["Location"])[0] == 200


@pytest.mark.parametrize("confirmation", [None, "old-commit"])
def test_rollback_requires_current_confirmation(browser, monkeypatch, confirmation):
    from nc import arbiter, operations

    _, state, tid, _, _ = browser
    state.set_task(tid, status="done", merge_commit="current-commit")
    before = list(state.db.iterdump())

    def unexpected_revert(*args):
        pytest.fail("stale confirmation must not mutate Git")

    monkeypatch.setattr(arbiter, "revert", unexpected_revert)
    with pytest.raises(ValueError, match="Confirm the current merge commit"):
        operations.rollback_task(state, tid, confirmation)
    assert list(state.db.iterdump()) == before


def test_detail_preserves_multiline_content(browser):
    import json

    _, state, tid, _, request = browser
    state.set_task(tid, objective="first\nsecond", acceptance=json.dumps(["check\nagain"]),
                   boundaries=json.dumps(["stay\ninside"]), result="result\ncontinued")
    status, _, body = request(f"/t/{tid}")
    assert status == 200
    for content in ("first\nsecond", "check\nagain", "stay\ninside", "result\ncontinued"):
        assert f"<pre>{content}</pre>" in body
    assert "(no stored check output)" in body


@pytest.mark.parametrize("directory_link", [False, True])
def test_evidence_symlinks_are_not_read(browser, tmp_path, directory_link):
    cfg, _state, tid, _, request = browser
    external = tmp_path / "external"
    external.mkdir()
    (external / f"{tid}.txt").write_text("secret external evidence")
    checks = cfg.home / "checks"
    if directory_link:
        checks.symlink_to(external, target_is_directory=True)
    else:
        checks.mkdir(exist_ok=True)
        (checks / f"{tid}.txt").symlink_to(external / f"{tid}.txt")
    status, _, body = request(f"/t/{tid}")
    assert status == 200
    assert "secret external evidence" not in body
    assert "no stored check output" in body


@pytest.mark.parametrize("payload,expected", [
    (('{"project":"one","title":"Uploaded","objective":"line 1\\nline 2",'
     '"acceptance":["check"],"boundaries":["scope"],"priority":7,'
     '"budget_turns":9,"depends_on":[]}' ), 303),
    ('{"project":"one","title":"bad","objective":"x","acceptance":[],"budget_turns":0}', 400),
    ('/etc/passwd', 400),
])
def test_uploaded_json_content(browser, payload, expected):
    _, state, _, server, request = browser
    _, headers, page = request('/p/one/tasks/import')
    token = re.search(r'name="csrf_token" value="([^"]+)"', page)[1]
    before = list(state.db.iterdump())
    boundary = 'nc-upload-boundary'
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="csrf_token"\r\n\r\n{token}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="upload"; '
        f'filename="/etc/passwd"\r\nContent-Type: application/json\r\n\r\n{payload}\r\n'
        f'--{boundary}--\r\n'
    ).encode()
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    conn.request('POST', '/p/one/tasks/import', body, {
        'Content-Type': f'multipart/form-data; boundary={boundary}',
        'Cookie': headers['Set-Cookie'].split(';')[0],
        'Origin': f'http://127.0.0.1:{server.server_port}',
    })
    response = conn.getresponse()
    assert response.status == expected, response.read().decode()
    response.read()
    conn.close()
    if expected == 400:
        assert list(state.db.iterdump()) == before
    else:
        task = state.one("SELECT * FROM task WHERE title='Uploaded'")
        assert task['objective'] == 'line 1\nline 2'
        assert task['budget_turns'] == 9
        assert task['priority'] == 7
        assert task['boundaries'] == '["scope"]'


def test_fresh_discard_confirmation_tracks_uncommitted_content(browser):
    from nc import operations

    cfg, state, tid, _, _ = browser
    worktree = cfg.work_dir / tid
    worktree.mkdir(parents=True)
    content = worktree / "untracked.txt"
    content.write_text("first")
    preview = operations.discard_preview(cfg, state, tid)
    content.write_text("second")
    before = list(state.db.iterdump())
    with pytest.raises(ValueError, match="Confirm the current discarded work"):
        operations.requeue_task(cfg, state, tid, fresh=True,
                                expected_discard=preview["token"])
    assert content.read_text() == "second"
    assert list(state.db.iterdump()) == before
    current = operations.discard_preview(cfg, state, tid)
    assert current["token"] != preview["token"]
    with pytest.raises(ValueError, match="Confirm the current discarded work"):
        operations.requeue_task(cfg, state, tid, fresh=True)
    assert list(state.db.iterdump()) == before


def test_fresh_discard_confirmation_tracks_task_budget(browser):
    from nc import operations

    cfg, state, tid, _, _ = browser
    preview = operations.discard_preview(cfg, state, tid)
    operations.requeue_task(cfg, state, tid, budget=20)
    before = list(state.db.iterdump())
    with pytest.raises(ValueError, match="Confirm the current discarded work"):
        operations.requeue_task(cfg, state, tid, fresh=True,
                                expected_discard=preview["token"])
    assert list(state.db.iterdump()) == before
    current = operations.discard_preview(cfg, state, tid)
    operations.requeue_task(cfg, state, tid, fresh=True, expected_discard=current["token"])
