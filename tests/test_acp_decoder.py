"""Pure decoder coverage for the synthetic ACP contract wires."""

import json
from pathlib import Path

import pytest

from nc.acp_decoder import decode_acp_prompt_result

FIXTURES = Path(__file__).parent / "fixtures"


def decode(name):
    wire = json.loads((FIXTURES / name).read_text())["wire"]
    prompt = next(item for item in wire if item.get("method") == "session/prompt")
    return decode_acp_prompt_result(
        wire, request_id=prompt["id"], session_id=prompt["params"]["sessionId"]
    )


@pytest.mark.parametrize(("name", "kind"), [
    ("acp-success.synthetic.json", "success"),
    ("acp-warning-success.synthetic.json", "success"),
    ("acp-typed-terminal.synthetic.json", "failed"),
    ("acp-quota.synthetic.json", "failed"),
    ("acp-retryable-limit.synthetic.json", "success"),
    ("acp-retryable-service.synthetic.json", "success"),
    ("acp-access.synthetic.json", "failed"),
    ("acp-request.synthetic.json", "failed"),
    ("acp-cancelled.synthetic.json", "cancelled"),
    ("acp-malformed-meta.synthetic.json", "protocol_invalid"),
    ("acp-malformed-air-record.synthetic.json", "protocol_invalid"),
    ("acp-revision-update.synthetic.json", "success"),
    ("acp-retry-recovery.synthetic.json", "success"),
])
def test_decodes_every_contract_fixture(name, kind):
    assert decode(name).kind == kind


def test_omitted_severity_is_a_terminal_error():
    wire = [{"id": "p", "result": {"stopReason": "end_turn", "_meta": {"jetbrains": {"air": {
        "version": 1,
        "sessionFailure": {"id": "p:error", "revision": 1, "category": "unknown",
                           "title": "failed", "actions": []},
    }}}}}]
    assert decode_acp_prompt_result(wire, request_id="p", session_id="s").kind == "failed"


def test_unknown_extension_values_are_evidence_not_authority():
    wire = [{"id": "p", "result": {"stopReason": "end_turn", "_meta": {"jetbrains": {"air": {
        "version": 1,
        "sessionFailure": {"id": "p:error", "revision": 1, "category": "future",
                           "severity": "error", "title": "x", "actions": ["later"]},
    }}}}}]
    result = decode_acp_prompt_result(wire, request_id="p", session_id="s")
    assert result.kind == "failed"
    assert result.failures[0].category == "unknown"
    assert result.failures[0].unknown_category == "future"
    assert result.failures[0].unknown_actions == ("later",)


def test_identity_conflicts_stale_revisions_and_quoted_text_are_safe():
    terminal = {"id": "p", "result": {"stopReason": "end_turn"}}
    assert decode_acp_prompt_result([terminal, terminal], request_id="p", session_id="s").kind == "protocol_invalid"
    warning = {
        "id": "p:retry", "revision": 2, "category": "service", "severity": "warning",
        "title": "x", "actions": [],
    }
    stale = {**warning, "revision": 1, "title": "old"}
    wire = [
        {"method": "session/update", "params": {"sessionId": "s", "update": {
            "_meta": {"jetbrains": {"air": {"version": 1, "sessionFailure": warning}}},
        }}},
        {"method": "session/update", "params": {"sessionId": "s", "update": {
            "_meta": {"jetbrains": {"air": {"version": 1, "sessionFailure": stale}}},
        }}},
        terminal,
        {"method": "session/update", "params": {"sessionId": "s", "update": {"content": {"text": "error: no"}}}},
    ]
    result = decode_acp_prompt_result(wire, request_id="p", session_id="s")
    assert result.kind == "success" and result.failures[0].revision == 2


def test_terminal_metadata_and_usage_are_independent_and_complete():
    wire = [{"id": "p", "result": {
        "stopReason": "end_turn",
        "usage": {"inputTokens": 10, "cachedInputTokens": 8, "outputTokens": 4},
        "_meta": {"jetbrains": {"air": {"sessionFailure": {
            "id": "other:error", "revision": 1, "category": "service", "severity": "error",
            "title": "\x00quoted 'error'", "actions": [],
        }, "version": 1}}},
    }}]
    result = decode_acp_prompt_result(wire, request_id="p", session_id="s")
    assert result.kind == "protocol_invalid" and result.usage and result.usage.total_tokens == 14
    assert decode_acp_prompt_result(wire, request_id="p", session_id="s", air_version=2).kind == "protocol_invalid"


@pytest.mark.parametrize("version", [None, "1", 2, True])
def test_wire_air_versions_fail_closed_but_retain_terminal_facts(version):
    wire = [{"id": "p", "result": {
        "stopReason": "end_turn", "usage": {"inputTokens": 2, "outputTokens": 3},
        "_meta": {"jetbrains": {"air": {"version": version}}},
    }}]
    result = decode_acp_prompt_result(wire, request_id="p", session_id="s")
    assert result.kind == "protocol_invalid"
    assert result.stop_reason == "end_turn"
    assert result.usage and result.usage.total_tokens == 5


def test_wire_air_update_version_is_validated_without_a_failure():
    wire = [
        {"method": "session/update", "params": {"sessionId": "s", "update": {
            "_meta": {"jetbrains": {"air": {"version": 2}}},
        }}},
        {"id": "p", "result": {"stopReason": "end_turn"}},
    ]
    assert decode_acp_prompt_result(wire, request_id="p", session_id="s").kind == "protocol_invalid"


@pytest.mark.parametrize("usage", [
    {"inputTokens": "bad", "outputTokens": 2, "totalTokens": 5},
    {"inputTokens": 2, "totalTokens": 5},
    {"inputTokens": 2, "outputTokens": 3, "cachedInputTokens": "bad"},
    {"inputTokens": 2, "outputTokens": 3, "totalTokens": "bad"},
    {"inputTokens": 2, "input_tokens": 3, "outputTokens": 3},
])
def test_malformed_or_incomplete_usage_is_unknown(usage):
    wire = [{"id": "p", "result": {"stopReason": "end_turn", "usage": usage}}]
    result = decode_acp_prompt_result(wire, request_id="p", session_id="s")
    assert result.kind == "success" and result.usage is None


def test_matching_usage_aliases_are_a_complete_report():
    wire = [{"id": "p", "result": {"stopReason": "end_turn", "usage": {
        "inputTokens": 2, "input_tokens": 2, "outputTokens": 3, "output_tokens": 3,
    }}}]
    result = decode_acp_prompt_result(wire, request_id="p", session_id="s")
    assert result.usage and result.usage.total_tokens == 5


@pytest.mark.parametrize("error", ["bad", {}, {"code": "bad", "message": "x"},
                                  {"code": -1, "message": None}])
def test_malformed_jsonrpc_error_is_protocol_invalid(error):
    result = decode_acp_prompt_result([{"id": "p", "error": error}], request_id="p", session_id="s")
    assert result.kind == "protocol_invalid"


def test_valid_jsonrpc_error_is_failed():
    result = decode_acp_prompt_result([{"id": "p", "error": {"code": -32000, "message": "nope"}}],
                                      request_id="p", session_id="s")
    assert result.kind == "failed" and result.diagnostic == "nope"


def test_unsupported_wire_version_with_warning_cannot_be_success():
    wire = [{"id": "p", "result": {"stopReason": "end_turn", "_meta": {"jetbrains": {"air": {
        "version": 2, "sessionFailure": {"id": "p:warning", "revision": 1,
        "category": "service", "severity": "warning", "title": "advisory", "actions": []},
    }}}}}]
    assert decode_acp_prompt_result(wire, request_id="p", session_id="s").kind == "protocol_invalid"


def test_failure_diagnostics_redact_credentials_before_bounding():
    secret = "Bearer super-secret API_KEY=abcdefghijklmnop secret=hunter2"
    wire = [{"id": "p", "result": {"stopReason": "end_turn", "_meta": {"jetbrains": {"air": {
        "version": 1, "sessionFailure": {"id": "p:error", "revision": 1,
        "category": "service", "severity": "error", "title": secret + " x" * 3000,
        "actions": []},
    }}}}}]
    result = decode_acp_prompt_result(wire, request_id="p", session_id="s")
    diagnostic = result.failures[0].diagnostic
    assert result.kind == "failed" and len(diagnostic) == 4000
    assert "super-secret" not in diagnostic
    assert "abcdefghijklmnop" not in diagnostic
    assert "hunter2" not in diagnostic
    assert "[REDACTED]" in diagnostic
