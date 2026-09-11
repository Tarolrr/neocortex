"""Pure, fail-closed decoding of one ACP prompt completion.

This is deliberately not an adapter.  It consumes already received JSON-RPC
objects and produces evidence for a future host; it neither reads transcript
text nor makes policy decisions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

ResultKind = Literal["success", "failed", "cancelled", "protocol_invalid"]
_KNOWN_CATEGORIES = {"connection", "access", "limit", "request", "service", "unknown"}
_KNOWN_ACTIONS = {"retry", "new_session", "login"}
_MAX_DIAGNOSTIC = 4000


@dataclass(frozen=True)
class AcpUsage:
    """Reported terminal usage.  ``total_tokens`` is never a sum of reports."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class AirFailureEvidence:
    """A structured AIR failure, with unknown extension values retained safely."""

    incident_id: str
    revision: int
    category: str
    actions: tuple[str, ...]
    severity: Literal["warning", "error"]
    diagnostic: str
    unknown_category: str | None = None
    unknown_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class AcpPromptResult:
    kind: ResultKind
    stop_reason: str | None
    diagnostic: str | None
    failures: tuple[AirFailureEvidence, ...]
    usage: AcpUsage | None


def _at(value: object, *keys: str) -> object | None:
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _has(value: object, *keys: str) -> bool:
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return False
        value = value[key]
    return True


def _diagnostic(value: object) -> str:
    if not isinstance(value, str):
        return ""
    # Keep diagnostics useful in a database/UI without accepting terminal control text.
    return " ".join("".join(c for c in value if c >= " " or c in "\n\t").split())[:_MAX_DIAGNOSTIC]


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _usage(value: object) -> AcpUsage | None:
    """Decode one complete structured usage report, never notification totals."""
    if not isinstance(value, dict):
        return None
    get = lambda camel, snake: value.get(camel, value.get(snake))
    input_tokens = _integer(get("inputTokens", "input_tokens"))
    output_tokens = _integer(get("outputTokens", "output_tokens"))
    cached = _integer(get("cachedInputTokens", "cached_input_tokens"))
    total = _integer(get("totalTokens", "total_tokens"))
    if total is None:
        # Cached input is normally a subset of input, so it is not added.
        if input_tokens is None or output_tokens is None:
            return None
        total = input_tokens + output_tokens
    return AcpUsage(input_tokens, output_tokens, cached, total)


def _failure(value: object) -> tuple[AirFailureEvidence | None, str | None]:
    """Return evidence or a protocol defect; omitted severity is pinned to error."""
    if not isinstance(value, dict):
        return None, "AIR sessionFailure is not an object"
    incident_id, revision = value.get("id"), value.get("revision")
    title, details, category, actions = (value.get(key) for key in
                                         ("title", "details", "category", "actions"))
    if not isinstance(incident_id, str) or not incident_id:
        return None, "AIR sessionFailure id is invalid"
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        return None, "AIR sessionFailure revision is invalid"
    if not isinstance(title, str) or (details is not None and not isinstance(details, str)):
        return None, "AIR sessionFailure diagnostic is invalid"
    if not isinstance(category, str) or not isinstance(actions, list) or not all(
        isinstance(action, str) for action in actions
    ):
        return None, "AIR sessionFailure category or actions is invalid"
    severity = value.get("severity", "error")
    if severity not in {"warning", "error"}:
        return None, "AIR sessionFailure severity is invalid"
    unknown_category = category if category not in _KNOWN_CATEGORIES else None
    unknown_actions = tuple(action for action in actions if action not in _KNOWN_ACTIONS)
    diagnostic = _diagnostic(f"{title}: {details}" if details else title)
    return AirFailureEvidence(incident_id, revision, category if unknown_category is None else "unknown",
                              tuple(actions), severity, diagnostic, unknown_category,
                              unknown_actions), None


def decode_acp_prompt_result(
    wire: Sequence[object], *, request_id: str, session_id: str, air_version: int = 1
) -> AcpPromptResult:
    """Decode a single active prompt from its JSON-RPC wire records.

    Only the response whose id is ``request_id`` and prior updates for
    ``session_id`` whose AIR incident id starts with ``request_id + ':'`` are
    considered.  Duplicate equal revisions are ignored; a lower revision is
    stale, while divergent duplicate revisions are protocol-invalid.
    """
    if air_version != 1:
        return AcpPromptResult("protocol_invalid", None, "unsupported AIR extension version", (), None)
    responses = [item for item in wire if isinstance(item, dict) and item.get("id") == request_id
                 and "method" not in item]
    if len(responses) != 1:
        return AcpPromptResult("protocol_invalid", None, "conflicting prompt response identity", (), None)
    response = responses[0]
    if ("result" in response) == ("error" in response):
        return AcpPromptResult("protocol_invalid", None, "prompt response must have exactly one result or error", (), None)
    if "error" in response:
        error = response["error"]
        message = _diagnostic(error.get("message") if isinstance(error, dict) else "")
        return AcpPromptResult("failed", None, message or "JSON-RPC prompt error", (), None)
    result = response["result"]
    if not isinstance(result, dict):
        return AcpPromptResult("protocol_invalid", None, "prompt result is not an object", (), None)
    stop_reason = result.get("stopReason")
    if not isinstance(stop_reason, str):
        return AcpPromptResult("protocol_invalid", None, "prompt stopReason is invalid", (), _usage(result.get("usage")))
    response_index = next(index for index, item in enumerate(wire) if item is response)
    raw_failures: list[object] = []
    for item in wire[:response_index]:
        if not isinstance(item, dict) or item.get("method") != "session/update":
            continue
        params = item.get("params")
        if isinstance(params, dict) and params.get("sessionId") == session_id:
            update = params.get("update")
            if _has(update, "_meta", "jetbrains", "air", "sessionFailure"):
                failure = _at(update, "_meta", "jetbrains", "air", "sessionFailure")
                # A well-formed incident for another prompt is not evidence for this one.
                if isinstance(failure, dict) and isinstance(failure.get("id"), str):
                    if failure["id"].startswith(request_id + ":"):
                        raw_failures.append(failure)
                else:
                    return AcpPromptResult("protocol_invalid", stop_reason,
                                           "malformed correlated AIR update", (),
                                           _usage(result.get("usage")))
    terminal = _at(result, "_meta", "jetbrains", "air", "sessionFailure")
    if _has(result, "_meta", "jetbrains", "air", "sessionFailure"):
        if not isinstance(terminal, dict) or not isinstance(terminal.get("id"), str) or not terminal["id"].startswith(request_id + ":"):
            return AcpPromptResult("protocol_invalid", stop_reason,
                                   "terminal AIR failure identity conflicts with prompt", (),
                                   _usage(result.get("usage")))
        raw_failures.append(terminal)
    failures: dict[str, AirFailureEvidence] = {}
    for raw in raw_failures:
        evidence, problem = _failure(raw)
        if problem:
            return AcpPromptResult("protocol_invalid", stop_reason, problem, tuple(failures.values()), _usage(result.get("usage")))
        assert evidence is not None
        old = failures.get(evidence.incident_id)
        if old is not None and evidence.revision == old.revision:
            if evidence != old:
                return AcpPromptResult("protocol_invalid", stop_reason, "conflicting AIR failure revision", tuple(failures.values()), _usage(result.get("usage")))
            continue
        if old is None or evidence.revision > old.revision:
            failures[evidence.incident_id] = evidence
    evidence = tuple(failures.values())
    usage = _usage(result.get("usage"))
    if stop_reason == "cancelled":
        return AcpPromptResult("cancelled", stop_reason, None, evidence, usage)
    if stop_reason != "end_turn":
        return AcpPromptResult("failed", stop_reason, None, evidence, usage)
    errors = [item for item in evidence if item.severity == "error"]
    if errors:
        return AcpPromptResult("failed", stop_reason, errors[-1].diagnostic or None, evidence, usage)
    return AcpPromptResult("success", stop_reason, None, evidence, usage)


# A short public name is convenient for a future adapter without making it one.
decode_prompt_result = decode_acp_prompt_result
