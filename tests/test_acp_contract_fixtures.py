"""Offline shape checks for synthetic Codex ACP 1.11.0 JSON-RPC wires."""

import json
from pathlib import Path

import pytest

from nc.acp_contract import is_completion_candidate

FIXTURES = Path(__file__).parent / "fixtures"
AIR = ("_meta", "jetbrains", "air", "sessionFailure")
VALID_CATEGORIES = {"connection", "access", "limit", "request", "service", "unknown"}


def event(wire, *, method=None, request_id=None):
    for item in wire["wire"]:
        if method is not None and item.get("method") == method:
            return item
        if request_id is not None and item.get("id") == request_id and "method" not in item:
            return item
    raise AssertionError("missing correlated JSON-RPC event")


def air_failure(message):
    value = message
    for key in AIR:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def prompt_fact(wire):
    prompt_request = event(wire, method="session/prompt")
    prompt_response = event(wire, request_id=prompt_request["id"])
    assert "result" in prompt_response or "error" in prompt_response
    fact = {
        "request_id": prompt_request["id"],
        "session_id": prompt_request["params"]["sessionId"],
        "prompt_id": prompt_request["id"],
        "stop_reason": prompt_response.get("result", {}).get("stopReason"),
    }
    if "error" in prompt_response:
        fact["jsonrpc_error"] = prompt_response["error"]
    terminal = air_failure(prompt_response.get("result", {}))
    updates = [item for item in wire["wire"] if item.get("method") == "session/update"]
    for update in updates:
        assert update["params"]["sessionId"] == fact["session_id"]
        assert wire["wire"].index(update) < wire["wire"].index(prompt_response)
    failures = [air_failure(update["params"]["update"]) for update in updates]
    failures = [failure for failure in failures if failure is not None]
    if terminal is not None:
        failures.append(terminal)
    if failures:
        assert len(failures) == 1
        fact["session_failure"] = failures[0]
    return fact


@pytest.mark.parametrize(
    ("name", "candidate"),
    [
        ("acp-success.synthetic.json", True),
        ("acp-typed-terminal.synthetic.json", False),
        ("acp-warning-success.synthetic.json", True),
        ("acp-quota.synthetic.json", False),
        ("acp-retryable-limit.synthetic.json", True),
        ("acp-retryable-service.synthetic.json", True),
        ("acp-access.synthetic.json", False),
        ("acp-request.synthetic.json", False),
        ("acp-cancelled.synthetic.json", False),
        ("acp-malformed-meta.synthetic.json", False),
    ],
)
def test_synthetic_acp_wire_fixture_shape(name, candidate):
    wire = json.loads((FIXTURES / name).read_text())
    assert wire["attribution"] == "synthetic; not a captured incident"
    initialize = event(wire, method="initialize")
    assert initialize["params"]["clientCapabilities"]["_meta"]["jetbrains"]["air"] == {
        "version": 1, "capabilities": ["sessionFailure"],
    }
    initialize_response = event(wire, request_id=initialize["id"])
    assert "sessionFailure" not in initialize_response["result"]["agentCapabilities"].get("_meta", {})
    session = event(wire, method="session/new")
    assert event(wire, request_id=session["id"])["result"]["sessionId"] == session["params"]["sessionId"]
    fact = prompt_fact(wire)
    failure = fact.get("session_failure")
    if name == "acp-malformed-meta.synthetic.json":
        assert not isinstance(failure, dict)
        fact.pop("session_failure", None)
        fact["jsonrpc_error"] = {"code": "malformed_air"}
    elif failure is not None:
        assert failure["category"] in VALID_CATEGORIES
        assert failure["severity"] in {"warning", "error"}
        assert isinstance(failure["actions"], list)
    assert is_completion_candidate(fact) is candidate


def test_emitted_failure_shape_and_quota_mapping_are_not_invented():
    quota = prompt_fact(json.loads((FIXTURES / "acp-quota.synthetic.json").read_text()))
    assert quota["session_failure"] == {
        "id": "p4:error", "revision": 1, "category": "limit", "severity": "error",
        "title": "Quota exhausted", "actions": [],
    }


def test_generic_air_failure_without_severity_is_conservatively_terminal():
    assert not is_completion_candidate({
        "request_id": "p", "session_id": "s", "prompt_id": "p", "stop_reason": "end_turn",
        "session_failure": {"category": "unknown"},
    })
