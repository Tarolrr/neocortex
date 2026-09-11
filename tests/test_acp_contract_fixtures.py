"""Offline shape checks for synthetic Codex ACP 1.11.0 JSON-RPC wires."""

import json
from pathlib import Path

import pytest

from nc.acp_contract import is_air_session_failure, is_completion_candidate

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
    if any(not is_air_session_failure(value) for value in observations):
        fact["prompt_response_valid"] = False
    fact["air_observations"] = observations
    fact["session_failures"] = [value for value in observations if is_air_session_failure(value)]
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
        assert is_air_session_failure(failure)
        assert isinstance(failure["id"], str) and failure["id"]
        assert isinstance(failure["revision"], int) and failure["revision"] > 0
        assert failure["category"] in VALID_CATEGORIES
        assert failure["severity"] in {"warning", "error"}
        assert isinstance(failure["title"], str)
        assert isinstance(failure["actions"], list)
        assert all(action in {"retry", "new_session", "login"} for action in failure["actions"])
    assert is_completion_candidate(fact) is candidate


def test_emitted_failure_shape_and_quota_mapping_are_not_invented():
    quota = prompt_fact(json.loads((FIXTURES / "acp-quota.synthetic.json").read_text()))
    assert quota["session_failures"] == [{
        "id": "p4:error", "revision": 1, "category": "limit", "severity": "error",
        "title": "Quota exhausted", "actions": [],
    }]


def test_air_revisions_are_ordered_but_do_not_signal_recovery():
    wire = json.loads((FIXTURES / "acp-revision-update.synthetic.json").read_text())
    assert wire["attribution"] == "synthetic; not a captured incident"
    fact = prompt_fact(wire)
    assert [(item["id"], item["revision"]) for item in fact["session_failures"]] == [
        ("p11:retry", 1), ("p11:retry", 2),
    ]
    assert is_completion_candidate(fact)


def test_retry_recovery_is_turn_progress_then_success_without_air_clear_revision():
    wire = json.loads((FIXTURES / "acp-retry-recovery.synthetic.json").read_text())
    assert wire["attribution"] == "synthetic; not a captured incident"
    updates = [item["params"]["update"] for item in wire["wire"]
               if item.get("method") == "session/update"]
    assert is_air_session_failure(air_failure(updates[0]))
    assert updates[1]["sessionUpdate"] == "agent_message_chunk"
    assert air_failure(updates[1]) is None
    assert event(wire, request_id="p12")["result"]["stopReason"] == "end_turn"


def test_generic_air_failure_without_severity_is_conservatively_terminal():
    assert not is_completion_candidate({
        "request_id": "p", "session_id": "s", "prompt_id": "p", "stop_reason": "end_turn",
        "prompt_response_valid": True, "air_observations": [{"category": "unknown"}],
        "session_failures": [{"category": "unknown"}],
    })


@pytest.mark.parametrize("malformed", [
    {"severity": "warning"},
    {"id": "x", "revision": 1, "category": "unknown", "severity": "warning",
     "title": "x", "actions": ["owner_approve"]},
])
def test_malformed_air_dict_cannot_be_a_completion_candidate(malformed):
    assert not is_completion_candidate({
        "request_id": "p", "session_id": "s", "prompt_id": "p", "stop_reason": "end_turn",
        "prompt_response_valid": True, "jsonrpc_result": {"stopReason": "end_turn"},
        "air_observations": [malformed], "session_failures": [malformed],
    })


def test_malformed_air_record_fixture_is_retained_but_non_completing():
    fact = prompt_fact(json.loads((FIXTURES / "acp-malformed-air-record.synthetic.json").read_text()))
    assert fact["air_observations"] == [{"severity": "warning"}]
    assert fact["session_failures"] == []
    assert not is_completion_candidate(fact)
