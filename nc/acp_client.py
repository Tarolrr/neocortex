"""Explicit, isolated client for one pinned Codex ACP turn.

This is deliberately not an Adapter.  Call :func:`run_codex_acp_turn` directly
from a future integration after its role and outcome handling have happened.
It starts one process, makes one fresh session and sends exactly one prompt.
"""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .acp_contract import AcpProcessFact, AcpPromptFact, is_air_session_failure
from .acp_decoder import AcpPromptResult, decode_acp_prompt_result
from .acp_stream import AcpDeadlineExpired, AcpStreamError
from .acp_wire import AcpProcessError, AcpProcessTimeout, AcpSubprocess

_MODE = "agent"
_PINNED_ACP_PACKAGE = "@agentclientprotocol/codex-acp"
_PINNED_ACP_VERSION = "1.11.0"
_PINNED_ACP_INTEGRITY = (
    "sha512-opPKsRaekgdmQpOpHrR0EEDn9chgtiN+b+h0V78fTuQP84TNzB7vrn3EtKODwbiJQTBHJAlynjSFQazFfaT+VQ=="
)
_PINNED_CODEX_VERSION = "0.153.4"
_PINNED_SDK_VERSION = "1.4.0"
_PROTECTED_ENV = frozenset({
    "CODEX_CONFIG", "CODEX_PATH", "INITIAL_AGENT_MODE", "CODEX_HOME", "HOME",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME",
})


class AcpClientRejected(RuntimeError):
    """A profile, policy, or server capability was rejected before prompting."""


@dataclass(frozen=True)
class CodexAcpLaunchEvidence:
    """Recorded result of the isolated pinned-artifact launch verification.

    This is deliberately supplied by the future deployment verifier rather
    than guessed from an executable name.  The client consumes it before it
    starts a child: it binds the exact command to the artifact integrity and
    resolved dependency versions.  The initialize response below supplies the
    remaining, live handshake part of this contract.
    """

    command: tuple[str, ...]
    package: str
    package_version: str
    artifact_integrity: str
    codex_version: str
    sdk_version: str

    def validate_for(self, command: Sequence[str]) -> None:
        if tuple(command) != self.command:
            raise AcpClientRejected("launch command does not match verified ACP evidence")
        if (self.package != _PINNED_ACP_PACKAGE
                or self.package_version != _PINNED_ACP_VERSION
                or self.artifact_integrity != _PINNED_ACP_INTEGRITY
                or self.codex_version != _PINNED_CODEX_VERSION
                or self.sdk_version != _PINNED_SDK_VERSION):
            raise AcpClientRejected("launch evidence does not match pinned ACP contract")


@dataclass(frozen=True)
class CodexAcpPolicy:
    """The two documented launch inputs; neither grants client tools or MCP."""

    kind: Literal["ordinary", "restricted"]
    cwd: Path
    workspace_root: Path
    public_web_search: bool = False
    network_access: bool = False

    @classmethod
    def ordinary(cls, cwd: Path) -> CodexAcpPolicy:
        return cls("ordinary", cwd.resolve(), cwd.resolve())

    @classmethod
    def restricted(cls, run_directory: Path) -> CodexAcpPolicy:
        # This is an executable, isolated launch profile.  Its settings are
        # written into the only configuration home visible to the child; they
        # are not advisory values inherited from the host.
        path = run_directory.resolve()
        return cls("restricted", path, path, public_web_search=True, network_access=True)

    def validate(self) -> None:
        if self.kind not in {"ordinary", "restricted"}:
            raise AcpClientRejected("unknown ACP policy kind")
        if not self.cwd.is_absolute() or not self.workspace_root.is_absolute():
            raise AcpClientRejected("ACP cwd and workspace root must be absolute")
        if not self.cwd.is_dir() or not self.workspace_root.is_dir():
            raise AcpClientRejected("ACP cwd and workspace root must exist")
        if self.kind == "restricted" and self.cwd != self.workspace_root:
            raise AcpClientRejected("restricted ACP must preserve its run-directory cwd")
        if self.kind == "restricted" and not (self.public_web_search and self.network_access):
            raise AcpClientRejected("restricted ACP requires pinned web and network settings")
        if self.kind == "ordinary" and (self.public_web_search or self.network_access):
            raise AcpClientRejected("ordinary ACP cannot request web or network access")


@dataclass(frozen=True)
class CodexAcpTurn:
    """Prompt facts and independently observed local process facts."""

    prompt: AcpPromptResult
    prompt_fact: AcpPromptFact
    process: AcpProcessFact
    shutdown: str | None
    live_sandbox_enforcement_verified: Literal[False] = False


def _safe_environment(
    base: Mapping[str, str] | None = None, *, config_home: Path,
) -> dict[str, str]:
    """Isolate Codex configuration without touching inherited credentials."""
    env = dict(os.environ if base is None else base)
    for name in _PROTECTED_ENV:
        env.pop(name, None)
    # Codex uses this home for both configuration and its auth cache.  This
    # deliberately does not copy, alter, or authenticate with the caller's
    # credentials: a future live activation must arrange its own verified auth
    # boundary.  The isolated config below is the only policy authority.
    for name in ("HOME", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        env[name] = str(config_home)
    return env


def _write_pinned_config(policy: CodexAcpPolicy, config_home: Path) -> None:
    """Write the sole Codex configuration consulted by the ACP child.

    ACP config negotiation explicitly selects the pinned ``agent`` execution
    policy too.  This launch boundary prevents global configuration from
    changing the requested web/network settings, or changing the
    noninteractive request handler.  The source-pinned ACP mode subsequently
    owns its live sandbox tuple; this client records no claim that its private
    config request has been enforced by a real Codex process.
    """
    config_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    network = "true" if policy.network_access else "false"
    search = '"live"' if policy.public_web_search else '"disabled"'
    (config_home / "config.toml").write_text(
        "approval_policy = \"on-request\"\n"
        "sandbox_mode = \"workspace-write\"\n"
        f"web_search = {search}\n"
        "[sandbox_workspace_write]\n"
        f"network_access = {network}\n"
    )


def _options(response: object) -> list[dict[str, object]]:
    if not isinstance(response, dict) or not isinstance(response.get("configOptions"), list):
        raise AcpClientRejected("server did not return config options")
    options = response["configOptions"]
    if not all(isinstance(option, dict) for option in options):
        raise AcpClientRejected("server config options are malformed")
    return options  # type: ignore[return-value]


def _offered(options: list[dict[str, object]], config_id: str, value: str) -> bool:
    option = next((item for item in options if item.get("id") == config_id), None)
    return isinstance(option, dict) and isinstance(option.get("options"), list) and any(
        isinstance(choice, dict) and choice.get("value") == value for choice in option["options"]
    )


def _selected(response: object, config_id: str, value: str) -> bool:
    return any(option.get("id") == config_id and option.get("currentValue") == value
               for option in _options(response))


def _air_advertised(response: object) -> bool:
    if not isinstance(response, dict) or response.get("protocolVersion") != 1:
        return False
    try:
        air = response["_meta"]["jetbrains"]["air"]  # type: ignore[index]
    except (KeyError, TypeError):
        return False
    return (isinstance(air, dict) and air.get("version") == 1
            and isinstance(air.get("capabilities"), list)
            and "sessionFailure" in air["capabilities"])


def _process_fact(child: AcpSubprocess, timed_out: bool,
                  timeout_phases: Sequence[str] = ()) -> AcpProcessFact:
    code = child.proc.returncode
    return {
        "pid": child.proc.pid,
        "exit_code": code if code is None or code >= 0 else None,
        "signal": -code if isinstance(code, int) and code < 0 else None,
        "timed_out": timed_out,
        "timeout_phases": list(timeout_phases) if timed_out else [],
        "stderr_available": bool(child.diagnostics),
    }


def _air_observations(wire: Sequence[object], session_id: str,
                      prompt_id: int) -> list[object]:
    """Retain AIR observations correlated to this session's active prompt.

    Setup replies are deliberately excluded even if they carry AIR-shaped
    metadata: they have no prompt ownership.  The prompt response itself and
    matching ``session/update`` notifications observed after its request and
    before its response are the only evidence for this turn.
    """
    observed: list[object] = []
    prompt_active = False
    for item in wire:
        if not isinstance(item, dict):
            continue
        value: object | None = None
        if (item.get("method") == "session/prompt" and item.get("id") == prompt_id
                and isinstance(item.get("params"), dict)
                and item["params"].get("sessionId") == session_id):
            prompt_active = True
            continue
        if prompt_active and item.get("method") == "session/update":
            params = item.get("params")
            if isinstance(params, dict) and params.get("sessionId") == session_id:
                value = params.get("update")
        elif prompt_active and item.get("id") == prompt_id and "result" in item:
            value = item["result"]
            prompt_active = False
        if not isinstance(value, dict):
            continue
        try:
            failure = value["_meta"]["jetbrains"]["air"]["sessionFailure"]  # type: ignore[index]
        except (KeyError, TypeError):
            continue
        # A session can deliver a late failure from an earlier prompt during
        # this request's window.  Match the decoder's incident ownership rule
        # so prompt facts cannot attribute it to this invocation.
        if (isinstance(failure, dict) and isinstance(failure.get("id"), str)
                and failure["id"].startswith(f"{prompt_id}:")):
            observed.append(failure)
    return observed


def run_codex_acp_turn(command: Sequence[str], *, launch: CodexAcpLaunchEvidence,
                       policy: CodexAcpPolicy, model: str, prompt: str,
                       log_path: Path, timeout_s: float = 60,
                       environment: Mapping[str, str] | None = None) -> CodexAcpTurn:
    """Run the pinned initialize/new/configure/prompt sequence once.

    ``launch`` is the verified codex-acp 1.11.0 artifact/dependency evidence
    bound to ``command``.  It is checked before a child exists; the live ACP
    v1/AIR handshake is then checked before session creation.  No installation,
    authentication, outcome-file handling, or role validation is performed
    here.  Exceptions are deliberate fail-closed evidence.
    """
    policy.validate()
    if not command or not model or not prompt or timeout_s <= 0:
        raise ValueError("command, model, prompt, and positive timeout are required")
    launch.validate_for(command)
    # The prompt budget is separate from the prescribed bounded cancellation
    # and cleanup phases.  The supervisor still owns one outer deadline.
    deadline = time.monotonic() + timeout_s + 15
    wire: list[object] = []
    session_id: str | None = None
    # AcpJsonRpcStream must reply to a peer request before it can resume the
    # host request it was pumping.  Retain a policy-owner rejection across
    # that JSON-RPC error reply so a cooperative (or racing) prompt response
    # can never turn the invocation into a success.
    server_request_rejection: AcpClientRejected | None = None

    def raise_server_request_rejection() -> None:
        if server_request_rejection is not None:
            raise server_request_rejection

    def observe_notification(value: dict[str, object]) -> None:
        wire.append(value)

    def deny_request(value: dict[str, object]) -> object:
        nonlocal server_request_rejection
        wire.append(value)
        method = value.get("method")
        params = value.get("params")
        if (not isinstance(params, dict) or session_id is None
                or params.get("sessionId") != session_id):
            server_request_rejection = AcpClientRejected("malformed or foreign server request")
            raise server_request_rejection
        if method == "session/request_permission":
            # ACP v1 has no ``denied`` permission outcome.  Cancellation is
            # the pinned, fail-closed response shape.
            return {"outcome": {"outcome": "cancelled"}}
        if method == "elicitation/create":
            return {"action": "cancel", "content": None}
        server_request_rejection = AcpClientRejected("unsupported server request")
        raise server_request_rejection

    log_path.parent.mkdir(parents=True, exist_ok=True)
    config_home = Path(tempfile.mkdtemp(prefix="nc-acp-codex-", dir=log_path.parent))
    _write_pinned_config(policy, config_home)
    child = AcpSubprocess(command, cwd=policy.cwd, deadline=deadline, log_path=log_path,
                          env=_safe_environment(environment, config_home=config_home))
    result: CodexAcpTurn | None = None
    timed_out = False
    # These are only *expired* bounded cleanup phases; successful protocol
    # operations and the original prompt deadline are not process facts.
    timeout_phases: list[str] = []
    failure: BaseException | None = None
    try:
        stream = child.stream(notification_handler=observe_notification, request_handler=deny_request)

        init_params: dict[str, object] = {"protocolVersion": 1, "clientCapabilities": {
            "_meta": {"jetbrains": {"air": {"version": 1, "capabilities": ["sessionFailure"]}}},
        }}
        init_id = stream.send_request("initialize", init_params)
        wire.append({"jsonrpc": "2.0", "id": init_id, "method": "initialize", "params": init_params})
        init_response = stream.wait_for(init_id)
        raise_server_request_rejection()
        wire.append(init_response)
        if not _air_advertised(init_response.get("result")):
            raise AcpClientRejected("server does not advertise pinned ACP/AIR profile")

        new_params: dict[str, object] = {"cwd": str(policy.cwd), "mcpServers": []}
        new_id = stream.send_request("session/new", new_params)
        wire.append({"jsonrpc": "2.0", "id": new_id, "method": "session/new", "params": new_params})
        new_response = stream.wait_for(new_id)
        raise_server_request_rejection()
        wire.append(new_response)
        new_result = new_response.get("result")
        if not isinstance(new_result, dict) or not isinstance(new_result.get("sessionId"), str):
            raise AcpClientRejected("server did not allocate a valid fresh session")
        session_id = new_result["sessionId"]
        capabilities = new_result.get("sessionCapabilities")
        close_advertised = isinstance(capabilities, dict) and capabilities.get("close") is True
        options = _options(new_result)
        if not _offered(options, "model", model) or not _offered(options, "mode", _MODE):
            raise AcpClientRejected("requested model or pinned execution policy is unsupported")

        for config_id, value in (("model", model), ("mode", _MODE)):
            params: dict[str, object] = {"sessionId": session_id, "configId": config_id, "value": value}
            config_id_request = stream.send_request("session/set_config_option", params)
            wire.append({"jsonrpc": "2.0", "id": config_id_request,
                         "method": "session/set_config_option", "params": params})
            response = stream.wait_for(config_id_request)
            raise_server_request_rejection()
            wire.append(response)
            if not _selected(response.get("result"), config_id, value):
                raise AcpClientRejected("server rejected explicit ACP configuration")

        # A set-config response is a complete option snapshot, not an
        # acknowledgement for only the option named by its request.  In
        # particular, a mode update must not be allowed to silently reset or
        # omit the selected model before dispatching the prompt.
        if not (_selected(response.get("result"), "model", model)
                and _selected(response.get("result"), "mode", _MODE)):
            raise AcpClientRejected("server did not retain the pinned model and execution policy")

        prompt_params: dict[str, object] = {"sessionId": session_id,
                                            "prompt": [{"type": "text", "text": prompt}]}
        prompt_id = stream.send_request("session/prompt", prompt_params)
        wire.append({"jsonrpc": "2.0", "id": prompt_id, "method": "session/prompt",
                     "params": prompt_params})
        try:
            prompt_response = stream.wait_for(prompt_id, deadline=time.monotonic() + timeout_s)
            raise_server_request_rejection()
        except (AcpDeadlineExpired, AcpProcessTimeout) as exc:
            timed_out = True
            cancel_deadline = time.monotonic() + 10
            # A total-bound expiry during a live prompt still owes the pinned
            # cancellation sequence.  Temporarily extend only the supervisor
            # guard through that independently bounded cleanup window; every
            # following wire operation supplies its shorter own deadline.
            if isinstance(exc, AcpProcessTimeout):
                child.deadline = cancel_deadline + 5
            try:
                stream.notify(
                    "session/cancel", {"sessionId": session_id}, deadline=cancel_deadline,
                )
                prompt_response = stream.wait_for(prompt_id, deadline=cancel_deadline)
            except (AcpProcessError, AcpStreamError, AcpDeadlineExpired):
                timeout_phases.append("cancel_response")
            if close_advertised:
                close_deadline = time.monotonic() + 5
                try:
                    stream.request(
                        "session/close", {"sessionId": session_id}, deadline=close_deadline,
                    )
                except (AcpProcessError, AcpStreamError, AcpDeadlineExpired):
                    timeout_phases.append("session_close")
            error = AcpProcessTimeout("ACP prompt deadline expired after bounded cancellation")
            error.timeout_phases = tuple(timeout_phases)
            raise error from exc
        wire.append(prompt_response)
        # The transport intentionally uses compact numeric JSON-RPC ids, while
        # AIR incident ownership is text-prefixed.  Give the pure decoder its
        # pinned textual correlation view without changing the captured facts.
        decoder_wire = [dict(item, id=str(prompt_id)) if isinstance(item, dict)
                        and item.get("id") == prompt_id else item for item in wire]
        decoded = decode_acp_prompt_result(decoder_wire, request_id=str(prompt_id), session_id=session_id)
        observations = _air_observations(wire, session_id, prompt_id)
        prompt_fact: AcpPromptFact = {
            "request_id": str(prompt_id), "session_id": session_id, "prompt_id": str(prompt_id),
            "prompt_response_valid": decoded.kind != "protocol_invalid",
            "stop_reason": decoded.stop_reason, "air_observations": observations,
            "session_failures": [item for item in observations if is_air_session_failure(item)],
        }
        if "result" in prompt_response and isinstance(prompt_response["result"], dict):
            prompt_fact["jsonrpc_result"] = prompt_response["result"]
        if "error" in prompt_response and isinstance(prompt_response["error"], dict):
            prompt_fact["jsonrpc_error"] = prompt_response["error"]
        # A completed response is not permission to hide a process which has
        # already failed.  Yield briefly so an immediate post-response exit
        # is observed before this client starts deliberate cleanup.
        child.check_post_response_failure()
        result = CodexAcpTurn(decoded, prompt_fact, _process_fact(child, False), None)
    except AcpProcessTimeout as exc:
        timed_out = True
        failure = exc
        raise
    except BaseException as exc:
        # Every post-launch failure carries facts independent of the ACP
        # response.  This includes EOF, a nonzero exit, malformed requests,
        # and profile/configuration rejection; callers never have to infer
        # local process state from an exception message.
        failure = exc
        raise
    finally:
        child.close()
        timeout_phases.extend(child.timeout_phases)
        # A server may reply successfully, outlive the short post-response
        # observation, then crash while orderly stdin-EOF cleanup waits for
        # it.  That is not an intentional shutdown and cannot be hidden by
        # successful prompt evidence.
        if failure is None and child.shutdown_failure is not None:
            failure = child.shutdown_failure
        if failure is not None:
            process = _process_fact(child, bool(timeout_phases), timeout_phases)
            failure.process = process
            failure.shutdown = child.shutdown_outcome
            if child.shutdown_failure is not None:
                raise failure
            if isinstance(failure, AcpProcessTimeout):
                failure.timeout_phases = tuple(timeout_phases)
    assert result is not None
    timed_out = timed_out or bool(timeout_phases)
    return CodexAcpTurn(result.prompt, result.prompt_fact, _process_fact(child, timed_out, timeout_phases),
                         child.shutdown_outcome)
