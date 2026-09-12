"""End-to-end coverage for the isolated client using a local fake server."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

from nc.acp_client import (
    AcpClientRejected,
    CodexAcpLaunchEvidence,
    CodexAcpPolicy,
    run_codex_acp_turn,
)
from nc.acp_wire import AcpProcessTimeout, AcpTransportEof, AcpUnexpectedExit


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
if scenario in {"restricted", "isolated"}:
    home = os.environ["HOME"]
    config = open(os.path.join(home, "config.toml")).read()
    assert os.environ["CODEX_HOME"] == home
    assert "CODEX_CONFIG" not in os.environ
    assert "sandbox_mode = \"workspace-write\"" in config
    if scenario == "restricted":
        assert "web_search = \"live\"" in config and "network_access = true" in config
    else:
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
response(init, {"protocolVersion":1, "_meta":{"jetbrains":{"air":{"version":1,"capabilities":["sessionFailure"]}}}})
new = read()
options = [{"id":"model","options":[{"value":"model"}],"currentValue":"x"}, {"id":"mode","options":[{"value":"agent"}],"currentValue":"x"}]
if scenario == "unsupported": options[0]["options"] = []
response(new, {"sessionId":"fresh", "configOptions":options,
               "sessionCapabilities":{"close": scenario == "timeout"}})
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
if scenario == "timeout":
    cancel = read()
    assert cancel["method"] == "session/cancel"
    # This is deliberately late: it proves cancellation is not completion.
    response(prompt, {"stopReason":"end_turn"})
    close = read()
    assert close["method"] == "session/close"
    response(close, {})
    time.sleep(.1); sys.exit(0)
if scenario == "permission":
    send({"jsonrpc":"2.0","id":"permission","method":"session/request_permission","params":{"sessionId":"fresh"}})
    assert read()["result"] == {"outcome":{"outcome":"cancelled"}}
if scenario == "elicitation":
    send({"jsonrpc":"2.0","id":"elicit","method":"elicitation/create","params":{"sessionId":"fresh"}})
    assert read()["result"] == {"action":"cancel", "content":None}
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
if scenario == "error":
    send({"jsonrpc":"2.0","id":prompt["id"],"error":{"code":-1,"message":"bad"}}); time.sleep(.2); sys.exit(0)
if scenario == "malformed":
    response(prompt, {"stopReason":"end_turn","_meta":{"jetbrains":{"air":{"version":2}}}}); time.sleep(.2); sys.exit(0)
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
if scenario == "foreign_incident":
    # Same session, but a late failure emitted by an earlier prompt.
    failure["id"] = "previous-prompt:x"
    failure["severity"] = "error"
    send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"fresh","update":{"_meta":{"jetbrains":{"air":{"version":1,"sessionFailure":failure}}}}}})
if scenario == "terminal":
    failure["severity"] = "error"
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
    }
    values.update(overrides)
    return CodexAcpLaunchEvidence(**values)  # type: ignore[arg-type]


def run(tmp_path: Path, scenario: str, **kwargs: object):
    command = fake_server(scenario)
    return run_codex_acp_turn(command, launch=verified_launch(command),
                               policy=CodexAcpPolicy.ordinary(tmp_path),
                               model="model", prompt="hello", log_path=tmp_path / "acp.log",
                               timeout_s=kwargs.pop("timeout_s", 1), **kwargs)


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


def test_fake_elicitation_is_cancelled_noninteractively(tmp_path: Path) -> None:
    assert run(tmp_path, "elicitation").prompt.kind == "success"


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
        assert raised.value.process["timed_out"] is False
        assert raised.value.process["timeout_phases"] == []
        assert "intentional shutdown" in raised.value.shutdown
    else:
        assert raised.value.process["pid"] > 0
        assert isinstance(raised.value.shutdown, str)
        assert raised.value.process["timed_out"] is False


def test_restricted_policy_is_rejected_before_dispatch_when_agent_mode_cannot_preserve_network(
    tmp_path: Path,
) -> None:
    policy = CodexAcpPolicy.restricted(tmp_path)
    assert policy.cwd == tmp_path.resolve()
    assert policy.public_web_search and policy.network_access
    # The pinned source says selecting ``mode=agent`` makes effective
    # workspace-write networkAccess false.  The requested true setting in a
    # private config file would therefore be overridden.  The client must not
    # start an ACP process and present that request as restricted support.
    source = json.loads((Path(__file__).parent / "fixtures" /
                         "codex-acp-agent-tool-path.source.json").read_text())
    assert source["selected_mode"]["sandboxPolicy"]["networkAccess"] is False
    marker = tmp_path / "restricted-server-started"
    command = [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]
    with pytest.raises(AcpClientRejected, match="pinned agent mode disables network access"):
        run_codex_acp_turn(command, launch=verified_launch(command), policy=policy,
                            model="model", prompt="hello", log_path=tmp_path / "restricted.log",
                            timeout_s=1)
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
        run_codex_acp_turn(command, launch=verified_launch(command, **overrides),
                            policy=CodexAcpPolicy.ordinary(tmp_path), model="model", prompt="hello",
                            log_path=tmp_path / "unverified.log")
    assert not marker.exists()


def test_launch_evidence_cannot_authorize_a_different_command(tmp_path: Path) -> None:
    expected = fake_server("success")
    marker = tmp_path / "wrong-server-started"
    actual = [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]
    with pytest.raises(AcpClientRejected, match="does not match"):
        run_codex_acp_turn(actual, launch=verified_launch(expected),
                            policy=CodexAcpPolicy.ordinary(tmp_path), model="model", prompt="hello",
                            log_path=tmp_path / "wrong.log")
    assert not marker.exists()
