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
