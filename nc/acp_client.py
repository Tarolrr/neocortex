"""Explicit, isolated client for one pinned Codex ACP turn.

This is deliberately not an Adapter.  Call :func:`run_codex_acp_turn` directly
from a future integration after its role and outcome handling have happened.
It starts one process, makes one fresh session and sends exactly one prompt.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .acp_contract import AcpProcessFact, AcpPromptFact
from .acp_decoder import AcpPromptResult, _usage, decode_acp_prompt_result
from .acp_stream import AcpDeadlineExpired, AcpStreamError
from .acp_wire import AcpProcessError, AcpProcessTimeout, AcpSubprocess

_MAX_EVIDENCE_RECORDS = 128
_MAX_EVIDENCE_BYTES = 64 * 1024
_MAX_AIR_TEXT_BYTES = 4 * 1024
_MAX_AIR_ACTIONS = 64
_MAX_AIR_ACTION_BYTES = 256
_MAX_RAW_AIR_FAILURE_BYTES = 8 * 1024
_MAX_JSONRPC_ERROR_BYTES = 4 * 1024
_MISSING = object()

_ORDINARY_MODE = "agent"
# These are the effective sandbox values of ``AgentMode.Agent`` in the pinned
# ACP artifact, not desired values supplied by an incoming request.  They are
# kept beside the selected mode so that a policy whose requirements conflict
# with that mode is rejected before a child or private config is created.
_PINNED_MODE_SANDBOX = "workspaceWrite"
_PINNED_MODE_NETWORK_ACCESS = False
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

    def __init__(self, message: str, *, log_path: str = "") -> None:
        super().__init__(message)
        # Pre-launch rejection has no child facts, but consumers still get a
        # stable envelope instead of scraping an exception string.
        self.evidence = AcpTurnEvidence(None, None, None, None, False, log_path, "setup_rejected")


class AcpEvidenceOverflow(RuntimeError):
    """Authoritative correlated evidence exceeded the published local bound."""


class AcpCleanupUncertain(RuntimeError):
    """Owned ACP helpers could not be proven contained after cleanup."""


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
    profile: str

    def validate_for(self, command: Sequence[str], profile: str) -> None:
        if tuple(command) != self.command:
            raise AcpClientRejected("launch command does not match verified ACP evidence")
        if (self.package != _PINNED_ACP_PACKAGE
                or self.package_version != _PINNED_ACP_VERSION
                or self.artifact_integrity != _PINNED_ACP_INTEGRITY
                or self.codex_version != _PINNED_CODEX_VERSION
                or self.sdk_version != _PINNED_SDK_VERSION):
            raise AcpClientRejected("launch evidence does not match pinned ACP contract")
        if self.profile != profile:
            raise AcpClientRejected("launch evidence does not bind the selected ACP profile")


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
        # Restricted runs deliberately own only their run directory.  The
        # child also receives a private HOME, so a repository or runtime home
        # cannot become an implicit writable/configuration root.
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


@dataclass(frozen=True)
class AcpTurnEvidence:
    """Stable local envelope attached to every post-launch failure.

    The collector retains at most 128 correlated AIR/terminal records and
    64 KiB of their JSON representation.  Usage is a single independent last
    complete snapshot, rather than a notification history.  Each raw AIR
    ``sessionFailure`` is retained as exact JSON only up to 8 KiB; the
    separate decoder projection limits AIR text to 4 KiB per field and
    actions to 64 x 256 bytes.  It never retains transcript or tool updates.
    Overflow is a failure, rather than a dropped observation.
    """

    prompt: AcpPromptFact | None
    usage: dict[str, int] | None
    process: AcpProcessFact | None
    shutdown: str | None
    cleanup_uncertain: bool
    log_path: str
    status: str


class _TurnEvidenceCollector:
    """Bounded, correlated input for the pure decoder (not a wire transcript)."""

    def __init__(self) -> None:
        self.records: list[object] = []
        self.prompt_id: int | None = None
        self.session_id: str | None = None
        self.bytes = 0
        self.overflow = False
        self.air_observations: list[object] = []
        self.usage: dict[str, int] | None = None

    def _add(self, value: object, raw_air_failure: object = _MISSING) -> None:
        try:
            size = len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode())
        except (TypeError, ValueError):
            self.overflow = True
            raise AcpEvidenceOverflow("ACP evidence-overflow: unserializable authoritative evidence")
        raw_size = 0
        if raw_air_failure is not _MISSING:
            try:
                raw_size = len(json.dumps(raw_air_failure, separators=(",", ":"), ensure_ascii=False).encode())
            except (TypeError, ValueError):
                self.overflow = True
                raise AcpEvidenceOverflow("ACP evidence-overflow: unserializable raw AIR evidence")
        if len(self.records) >= _MAX_EVIDENCE_RECORDS or self.bytes + size + raw_size > _MAX_EVIDENCE_BYTES:
            self.overflow = True
            raise AcpEvidenceOverflow("ACP evidence-overflow: correlated evidence limit exceeded")
        self.records.append(value)
        self.bytes += size + raw_size
        if raw_air_failure is not _MISSING:
            self.air_observations.append(raw_air_failure)

    def _raw_failure(self, update: object) -> object:
        """Return a detached, bounded exact sessionFailure value if present."""
        present, value = _raw_air_failure(update)
        if not present:
            return _MISSING
        try:
            encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
        except (TypeError, ValueError):
            self.overflow = True
            self.air_observations.append({"evidence_status": "evidence_overflow",
                                          "reason": "unserializable AIR sessionFailure"})
            raise AcpEvidenceOverflow("ACP evidence-overflow: unserializable raw AIR evidence")
        if len(encoded) > _MAX_RAW_AIR_FAILURE_BYTES:
            self.overflow = True
            self.air_observations.append({"evidence_status": "evidence_overflow",
                                          "reason": "AIR sessionFailure exceeds local limit"})
            raise AcpEvidenceOverflow("ACP evidence-overflow: raw AIR sessionFailure exceeds local limit")
        # A JSON round trip detaches a mutable parser object and is the exact
        # bounded wire value, including unknown fields and malformed shapes.
        return json.loads(encoded)

    def _mark_raw_overflow(self, reason: str) -> None:
        """Expose loss explicitly when a decoder-bound projection overflows."""
        self.overflow = True
        self.air_observations.append({"evidence_status": "evidence_overflow", "reason": reason})

    def prompt_request(self, value: dict[str, object], prompt_id: int, session_id: str) -> None:
        self.prompt_id, self.session_id = prompt_id, session_id
        # Never retain caller prompt text.
        self._add({"jsonrpc": value.get("jsonrpc"), "id": prompt_id,
                   "method": "session/prompt", "params": {"sessionId": session_id}})

    def notification(self, value: dict[str, object]) -> None:
        """Keep only update fields which the decoder is allowed to inspect."""
        if value.get("method") != "session/update" or self.session_id is None:
            return
        params = value.get("params")
        if not isinstance(params, dict) or params.get("sessionId") != self.session_id:
            return
        update = params.get("update")
        if not isinstance(update, dict):
            return
        raw_failure = self._raw_failure(update)
        try:
            air = _project_air(update)
        except AcpEvidenceOverflow:
            self._mark_raw_overflow("AIR decoder projection exceeds local limit")
            raise
        usage = _project_usage(update.get("usage")) if update.get("sessionUpdate") == "usage_update" else None
        if air is None and usage is None:
            return
        projected: dict[str, object] = {}
        if air is not None:
            projected["_meta"] = {"jetbrains": {"air": air}}
        if usage is not None:
            # Usage reports are snapshots.  Do not turn a notification flood
            # into a bounded transcript merely to give the decoder a value.
            # ``decoder_wire`` below injects this one final snapshot.
            self.usage = usage
        if air is not None:
            record = {"method": "session/update", "params": {
                "sessionId": self.session_id, "update": projected,
            }}
            # Commit the raw audit observation only with its bounded decoder
            # record.  In particular, a rejected overflow record cannot leak
            # through the failure envelope beyond the published limit.
            self._add(record, raw_failure if raw_failure is not _MISSING
                      and _incident_belongs(raw_failure, self.prompt_id) else _MISSING)

    def prompt_response(self, value: object) -> None:
        if not isinstance(value, dict):
            self._add(value)
            return
        # Prompt result extensions may carry arbitrary content too.  The
        # decoder has authority only over these terminal fields.
        retained: dict[str, object] = {"id": value.get("id")}
        raw_failure = _MISSING
        if "result" in value:
            result = value["result"]
            if isinstance(result, dict):
                raw_failure = self._raw_failure(result)
                terminal: dict[str, object] = {}
                if "stopReason" in result:
                    # A non-string is retained only as the invalid marker;
                    # arbitrary extension objects never enter evidence.
                    terminal["stopReason"] = (result["stopReason"]
                                               if isinstance(result["stopReason"], str) else None)
                usage = _project_usage(result.get("usage"))
                if usage is not None:
                    terminal["usage"] = usage
                    self.usage = usage
                try:
                    air = _project_air(result)
                except AcpEvidenceOverflow:
                    self._mark_raw_overflow("AIR decoder projection exceeds local limit")
                    raise
                if air is not None:
                    terminal["_meta"] = {"jetbrains": {"air": air}}
                retained["result"] = terminal
            else:
                retained["result"] = None
        if "error" in value:
            error = value["error"]
            if isinstance(error, dict):
                message = error.get("message")
                retained["error"] = {
                    "code": error.get("code") if isinstance(error.get("code"), int)
                    and not isinstance(error.get("code"), bool) else None,
                    "message": _bounded_text(message, _MAX_JSONRPC_ERROR_BYTES, "JSON-RPC error")
                    if isinstance(message, str) else None,
                }
            else:
                retained["error"] = None
        self._add(retained, raw_failure if raw_failure is not _MISSING
                  and _incident_belongs(raw_failure, self.prompt_id) else _MISSING)


def _project_usage(value: object) -> dict[str, int] | None:
    """Keep only a complete, bounded usage snapshot (never provider extras)."""
    decoded = _usage(value)
    if decoded is None or decoded.input_tokens is None or decoded.output_tokens is None:
        return None
    result = {"inputTokens": decoded.input_tokens, "outputTokens": decoded.output_tokens}
    if decoded.cached_input_tokens is not None:
        result["cachedInputTokens"] = decoded.cached_input_tokens
    if decoded.total_tokens is not None:
        result["totalTokens"] = decoded.total_tokens
    return result


def _project_air(update: object) -> dict[str, object] | None:
    if not isinstance(update, dict) or not isinstance(update.get("_meta"), dict):
        return None
    try:
        air = update["_meta"]["jetbrains"]["air"]
    except (KeyError, TypeError):
        return None
    if not isinstance(air, dict):
        return {"version": None}
    projected = {"version": air.get("version") if isinstance(air.get("version"), int)
                 and not isinstance(air.get("version"), bool) else None}
    failure = air.get("sessionFailure")
    if isinstance(failure, dict):
        safe: dict[str, object] = {}
        for key in ("id", "category", "severity", "title", "details"):
            if key in failure:
                raw = failure[key]
                safe[key] = (_bounded_text(raw, _MAX_AIR_TEXT_BYTES, f"AIR {key}")
                             if isinstance(raw, str) else _safe_scalar(raw))
        if "revision" in failure:
            revision = failure["revision"]
            safe["revision"] = revision if isinstance(revision, int) and not isinstance(revision, bool) else None
        actions = failure.get("actions")
        if "actions" in failure and isinstance(actions, list):
            if len(actions) > _MAX_AIR_ACTIONS:
                raise AcpEvidenceOverflow("ACP evidence-overflow: AIR actions limit exceeded")
            safe["actions"] = [
                _bounded_text(action, _MAX_AIR_ACTION_BYTES, "AIR action") if isinstance(action, str) else None
                for action in actions
            ]
        elif "actions" in failure:
            safe["actions"] = None
        projected["sessionFailure"] = safe
    elif "sessionFailure" in air:
        # Preserve the decoder-visible malformed fact without retaining an
        # arbitrary provider object/string as evidence.
        projected["sessionFailure"] = None
    return projected


def _raw_air_failure(update: object) -> tuple[bool, object]:
    """Find an AIR sessionFailure without normalizing its wire value."""
    if not isinstance(update, dict) or not isinstance(update.get("_meta"), dict):
        return False, None
    try:
        air = update["_meta"]["jetbrains"]["air"]
    except (KeyError, TypeError):
        return False, None
    if not isinstance(air, dict) or "sessionFailure" not in air:
        return False, None
    return True, air["sessionFailure"]


def _bounded_text(value: str, limit: int, field: str) -> str:
    if len(value.encode("utf-8")) > limit:
        raise AcpEvidenceOverflow(f"ACP evidence-overflow: {field} exceeds local limit")
    return value


def _safe_scalar(value: object) -> object:
    """Retain malformed scalar type evidence, never arbitrary containers."""
    return value if value is None or isinstance(value, (bool, int, float)) else None


def _incident_belongs(value: object, prompt_id: int | None) -> bool:
    """Keep malformed raw AIR forensics; exclude only identifiable foreign ids."""
    return (prompt_id is not None and (not isinstance(value, dict)
            or not isinstance(value.get("id"), str)
            or value["id"].startswith(f"{prompt_id}:")))


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

    ACP config negotiation explicitly selects the pinned execution policy too.
    This launch boundary prevents global configuration from changing the
    requested web/network settings or the noninteractive request handler.
    Restricted invocations are rejected before this function is reached: the
    pinned artifact contains no source-backed mode with its required tuple.
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


def _validate_pinned_mode_for_policy(policy: CodexAcpPolicy) -> None:
    """Fail before dispatch when the selected ACP mode weakens a policy.

    ``agent`` is source-pinned as workspaceWrite with network disabled.  The
    pinned 1.11.0 source contains no restricted mode that can establish the
    required workspace-write, on-request, public-web/network tuple.  A config
    echo or caller-provided profile name cannot prove those semantics, so do
    not launch a child for restricted work until a pinned artifact supports it.
    """
    if policy.kind == "ordinary" and (_PINNED_MODE_SANDBOX != "workspaceWrite"
                                      or _PINNED_MODE_NETWORK_ACCESS):
        raise AcpClientRejected(
            "pinned ordinary ACP mode cannot preserve workspace-write policy"
        )
    if policy.kind == "restricted":
        raise AcpClientRejected(
            "restricted ACP profile is unsupported by the pinned artifact"
        )


def _mode_for(policy: CodexAcpPolicy) -> str:
    if policy.kind == "restricted":
        # Keep this guard local too, so future callers cannot accidentally use
        # a fabricated config value after the validation gate above.
        raise AcpClientRejected("restricted ACP profile is unsupported by the pinned artifact")
    return _ORDINARY_MODE


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
    if (not isinstance(response, dict) or response.get("protocolVersion") != 1
            or isinstance(response.get("protocolVersion"), bool)):
        return False
    try:
        air = response["_meta"]["jetbrains"]["air"]  # type: ignore[index]
    except (KeyError, TypeError):
        return False
    return (isinstance(air, dict) and air.get("version") == 1
            and not isinstance(air.get("version"), bool)
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
        "timeout_phases": list(timeout_phases),
        "stderr_available": bool(child.diagnostics),
    }


def run_codex_acp_turn(command: Sequence[str], *, launch: CodexAcpLaunchEvidence,
                       policy: CodexAcpPolicy, model: str, prompt: str,
                       log_path: Path, timeout_s: float = 60,
                       environment: Mapping[str, str] | None = None,
                       private_home: Path | None = None) -> CodexAcpTurn:
    """Run the pinned initialize/new/configure/prompt sequence once.

    ``launch`` is the verified codex-acp 1.11.0 artifact/dependency evidence
    bound to ``command``.  It is checked before a child exists; the live ACP
    v1/AIR handshake is then checked before session creation.  No installation,
    authentication, outcome-file handling, or role validation is performed
    here.  Exceptions are deliberate fail-closed evidence.
    """
    # Setup is fallible too.  Give every setup exception the same stable
    # envelope as a post-launch failure, including the caller's log reference.
    try:
        policy.validate()
        _validate_pinned_mode_for_policy(policy)
        if not command or not model or not prompt or timeout_s <= 0:
            raise AcpClientRejected("command, model, prompt, and positive timeout are required",
                                    log_path=str(log_path))
        launch.validate_for(command, _mode_for(policy))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # The explicit owner opt-in supplies a fresh private home containing
        # only a copied auth.json.  Config is written into that same home so
        # CODEX_HOME cannot point at a credential-free sibling directory.
        config_home = (private_home.resolve() if private_home is not None else
                       Path(tempfile.mkdtemp(prefix="nc-acp-codex-", dir=log_path.parent)))
        if private_home is not None and not (config_home / ".codex" / "auth.json").is_file():
            raise AcpClientRejected("private ACP home has no prepared auth.json", log_path=str(log_path))
        _write_pinned_config(policy, config_home)
    except BaseException as exc:
        exc.evidence = getattr(exc, "evidence", AcpTurnEvidence(
            None, None, None, None, False, str(log_path), "setup_failure"))
        # Rejections made below helpers predate their log_path argument.
        if isinstance(exc, AcpClientRejected) and not exc.evidence.log_path:
            exc.evidence = AcpTurnEvidence(None, None, None, None, False, str(log_path), "setup_rejected")
        raise
    # The prompt budget is separate from the prescribed bounded cancellation
    # and cleanup phases.  The supervisor still owns one outer deadline.
    deadline = time.monotonic() + timeout_s + 15
    evidence = _TurnEvidenceCollector()
    prompt_fact: AcpPromptFact | None = None
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
        evidence.notification(value)

    def deny_request(value: dict[str, object]) -> object:
        nonlocal server_request_rejection
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

    try:
        child = AcpSubprocess(command, cwd=policy.cwd, deadline=deadline, log_path=log_path,
                              env=_safe_environment(environment, config_home=config_home))
    except BaseException as exc:
        exc.evidence = AcpTurnEvidence(
            None, None, getattr(exc, "acp_launch_process", None),
            getattr(exc, "acp_launch_shutdown", None),
            bool(getattr(exc, "acp_launch_cleanup_uncertain", False)), str(log_path), "launcher_failure",
        )
        raise
    result: CodexAcpTurn | None = None
    timed_out = False
    # These are only *expired* bounded cleanup phases; successful protocol
    # operations and the original prompt deadline are not process facts.
    timeout_phases: list[str] = []
    failure: BaseException | None = None

    def partial_prompt_fact() -> AcpPromptFact | None:
        if prompt_fact is None:
            return None
        # Rebuild the small typed view from independent collector state.  The
        # raw list itself is deliberately shared: a notification delivered
        # immediately before EOF/overflow remains attached to this envelope.
        partial = dict(prompt_fact)
        partial["air_observations"] = evidence.air_observations
        if evidence.usage is not None:
            partial["usage"] = evidence.usage
        return partial  # type: ignore[return-value]

    try:
        stream = child.stream(notification_handler=observe_notification, request_handler=deny_request)

        init_params: dict[str, object] = {"protocolVersion": 1, "clientCapabilities": {
            "_meta": {"jetbrains": {"air": {"version": 1, "capabilities": ["sessionFailure"]}}},
        }}
        init_id = stream.send_request("initialize", init_params)
        init_response = stream.wait_for(init_id)
        raise_server_request_rejection()
        if not _air_advertised(init_response.get("result")):
            raise AcpClientRejected("server does not advertise pinned ACP/AIR profile")

        new_params: dict[str, object] = {"cwd": str(policy.cwd), "mcpServers": []}
        new_id = stream.send_request("session/new", new_params)
        new_response = stream.wait_for(new_id)
        raise_server_request_rejection()
        new_result = new_response.get("result")
        if not isinstance(new_result, dict) or not isinstance(new_result.get("sessionId"), str):
            raise AcpClientRejected("server did not allocate a valid fresh session")
        session_id = new_result["sessionId"]
        capabilities = new_result.get("sessionCapabilities")
        close_advertised = isinstance(capabilities, dict) and capabilities.get("close") is True
        options = _options(new_result)
        mode = _mode_for(policy)
        if not _offered(options, "model", model) or not _offered(options, "mode", mode):
            raise AcpClientRejected("requested model or pinned execution policy is unsupported")

        for config_id, value in (("model", model), ("mode", mode)):
            params: dict[str, object] = {"sessionId": session_id, "configId": config_id, "value": value}
            config_id_request = stream.send_request("session/set_config_option", params)
            response = stream.wait_for(config_id_request)
            raise_server_request_rejection()
            if not _selected(response.get("result"), config_id, value):
                raise AcpClientRejected("server rejected explicit ACP configuration")

        # A set-config response is a complete option snapshot, not an
        # acknowledgement for only the option named by its request.  In
        # particular, a mode update must not be allowed to silently reset or
        # omit the selected model before dispatching the prompt.
        if not (_selected(response.get("result"), "model", model)
                and _selected(response.get("result"), "mode", mode)):
            raise AcpClientRejected("server did not retain the pinned model and execution policy")

        prompt_params: dict[str, object] = {"sessionId": session_id,
                                            "prompt": [{"type": "text", "text": prompt}]}
        prompt_id = stream.send_request("session/prompt", prompt_params)
        evidence.prompt_request({"jsonrpc": "2.0", "id": prompt_id, "method": "session/prompt",
                                 "params": prompt_params}, prompt_id, session_id)
        # This exists before wait_for: local EOF/timeout after an update must
        # retain correlated prompt identity, raw AIR and latest usage.
        prompt_fact = {"request_id": str(prompt_id), "session_id": session_id,
                       "prompt_id": str(prompt_id), "prompt_response_valid": False,
                       "stop_reason": None, "air_observations": evidence.air_observations,
                       "session_failures": []}

        def project_prompt_response(response: dict[str, object]) -> AcpPromptResult:
            """Project a correlated terminal response into bounded evidence.

            This is also used for a response received while performing the
            mandatory cancellation after the prompt deadline.  Such a reply
            cannot turn the deadline failure into success, but it is still
            authoritative terminal AIR/usage evidence and must survive in the
            failure envelope.
            """
            nonlocal prompt_fact
            evidence.prompt_response(response)
            # The transport intentionally uses compact numeric JSON-RPC ids,
            # while AIR incident ownership is text-prefixed.  Give the pure
            # decoder its pinned textual correlation view without changing
            # the captured facts.
            decoder_wire = [dict(item, id=str(prompt_id)) if isinstance(item, dict)
                            and item.get("id") == prompt_id else item for item in evidence.records]
            # The latest update snapshot is independent evidence, not a
            # retained notification list.  Place it inside the prompt interval
            # for the decoder, immediately before its terminal response.
            if evidence.usage is not None:
                response_index = next((index for index, item in enumerate(decoder_wire)
                                       if isinstance(item, dict) and item.get("id") == str(prompt_id)
                                       and "method" not in item), len(decoder_wire))
                decoder_wire.insert(response_index, {"method": "session/update", "params": {
                    "sessionId": session_id, "update": {
                        "sessionUpdate": "usage_update", "usage": evidence.usage,
                    },
                }})
            decoded = decode_acp_prompt_result(
                decoder_wire, request_id=str(prompt_id), session_id=session_id,
            )
            prompt_fact = {
                "request_id": str(prompt_id), "session_id": session_id, "prompt_id": str(prompt_id),
                "prompt_response_valid": decoded.kind != "protocol_invalid",
                "stop_reason": decoded.stop_reason, "air_observations": evidence.air_observations,
                "session_failures": [
                    {"id": item.incident_id, "revision": item.revision, "category": item.category,
                     "severity": item.severity, "title": item.title, "actions": list(item.actions),
                     **({"details": item.details} if item.details is not None else {})}
                    for item in decoded.failures
                ],
            }
            retained_response = evidence.records[-1]
            if isinstance(retained_response, dict):
                if isinstance(retained_response.get("result"), dict):
                    prompt_fact["jsonrpc_result"] = retained_response["result"]
                if isinstance(retained_response.get("error"), dict):
                    prompt_fact["jsonrpc_error"] = retained_response["error"]
            if evidence.usage is not None:
                prompt_fact["usage"] = evidence.usage
            return decoded

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
                project_prompt_response(prompt_response)
            except AcpDeadlineExpired:
                timeout_phases.append("cancel_response")
            except (AcpProcessError, AcpStreamError) as cleanup_error:
                # EOF/write/protocol failure is evidence in its own right;
                # do not relabel it as an expired cancellation deadline.
                raise cleanup_error from exc
            if close_advertised:
                close_deadline = time.monotonic() + 5
                try:
                    stream.request(
                        "session/close", {"sessionId": session_id}, deadline=close_deadline,
                    )
                except AcpDeadlineExpired:
                    timeout_phases.append("session_close")
                except (AcpProcessError, AcpStreamError) as cleanup_error:
                    raise cleanup_error from exc
            error = AcpProcessTimeout("ACP prompt deadline expired after bounded cancellation")
            error.timeout_phases = tuple(timeout_phases)
            raise error from exc
        decoded = project_prompt_response(prompt_response)
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
        if failure is None and child.cleanup_uncertain:
            # A successful prompt is not enough when the owned helper tree
            # cannot be proved gone.  Return the same typed envelope as every
            # other local failure instead of hiding this in a shutdown string.
            failure = AcpCleanupUncertain("ACP cleanup containment is uncertain")
        if failure is not None:
            # Prompt expiry remains a timeout fact even if cancellation and
            # closing complete inside their separately bounded windows.
            process = _process_fact(child, timed_out or bool(timeout_phases), timeout_phases)
            failure.process = process
            failure.shutdown = child.shutdown_outcome
            # Preserve facts that arrived before every local error, including
            # an overflow raised while processing an AIR notification.
            prompt_fact = partial_prompt_fact()
            failure.evidence = AcpTurnEvidence(
                prompt_fact, evidence.usage, process, child.shutdown_outcome, child.cleanup_uncertain,
                str(log_path), "evidence_overflow" if evidence.overflow else (
                    "cleanup_uncertain" if child.cleanup_uncertain else "local_failure"
                ),
            )
            if child.shutdown_failure is not None or isinstance(failure, AcpCleanupUncertain):
                raise failure
            if isinstance(failure, AcpProcessTimeout):
                failure.timeout_phases = tuple(timeout_phases)
    assert result is not None
    timed_out = timed_out or bool(timeout_phases)
    return CodexAcpTurn(result.prompt, result.prompt_fact, _process_fact(child, timed_out, timeout_phases),
                         child.shutdown_outcome)
