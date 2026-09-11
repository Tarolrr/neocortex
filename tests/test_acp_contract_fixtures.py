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
        "prompt_response_valid": "result" in prompt_response,
        "stop_reason": prompt_response.get("result", {}).get("stopReason"),
        "air_observations": [],
        "session_failures": [],
    }
    if "result" in prompt_response:
        fact["jsonrpc_result"] = prompt_response["result"]
    if "error" in prompt_response:
        fact["jsonrpc_error"] = prompt_response["error"]
    terminal = air_failure(prompt_response.get("result", {}))
    updates = [item for item in wire["wire"] if item.get("method") == "session/update"]
    for update in updates:
        assert update["params"]["sessionId"] == fact["session_id"]
        assert wire["wire"].index(update) < wire["wire"].index(prompt_response)
    observations = [air_failure(update["params"]["update"]) for update in updates]
    if terminal is not None:
        observations.append(terminal)
    # Invalid AIR metadata is terminal for completion but remains observable.
    if any(not isinstance(value, dict) for value in observations):
        fact["prompt_response_valid"] = False
    fact["air_observations"] = observations
    fact["session_failures"] = [value for value in observations if isinstance(value, dict)]
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
        ("acp-revision-recovery.synthetic.json", True),
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
    assert initialize_response["result"]["_meta"]["jetbrains"]["air"] == {
        "version": 1, "capabilities": ["sessionFailure", "agentFileChangeReport",
        "nativeSubagentSessions", "asyncTasks", "recommendedValue"],
    }
    agent_meta = initialize_response["result"]["agentCapabilities"].get("_meta", {})
    assert "sessionFailure" not in agent_meta
    session = event(wire, method="session/new")
    assert set(session["params"]) == {"cwd", "mcpServers"}
    session_response = event(wire, request_id=session["id"])["result"]
    assert isinstance(session_response["sessionId"], str)
    assert session_response["sessionId"] != session["id"]
    advertised = session_response["configOptions"]
    assert {option["id"] for option in advertised} >= {"model", "mode"}
    for option in advertised:
        assert {"id", "name", "category", "type", "options", "currentValue"} <= option.keys()
        assert option["type"] == "select"
        assert all({"value", "name"} <= choice.keys() for choice in option["options"])
    config_calls = [
        item for item in wire["wire"] if item.get("method") == "session/set_config_option"
    ]
    assert [(call["params"]["configId"], call["params"]["value"]) for call in config_calls] == [
        ("model", "configured"), ("mode", "agent"),
    ]
    for call in config_calls:
        assert call["params"]["sessionId"] == session_response["sessionId"]
        response = event(wire, request_id=call["id"])
        assert "result" in response
        selected = next(option for option in response["result"]["configOptions"]
                        if option["id"] == call["params"]["configId"])
        assert selected["currentValue"] == call["params"]["value"]
        prompt_index = wire["wire"].index(event(wire, method="session/prompt"))
        assert wire["wire"].index(response) < prompt_index
    fact = prompt_fact(wire)
    failures = fact["session_failures"]
    if name == "acp-malformed-meta.synthetic.json":
        assert not failures
        assert fact["air_observations"] == ["invalid"]
        assert fact["jsonrpc_result"]["stopReason"] == "end_turn"
    for failure in failures:
        assert failure["category"] in VALID_CATEGORIES
        assert failure["severity"] in {"warning", "error"}
        assert isinstance(failure["actions"], list)
    assert is_completion_candidate(fact) is candidate


def test_emitted_failure_shape_and_quota_mapping_are_not_invented():
    quota = prompt_fact(json.loads((FIXTURES / "acp-quota.synthetic.json").read_text()))
    assert quota["session_failures"] == [{
        "id": "p4:error", "revision": 1, "category": "limit", "severity": "error",
        "title": "Quota exhausted", "actions": [],
    }]


def test_air_revisions_are_ordered_and_recovery_needs_a_valid_success():
    fact = prompt_fact(json.loads((FIXTURES / "acp-revision-recovery.synthetic.json").read_text()))
    assert [(item["id"], item["revision"]) for item in fact["session_failures"]] == [
        ("p11:retry", 1), ("p11:retry", 2),
    ]
    assert is_completion_candidate(fact)


def test_generic_air_failure_without_severity_is_conservatively_terminal():
    assert not is_completion_candidate({
        "request_id": "p", "session_id": "s", "prompt_id": "p", "stop_reason": "end_turn",
        "prompt_response_valid": True, "air_observations": [{"category": "unknown"}],
        "session_failures": [{"category": "unknown"}],
    })
