"""End-to-end coverage for the isolated client using a local fake server."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

from nc.acp_client import (
    AcpCleanupUncertain,
    AcpClientRejected,
    CodexAcpLaunchEvidence,
    CodexAcpPolicy,
    _inspected_launch_evidence,
    _run_codex_acp_turn,
)
from nc.acp_wire import AcpProcessTimeout, AcpSubprocess, AcpTransportEof, AcpUnexpectedExit


def fake_server(scenario: str) -> list[str]:
    # It deliberately asserts the client wire rather than duplicating client
    # internals: the test is an ACP stdio exchange, not fixture decoding.
    code = r'''
import json, os, sys, time
scenario = sys.argv[1]
def read(): return json.loads(sys.stdin.readline())
def send(value): print(json.dumps(value), flush=True)
def response(request, result): send({"jsonrpc":"2.0", "id":request["id"], "result":result})
init = read()
if scenario == "agent_tool":
    assert set(init["params"]["clientCapabilities"]) == {"_meta"}
if scenario == "isolated":
    home = os.environ["HOME"]
    config = open(os.path.join(home, "config.toml")).read()
    assert os.environ["CODEX_HOME"] == home
    assert "CODEX_CONFIG" not in os.environ
    assert "CODEX_PATH" not in os.environ
    assert "INITIAL_AGENT_MODE" not in os.environ
    assert "sandbox_mode = \"workspace-write\"" in config
    assert "web_search = \"disabled\"" in config and "network_access = false" in config
    assert home != os.environ.get("INHERITED_HOME")
if scenario == "eof":
    import os
    os.close(1); time.sleep(2); sys.exit(0)
if scenario == "unsupported_profile":
    # Negotiation is validated before a session can be created.  EOF proves
    # the client did not send session/new (and therefore cannot prompt).
    response(init, {"protocolVersion":2, "_meta":{"jetbrains":{"air":{"version":1,"capabilities":["sessionFailure"]}}}})
    assert sys.stdin.readline() == ""
    sys.exit(0)
if scenario in {"bool_protocol_version", "bool_air_version"}:
    profile = {"protocolVersion": True, "_meta":{"jetbrains":{"air":{"version":1,"capabilities":["sessionFailure"]}}}}
    if scenario == "bool_air_version":
        profile["protocolVersion"] = 1
        profile["_meta"]["jetbrains"]["air"]["version"] = True
    response(init, profile)
    # The rejected initialize profile must not create a session.
    assert sys.stdin.readline() == ""
    sys.exit(0)
response(init, {"protocolVersion":1, "_meta":{"jetbrains":{"air":{"version":1,"capabilities":["sessionFailure"]}}}})
new = read()
mode = "agent"
options = [{"id":"model","options":[{"value":"model"}],"currentValue":"x"}, {"id":"mode","options":[{"value":mode}],"currentValue":"x"}]
if scenario == "unsupported": options[0]["options"] = []
response(new, {"sessionId":"fresh", "configOptions":options,
               "sessionCapabilities":{"close": scenario in {"timeout", "cancel_timeout", "close_timeout"}}})
if scenario == "unsupported": sys.exit(0)
for expected in ("model", "mode"):
    request = read()
    assert request["method"] == "session/set_config_option" and request["params"]["configId"] == expected
    for option in options:
        if option["id"] == expected: option["currentValue"] = request["params"]["value"]
    if scenario == "reset_model" and expected == "mode":
        options = [option for option in options if option["id"] != "model"]
    config_result = {"configOptions":options}
    if scenario == "setup_air" and expected == "model":
        # AIR metadata on a setup reply belongs to neither the active prompt
        # nor its session/update window and must not taint prompt evidence.
        config_result["_meta"] = {"jetbrains":{"air":{"version":1,"sessionFailure":{
            "id":"setup:x", "revision":1, "category":"service", "severity":"error",
            "title":"unrelated setup failure", "actions":[]
        }}}}
    response(request, config_result)
if scenario == "reset_model":
    # The client must reject the final snapshot before it can send a prompt.
    assert sys.stdin.readline() == ""
    sys.exit(0)
prompt = read()
assert prompt["method"] == "session/prompt" and prompt["params"]["sessionId"] == "fresh"
if scenario in {"usage_eof", "air_overflow"}:
    failure = {"id":str(prompt["id"])+":x","revision":1,"category":"service","severity":"error","title":"retained","actions":[]}
    if scenario == "usage_eof":
        send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"fresh","update":{"_meta":{"jetbrains":{"air":{"version":1,"sessionFailure":failure}}},"sessionUpdate":"usage_update","usage":{"inputTokens":2,"outputTokens":3},"toolPayload":"must-not-retain"}}})
        os.close(1); time.sleep(.2); sys.exit(0)
    for number in range(200):
        failure["revision"] = number + 1
        send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"fresh","update":{"_meta":{"jetbrains":{"air":{"version":1,"sessionFailure":failure}}}}}})
    time.sleep(.2); sys.exit(0)
if scenario == "usage_timeout":
    send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"fresh","update":{"_meta":{"jetbrains":{"air":{"version":1,"sessionFailure":{"id":str(prompt["id"])+":x","revision":1,"category":"service","severity":"error","title":"retained","details":"full detail","actions":[]}}}},"sessionUpdate":"usage_update","usage":{"inputTokens":2,"outputTokens":3},"toolPayload":"must-not-retain"}}})
if scenario in {"timeout", "usage_timeout"}:
    cancel = read()
    assert cancel["method"] == "session/cancel"
    # This is deliberately late: it proves cancellation is not completion.
    response(prompt, {"stopReason":"end_turn"})
    close = read()
    assert close["method"] == "session/close"
    response(close, {})
    time.sleep(.1); sys.exit(0)
if scenario == "cancel_timeout":
    cancel = read()
    assert cancel["method"] == "session/cancel"
    # Exceed the actual client cancel-response deadline, then still service
    # the close request so the test observes this phase separately.
    time.sleep(10.2)
    close = read()
    assert close["method"] == "session/close"
    response(close, {})
    sys.exit(0)
if scenario == "close_timeout":
    cancel = read()
    assert cancel["method"] == "session/cancel"
    response(prompt, {"stopReason":"end_turn"})
    close = read()
    assert close["method"] == "session/close"
    time.sleep(5.2)
    sys.exit(0)
if scenario == "permission":
    send({"jsonrpc":"2.0","id":"permission","method":"session/request_permission","params":{"sessionId":"fresh"}})
    assert read()["result"] == {"outcome":{"outcome":"cancelled"}}
if scenario == "elicitation":
    send({"jsonrpc":"2.0","id":"elicit","method":"elicitation/create","params":{"sessionId":"fresh"}})
    assert read()["result"] == {"action":"cancel", "content":None}
if scenario == "numeric_overlap":
    # JSON-RPC ids are bidirectional namespaces.  A peer request may use the
    # same numeric value as our outstanding prompt id without becoming its
    # response.
    send({"jsonrpc":"2.0","id":prompt["id"],"method":"session/request_permission","params":{"sessionId":"fresh"}})
    assert read()["id"] == prompt["id"] and read is not None
if scenario in {"malformed_request", "foreign_request", "unsupported_request"}:
    if scenario == "malformed_request":
        method, params = "session/request_permission", {}
    elif scenario == "foreign_request":
        method, params = "session/request_permission", {"sessionId":"other"}
    else:
        method, params = "client/unsupported", {"sessionId":"fresh"}
    send({"jsonrpc":"2.0","id":"bad","method":method,"params":params})
    assert read()["error"]["code"] == -32603
if scenario == "agent_tool":
    # This is an agent-owned update; it requires no client terminal or
    # filesystem capability and is not a client-directed tool request.
    send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"fresh","update":{"sessionUpdate":"tool_call_update","toolCallId":"agent-owned-command","status":"completed"}}})
if scenario == "stale_duplicate":
    failure = {"id":str(prompt["id"])+":x","revision":2,"category":"service","severity":"warning","title":"current","actions":[]}
    send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"fresh","update":{"_meta":{"jetbrains":{"air":{"version":1,"sessionFailure":failure}}}}}})
    send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"fresh","update":{"_meta":{"jetbrains":{"air":{"version":1,"sessionFailure":failure}}}}}})
    failure["revision"] = 1
    failure["title"] = "stale"
    send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"fresh","update":{"_meta":{"jetbrains":{"air":{"version":1,"sessionFailure":failure}}}}}})
if scenario == "unknown_warning":
    failure = {"id":str(prompt["id"])+":future","revision":1,"category":"future","severity":"warning","title":"future warning","actions":["future_action"]}
    send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"fresh","update":{"_meta":{"jetbrains":{"air":{"version":1,"sessionFailure":failure}}}}}})
if scenario == "error":
    send({"jsonrpc":"2.0","id":prompt["id"],"error":{"code":-1,"message":"bad"}}); time.sleep(.2); sys.exit(0)
if scenario == "malformed":
    response(prompt, {"stopReason":"end_turn","_meta":{"jetbrains":{"air":{"version":2}}}}); time.sleep(.2); sys.exit(0)
if scenario == "malformed_failure":
    bad_failure = {"id": 7}
    response(prompt, {"stopReason":"end_turn", "_meta":{"jetbrains":{"air":{
        "version": 1, "sessionFailure": bad_failure}}}})
    time.sleep(.2); sys.exit(0)
if scenario == "post_response_exit":
    response(prompt, {"stopReason":"end_turn","usage":{"inputTokens":2,"outputTokens":3}})
    sys.exit(7)
if scenario == "delayed_post_response_exit":
    response(prompt, {"stopReason":"end_turn","usage":{"inputTokens":2,"outputTokens":3}})
    # Outlive the 50ms post-response observation, but die before the
    # five-second stdin-EOF grace period is allowed to expire.
    time.sleep(.15); sys.exit(9)
failure = {"id":str(prompt["id"])+":x","revision":1,"category":"service","severity":"warning","title":"retry","actions":["retry"]}
if scenario == "warning": send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"fresh","update":{"_meta":{"jetbrains":{"air":{"version":1,"sessionFailure":failure}}}}}})
if scenario == "raw_extension":
    failure["providerExtension"] = {"nested": ["retained", {"shape": "exact"}]}
    send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"fresh","update":{"_meta":{"jetbrains":{"air":{"version":1,"sessionFailure":failure}}}}}})
if scenario == "foreign_incident":
    # Same session, but a late failure emitted by an earlier prompt.
    failure["id"] = "previous-prompt:x"
    failure["severity"] = "error"
    send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"fresh","update":{"_meta":{"jetbrains":{"air":{"version":1,"sessionFailure":failure}}}}}})
if scenario in {"terminal", "terminal_details"}:
    failure["severity"] = "error"
    if scenario == "terminal_details": failure["details"] = "provider detail"
    response(prompt, {"stopReason":"end_turn","_meta":{"jetbrains":{"air":{"version":1,"sessionFailure":failure}}}})
else: response(prompt, {"stopReason":"end_turn","usage":{"inputTokens":2,"outputTokens":3}})
time.sleep(.2)
'''
    return [sys.executable, "-c", code, scenario]


def verified_launch(command: list[str], **overrides: str) -> CodexAcpLaunchEvidence:
    values = {
        "command": tuple(command),
        "package": "@agentclientprotocol/codex-acp",
        "package_version": "1.11.0",
        "artifact_integrity": (
            "sha512-opPKsRaekgdmQpOpHrR0EEDn9chgtiN+b+h0V78fTuQP84TNzB7vrn3EtKODwbiJQTBHJAlynjSFQazFfaT+VQ=="
        ),
        "codex_version": "0.153.4",
        "sdk_version": "1.4.0",
        "profile": "agent",
        "platform": "linux-amd64",
        "binary_package": "/isolated/codex-linux-x64",
        "binary_resolution": "/isolated/codex",
    }
    values.update(overrides)
    return _inspected_launch_evidence(**values)  # type: ignore[arg-type]


def test_plain_string_launch_evidence_is_not_an_attestation(tmp_path: Path) -> None:
    command = fake_server("success")
    forged = CodexAcpLaunchEvidence(
        command=tuple(command), package="@agentclientprotocol/codex-acp", package_version="1.11.0",
        artifact_integrity="sha512-opPKsRaekgdmQpOpHrR0EEDn9chgtiN+b+h0V78fTuQP84TNzB7vrn3EtKODwbiJQTBHJAlynjSFQazFfaT+VQ==",
        codex_version="0.153.4", sdk_version="1.4.0", profile="agent", platform="linux-amd64",
        binary_package="/claim", binary_resolution="/claim/bin")
    with pytest.raises(AcpClientRejected, match="runtime inspection"):
        _run_codex_acp_turn(command, launch=forged, policy=CodexAcpPolicy.ordinary(tmp_path),
                           model="model", prompt="hello", log_path=tmp_path / "acp.log")


def run(tmp_path: Path, scenario: str, **kwargs: object):
    command = fake_server(scenario)
    return _run_codex_acp_turn(command, launch=verified_launch(command),
                               policy=CodexAcpPolicy.ordinary(tmp_path),
                               model="model", prompt="hello", log_path=tmp_path / "acp.log",
                               timeout_s=kwargs.pop("timeout_s", 1), **kwargs)


def test_private_auth_is_at_codex_home_and_absolute_launcher_ignores_path(tmp_path: Path) -> None:
    """A stub child proves the actual launch environment, without auth values."""
    from nc import acp_runtime

    auth = tmp_path / "source-auth.json"
    auth.write_text(
        '{"tokens":{"id_token":"e30.e30.c2ln","access_token":"fixture",'
        '"refresh_token":"fixture"}}'
    )
    auth.chmod(0o600)
    home = acp_runtime.prepare_private_home(auth, parent=tmp_path / "homes")
    observed = tmp_path / "observed.json"
    code = (
        "import json, os, pathlib; "
        "pathlib.Path(os.environ['OBSERVED']).write_text(json.dumps({k: os.environ.get(k) for k in "
        "('PATH','HOME','CODEX_HOME','CODEX_PATH','CODEX_CONFIG','XDG_CONFIG_HOME')}));"
    )
    command = [sys.executable, "-c", code]
    try:
        with pytest.raises(AcpUnexpectedExit):
            _run_codex_acp_turn(command, launch=verified_launch(command),
                                policy=CodexAcpPolicy.ordinary(tmp_path), model="model", prompt="hello",
                                log_path=tmp_path / "acp.log", timeout_s=1, private_home=home,
                                environment={"PATH": "", "OBSERVED": str(observed), "CODEX_PATH": "/bad",
                                             "CODEX_CONFIG": "/bad", "XDG_CONFIG_HOME": "/bad"})
        child_env = json.loads(observed.read_text())
        assert child_env["PATH"] == ""
        assert child_env["HOME"] == str(home)
        assert child_env["CODEX_HOME"] == str(home)
        assert child_env["CODEX_PATH"] is None and child_env["CODEX_CONFIG"] is None
        assert child_env["XDG_CONFIG_HOME"] == str(home)
        assert (Path(child_env["CODEX_HOME"]) / "auth.json").is_file()
    finally:
        acp_runtime.cleanup_private_home(home, expected_parent=tmp_path / "homes")


def test_fake_success_has_independent_shutdown_evidence(tmp_path: Path) -> None:
    turn = run(tmp_path, "success")
    assert turn.prompt.kind == "success"
    assert turn.prompt.usage and turn.prompt.usage.total_tokens == 5
    assert turn.prompt_fact["session_id"] == "fresh"
    assert turn.process["pid"] > 0 and turn.shutdown and "intentional shutdown" in turn.shutdown
    assert turn.live_sandbox_enforcement_verified is False


@pytest.mark.parametrize("scenario,kind", [("terminal", "failed"), ("warning", "success"),
                                              ("error", "failed"), ("malformed", "protocol_invalid")])
def test_fake_terminal_and_recoverable_evidence(tmp_path: Path, scenario: str, kind: str) -> None:
    turn = run(tmp_path, scenario)
    assert turn.prompt.kind == kind
    if scenario in {"terminal", "warning"}:
        assert turn.prompt_fact["air_observations"]


def test_fake_permission_is_denied_noninteractively(tmp_path: Path) -> None:
    assert run(tmp_path, "permission").prompt.kind == "success"


def test_fake_setup_air_metadata_cannot_taint_prompt_evidence(tmp_path: Path) -> None:
    turn = run(tmp_path, "setup_air")
    assert turn.prompt.kind == "success"
    assert turn.prompt_fact["air_observations"] == []
    assert turn.prompt_fact["session_failures"] == []


def test_fake_malformed_air_failure_is_lossless_prompt_evidence(tmp_path: Path) -> None:
    turn = run(tmp_path, "malformed_failure")
    assert turn.prompt.kind == "protocol_invalid"
    assert turn.prompt_fact["air_observations"] == [{"id": 7}]
    assert turn.prompt_fact["session_failures"] == []


def test_source_pinned_agent_owned_tool_update_needs_no_client_tools(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "pinned-codex-acp"
    agent_mode = (fixture / "AgentMode.ts").read_text()
    event_handler = (fixture / "CodexEventHandler.commandExecution.extract.ts").read_text()
    # Fixed digests verify the extracted upstream source, independently of
    # the semantic assertions below.  SOURCE.json binds these extracts to the
    # pinned commit and the complete files' upstream digests.
    assert hashlib.sha256(agent_mode.encode()).hexdigest() == "15c942b2dc93075e50dc9f00e01339041f9d4667e5d7f83ebe28240210f4b941"
    assert hashlib.sha256(event_handler.encode()).hexdigest() == "ea9b6b709c1152487a2264b46a4bafac10d8185ec985ad05fcc357253f70036a"
    manifest = json.loads((fixture / "SOURCE.json").read_text())
    assert manifest["commit"] == "51d6247ac7448485bfcf534b813196fafc26df59"
    assert manifest["AgentMode.ts"]["source_sha256"] == "2c014d971fff367710779f102557dda9ec1a56c4b5faebc745bb2642280388e7"
    assert manifest["CodexEventHandler.ts"]["source_sha256"] == "42097f14d827e936232188289ea3389c4fbd24b9e826f763a301658e1b745d39"
    # Derive selected agent policy and its commandExecution ACP mapping from
    # the integrity-checked TypeScript artifacts.
    agent = re.search(r'static readonly Agent = new AgentMode\((.*?)\n    \);', agent_mode, re.DOTALL)
    assert agent and '"agent"' in agent.group(1)
    assert '"on-request"' in agent.group(1) and '"auto_review"' in agent.group(1)
    assert 'type: "workspaceWrite"' in agent.group(1)
    assert 'networkAccess: false' in agent.group(1)
    assert '"type": "commandExecution"' in event_handler
    assert 'sessionUpdate: "tool_call_update"' in event_handler
    assert 'toolCallId: item.id' in event_handler
    assert 'status: item.status === "completed" ? "completed" : "failed"' in event_handler
    turn = run(tmp_path, "agent_tool")
    assert turn.prompt.kind == "success"
    # The fake server only proceeds if it received the client's exact
    # initialize shape; this update is the source-backed server-to-client
    # agent tool path, not a capability advertised by NC.
    assert turn.live_sandbox_enforcement_verified is False


def test_fake_late_foreign_incident_is_not_prompt_evidence(tmp_path: Path) -> None:
    turn = run(tmp_path, "foreign_incident")
    assert turn.prompt.kind == "success"
    assert turn.prompt.failures == ()
    assert turn.prompt_fact["air_observations"] == []
    assert turn.prompt_fact["session_failures"] == []


def test_fake_post_response_nonzero_exit_cannot_be_success(tmp_path: Path) -> None:
    with pytest.raises(AcpUnexpectedExit, match="exited unexpectedly with status 7") as raised:
        run(tmp_path, "post_response_exit")
    assert raised.value.process["exit_code"] == 7
    assert "before deliberate shutdown" in raised.value.shutdown


def test_fake_delayed_shutdown_nonzero_exit_cannot_be_success(tmp_path: Path) -> None:
    with pytest.raises(AcpUnexpectedExit, match="exited unexpectedly with status 9") as raised:
        run(tmp_path, "delayed_post_response_exit")
    assert raised.value.process["exit_code"] == 9
    assert "failure during deliberate shutdown" in raised.value.shutdown


def test_usage_and_air_are_retained_when_prompt_ends_in_eof(tmp_path: Path) -> None:
    with pytest.raises(AcpTransportEof) as raised:
        run(tmp_path, "usage_eof")
    evidence = raised.value.evidence
    assert evidence.prompt is not None
    assert evidence.prompt["air_observations"][0]["id"].endswith(":x")
    assert evidence.usage == {"inputTokens": 2, "outputTokens": 3, "totalTokens": 5}
    assert "toolPayload" not in str(evidence.prompt)


def test_usage_and_raw_air_are_retained_when_prompt_times_out(tmp_path: Path) -> None:
    with pytest.raises(AcpProcessTimeout) as raised:
        run(tmp_path, "usage_timeout", timeout_s=.1)
    evidence = raised.value.evidence
    assert evidence.prompt is not None
    # The response arrived only after the original prompt deadline, while the
    # client was performing its bounded cancellation.  It remains terminal
    # evidence even though the outcome must stay a timeout.
    assert evidence.prompt["prompt_response_valid"] is True
    assert evidence.prompt["stop_reason"] == "end_turn"
    assert evidence.prompt["jsonrpc_result"] == {"stopReason": "end_turn"}
    assert evidence.prompt["air_observations"] == [{
        "id": evidence.prompt["prompt_id"] + ":x", "revision": 1,
        "category": "service", "severity": "error", "title": "retained",
        "details": "full detail", "actions": [],
    }]
    assert evidence.usage == {"inputTokens": 2, "outputTokens": 3, "totalTokens": 5}
    assert "toolPayload" not in str(evidence.prompt)


def test_notification_flood_after_air_failure_fails_closed_with_evidence(tmp_path: Path) -> None:
    from nc.acp_client import AcpEvidenceOverflow

    with pytest.raises(AcpEvidenceOverflow) as raised:
        run(tmp_path, "air_overflow")
    evidence = raised.value.evidence
    assert evidence.status == "evidence_overflow"
    assert evidence.prompt is not None and evidence.prompt["air_observations"]
    # The rejected 128th notification was never committed as raw evidence.
    assert len(evidence.prompt["air_observations"]) <= 127


def test_fake_elicitation_is_cancelled_noninteractively(tmp_path: Path) -> None:
    assert run(tmp_path, "elicitation").prompt.kind == "success"


def test_fake_numeric_bidirectional_request_id_overlap_is_not_prompt_response(tmp_path: Path) -> None:
    assert run(tmp_path, "numeric_overlap").prompt.kind == "success"


def test_fake_stale_air_revision_cannot_override_decoder_effective_failure(tmp_path: Path) -> None:
    turn = run(tmp_path, "stale_duplicate")
    assert turn.prompt.kind == "success"
    assert turn.prompt.failures[0].revision == 2
    assert turn.prompt_fact["session_failures"][0]["revision"] == 2


def test_unknown_air_warning_uses_decoder_effective_completion_semantics(tmp_path: Path) -> None:
    from nc.acp_contract import is_completion_candidate

    turn = run(tmp_path, "unknown_warning")
    assert turn.prompt.kind == "success"
    assert turn.prompt_fact["session_failures"] == [{
        "id": turn.prompt_fact["prompt_id"] + ":future", "revision": 1,
        "category": "unknown", "severity": "warning", "title": "future warning",
        "actions": ["future_action"],
    }]
    assert is_completion_candidate(turn.prompt_fact)


def test_raw_air_observation_keeps_bounded_unknown_extension_separately(tmp_path: Path) -> None:
    turn = run(tmp_path, "raw_extension")
    raw = turn.prompt_fact["air_observations"]
    assert raw[0]["providerExtension"] == {"nested": ["retained", {"shape": "exact"}]}
    assert "providerExtension" not in turn.prompt_fact["session_failures"][0]


def test_effective_air_record_preserves_title_and_details_separately(tmp_path: Path) -> None:
    # Use a normal terminal exchange so this asserts the effective, decoder
    # reconciled record rather than only the failure envelope above.
    # ``terminal_details`` shares the fake server's terminal path below.
    turn = run(tmp_path, "terminal_details")
    assert turn.prompt_fact["session_failures"] == [{
        "id": turn.prompt_fact["prompt_id"] + ":x", "revision": 1,
        "category": "service", "severity": "error", "title": "retry",
        "details": "provider detail", "actions": ["retry"],
    }]


def test_uncertain_cleanup_has_typed_failure_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate unavailable cgroup membership proof after the fake server has
    # exited.  The child is reaped, but that is deliberately insufficient to
    # claim its owned helper tree was contained.
    monkeypatch.setattr(AcpSubprocess, "_cgroup_empty", lambda self: None)
    with pytest.raises(AcpCleanupUncertain) as raised:
        run(tmp_path, "success")
    evidence = raised.value.evidence
    assert evidence.status == "cleanup_uncertain"
    assert evidence.cleanup_uncertain is True
    assert evidence.process and evidence.process["exit_code"] == 0


@pytest.mark.parametrize("scenario", ["malformed_request", "foreign_request", "unsupported_request"])
def test_fake_invalid_client_requests_are_answered_then_fail_closed(tmp_path: Path, scenario: str) -> None:
    with pytest.raises(AcpClientRejected, match="server request") as raised:
        run(tmp_path, scenario)
    assert raised.value.process["pid"] > 0
    assert isinstance(raised.value.shutdown, str)


@pytest.mark.parametrize("scenario,error", [("unsupported_profile", AcpClientRejected),
                                               ("unsupported", AcpClientRejected), ("reset_model", AcpClientRejected),
                                               ("eof", AcpTransportEof), ("timeout", AcpProcessTimeout)])
def test_fake_rejections_timeout_and_eof_fail_closed(tmp_path: Path, scenario: str, error: type[Exception]) -> None:
    with pytest.raises(error) as raised:
        run(tmp_path, scenario, timeout_s=.1 if scenario == "timeout" else 1)
    if scenario == "timeout":
        assert raised.value.timeout_phases == ()
        # The prompt budget expired even though cancellation and close both
        # completed within their own bounds.
        assert raised.value.process["timed_out"] is True
        assert raised.value.process["timeout_phases"] == []
        assert "intentional shutdown" in raised.value.shutdown
    else:
        assert raised.value.process["pid"] > 0
        assert isinstance(raised.value.shutdown, str)
        assert raised.value.process["timed_out"] is False


@pytest.mark.parametrize("scenario", ["bool_protocol_version", "bool_air_version"])
def test_fake_boolean_initialize_versions_are_rejected_before_session_creation(
    tmp_path: Path, scenario: str,
) -> None:
    with pytest.raises(AcpClientRejected, match="does not advertise") as raised:
        run(tmp_path, scenario)
    assert raised.value.process["exit_code"] == 0
    assert "intentional shutdown" in raised.value.shutdown


@pytest.mark.parametrize(("scenario", "phase"), [
    ("cancel_timeout", "cancel_response"), ("close_timeout", "session_close"),
])
def test_fake_expired_cancel_and_close_deadlines_are_distinct(
    tmp_path: Path, scenario: str, phase: str,
) -> None:
    with pytest.raises(AcpProcessTimeout) as raised:
        run(tmp_path, scenario, timeout_s=.05)
    assert phase in raised.value.timeout_phases
    assert phase in raised.value.process["timeout_phases"]


def test_restricted_policy_is_explicit_but_fails_closed_without_source_backing(tmp_path: Path) -> None:
    policy = CodexAcpPolicy.restricted(tmp_path)
    assert policy.cwd == tmp_path.resolve()
    assert policy.public_web_search and policy.network_access
    marker = tmp_path / "restricted-server-started"
    command = [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]
    with pytest.raises(AcpClientRejected, match="unsupported by the pinned artifact"):
        _run_codex_acp_turn(
            command, launch=verified_launch(command, profile="agent"), policy=policy,
            model="model", prompt="hello", log_path=tmp_path / "restricted.log",
        )
    assert not marker.exists()


def test_global_config_and_invalid_policy_cannot_override_pinned_launch(tmp_path: Path) -> None:
    bad = CodexAcpPolicy("restricted", tmp_path, tmp_path, False, False)  # type: ignore[arg-type]
    with pytest.raises(AcpClientRejected):
        bad.validate()
    unknown = CodexAcpPolicy("wrong", tmp_path, tmp_path)  # type: ignore[arg-type]
    with pytest.raises(AcpClientRejected):
        unknown.validate()
    env = {"HOME": "/global-config", "CODEX_CONFIG": "/global-config/config.toml",
           "CODEX_PATH": "bad", "INITIAL_AGENT_MODE": "full", "INHERITED_HOME": "/global-config"}
    assert run(tmp_path, "isolated", environment=env).prompt.kind == "success"


@pytest.mark.parametrize("overrides", [
    {"package_version": "1.11.1"},
    {"artifact_integrity": "sha512-unverified"},
    {"codex_version": "0.86.0"},
    {"sdk_version": "1.5.0"},
])
def test_unverified_launch_contract_is_rejected_before_server_starts(
    tmp_path: Path, overrides: dict[str, str],
) -> None:
    marker = tmp_path / "server-started"
    command = [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]
    with pytest.raises(AcpClientRejected, match="launch evidence"):
        _run_codex_acp_turn(command, launch=verified_launch(command, **overrides),
                            policy=CodexAcpPolicy.ordinary(tmp_path), model="model", prompt="hello",
                            log_path=tmp_path / "unverified.log")
    assert not marker.exists()


def test_launch_evidence_cannot_authorize_a_different_command(tmp_path: Path) -> None:
    expected = fake_server("success")
    marker = tmp_path / "wrong-server-started"
    actual = [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]
    with pytest.raises(AcpClientRejected, match="does not match"):
        _run_codex_acp_turn(actual, launch=verified_launch(expected),
                            policy=CodexAcpPolicy.ordinary(tmp_path), model="model", prompt="hello",
                            log_path=tmp_path / "wrong.log")
    assert not marker.exists()
