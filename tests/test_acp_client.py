"""End-to-end coverage for the isolated client using a local fake server."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nc.acp_client import AcpClientRejected, CodexAcpPolicy, run_codex_acp_turn
from nc.acp_wire import AcpProcessTimeout, AcpTransportEof


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
    response(request, {"configOptions":options})
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
    assert read()["result"]["outcome"]["outcome"] == "denied"
if scenario == "elicitation":
    send({"jsonrpc":"2.0","id":"elicit","method":"elicitation/create","params":{"sessionId":"fresh"}})
    assert read()["result"] == {"action":"cancel", "content":None}
if scenario == "malformed_request":
    send({"jsonrpc":"2.0","id":"bad","method":"session/request_permission","params":{}})
    assert read()["error"]["code"] == -32603
if scenario == "error":
    send({"jsonrpc":"2.0","id":prompt["id"],"error":{"code":-1,"message":"bad"}}); time.sleep(.2); sys.exit(0)
if scenario == "malformed":
    response(prompt, {"stopReason":"end_turn","_meta":{"jetbrains":{"air":{"version":2}}}}); time.sleep(.2); sys.exit(0)
failure = {"id":str(prompt["id"])+":x","revision":1,"category":"service","severity":"warning","title":"retry","actions":["retry"]}
if scenario == "warning": send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"fresh","update":{"_meta":{"jetbrains":{"air":{"version":1,"sessionFailure":failure}}}}}})
if scenario == "terminal":
    failure["severity"] = "error"
    response(prompt, {"stopReason":"end_turn","_meta":{"jetbrains":{"air":{"version":1,"sessionFailure":failure}}}})
else: response(prompt, {"stopReason":"end_turn","usage":{"inputTokens":2,"outputTokens":3}})
time.sleep(.2)
'''
    return [sys.executable, "-c", code, scenario]


def run(tmp_path: Path, scenario: str, **kwargs: object):
    return run_codex_acp_turn(fake_server(scenario), policy=CodexAcpPolicy.ordinary(tmp_path),
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


@pytest.mark.parametrize("scenario", ["elicitation", "malformed_request"])
def test_fake_client_requests_are_cancelled_or_fail_closed(tmp_path: Path, scenario: str) -> None:
    assert run(tmp_path, scenario).prompt.kind == "success"


@pytest.mark.parametrize("scenario,error", [("unsupported", AcpClientRejected), ("eof", AcpTransportEof),
                                               ("timeout", AcpProcessTimeout)])
def test_fake_rejections_timeout_and_eof_fail_closed(tmp_path: Path, scenario: str, error: type[Exception]) -> None:
    with pytest.raises(error) as raised:
        run(tmp_path, scenario, timeout_s=.1 if scenario == "timeout" else 1)
    if scenario == "timeout":
        assert raised.value.timeout_phases == (
            "prompt_deadline", "cancel_sent", "cancel_response", "close_response",
        )
        assert raised.value.process["timed_out"] is True
        assert "intentional shutdown" in raised.value.shutdown


def test_restricted_policy_and_environment_are_explicit(tmp_path: Path) -> None:
    policy = CodexAcpPolicy.restricted(tmp_path)
    assert policy.cwd == tmp_path.resolve()
    assert policy.public_web_search and policy.network_access
    assert run_codex_acp_turn(fake_server("restricted"), policy=policy, model="model", prompt="hello",
                              log_path=tmp_path / "restricted.log", timeout_s=1).prompt.kind == "success"


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
