"""Offline, synthetic ACP-wire contract checks; no ACP process is launched."""

import json
from pathlib import Path

import pytest

from nc.acp_contract import is_completion_candidate

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("name", "candidate"),
    [
        ("acp-success.synthetic.json", True),
        ("acp-typed-terminal.synthetic.json", False),
        ("acp-warning-success.synthetic.json", True),
        ("acp-quota.synthetic.json", False),
        ("acp-retryable.synthetic.json", False),
        ("acp-access-request.synthetic.json", False),
        ("acp-cancelled.synthetic.json", False),
        ("acp-malformed-meta.synthetic.json", False),
    ],
)
def test_synthetic_acp_wire_fixture_shape(name, candidate):
    wire = json.loads((FIXTURES / name).read_text())
    assert wire["attribution"] == "synthetic; not a captured incident"
    assert wire["initialize"]["clientCapabilities"]["_meta"]["jetbrains"]["air"] == {
        "version": 1, "capabilities": ["sessionFailure"],
    }
    assert wire["session"]["method"] == "session/new"
    assert wire["prompt"]["method"] == "session/prompt"
    result = wire["prompt"]["response"]
    prompt_fact = {
        "request_id": wire["prompt"]["id"],
        "session_id": wire["session"]["id"],
        "prompt_id": wire["prompt"]["id"],
        "stop_reason": result.get("stopReason"),
    }
    if "error" in result:
        prompt_fact["jsonrpc_error"] = result["error"]
    if isinstance(wire.get("sessionFailure"), dict):
        prompt_fact["session_failure"] = wire["sessionFailure"]
    elif "sessionFailure" in wire:
        prompt_fact["jsonrpc_error"] = {"code": "malformed_air"}
    assert is_completion_candidate(prompt_fact) is candidate


def test_air_failure_omitted_severity_is_error_and_malformed_meta_is_rejected():
    quota = json.loads((FIXTURES / "acp-quota.synthetic.json").read_text())
    assert quota["sessionFailure"].get("severity", "error") == "error"
    malformed = json.loads((FIXTURES / "acp-malformed-meta.synthetic.json").read_text())
    assert not isinstance(malformed["sessionFailure"], dict)
