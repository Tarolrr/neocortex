"""Pure, fail-closed decoding of one ACP prompt completion.

This is deliberately not an adapter.  It consumes already received JSON-RPC
objects and produces evidence for a future host; it neither reads transcript
text nor makes policy decisions.
"""

from __future__ import annotations

import re
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
    text = "".join(c for c in value if c >= " " or c in "\n\t")
    # AIR diagnostics are untrusted provider data.  Keep this local rather
    # than importing the adapter so the decoder remains an isolated pure fact
    # parser; the patterns intentionally match the host's diagnostic boundary.
    text = re.sub(r"(?i)\b(bearer\s+)[^\s,;]+", r"\1[REDACTED]", text)
    text = re.sub(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", text)
    text = re.sub(
        r"(?i)\b([\"']?(?:(?:[a-z][a-z0-9]*_)+)?(?:api[_ -]?key|access[_ -]?token|"
        r"authorization|password|secret|token|client[_ -]?secret|"
        r"secret[_ -]?access[_ -]?key)[\"']?)\s*([=:])\s*"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)",
        r"\1\2[REDACTED]", text,
    )
    return " ".join(text.split())[:_MAX_DIAGNOSTIC]


def _air_version_problem(value: object) -> str | None:
    """Validate an observed AIR envelope, even if it has no failure."""
    if not isinstance(value, dict):
        return "AIR extension envelope is not an object"
    version = value.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        return "AIR extension version is invalid"
    if version != 1:
        return "unsupported AIR extension version"
    return None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _usage_integer(value: dict[object, object], camel: str, snake: str) -> tuple[bool, int | None]:
    """Read one aliased usage field without letting one spelling hide another."""
    present = [key for key in (camel, snake) if key in value]
    if not present:
        return False, None
    raw = [value[key] for key in present]
    if len(raw) == 2 and raw[0] != raw[1]:
        return True, None
    return True, _integer(raw[0])


def _usage(value: object) -> AcpUsage | None:
    """Decode one complete structured usage snapshot."""
    if not isinstance(value, dict):
        return None
    input_present, input_tokens = _usage_integer(value, "inputTokens", "input_tokens")
    output_present, output_tokens = _usage_integer(value, "outputTokens", "output_tokens")
    cached_present, cached = _usage_integer(value, "cachedInputTokens", "cached_input_tokens")
    total_present, total = _usage_integer(value, "totalTokens", "total_tokens")
    # Input and output are the complete pinned report.  Optional cache and
    # total fields must also be valid when provided: a malformed partial report
    # is unknown, not a report with conveniently omitted pieces.
    if (not input_present or input_tokens is None or not output_present or output_tokens is None
            or (cached_present and cached is None) or (total_present and total is None)):
        return None
    if not total_present:
        # Cached input is normally a subset of input, so it is not added.
        total = input_tokens + output_tokens
    return AcpUsage(input_tokens, output_tokens, cached, total)


def _notification_usage(wire: Sequence[object], response_index: int, session_id: str) -> AcpUsage | None:
    """Return the final direct usage snapshot from this prompt's updates.

    ``session/update`` usage reports are snapshots, not increments.  Reading
    only the direct ``update.usage`` field deliberately excludes message and
    tool payloads, which may contain arbitrary provider-shaped JSON.
    """
    reported = False
    usage: AcpUsage | None = None
    for item in wire[:response_index]:
        if not isinstance(item, dict) or item.get("method") != "session/update":
            continue
        params = item.get("params")
        if not isinstance(params, dict) or params.get("sessionId") != session_id:
            continue
        update = params.get("update")
        if isinstance(update, dict) and "usage" in update:
            reported = True
            usage = _usage(update["usage"])
    return usage if reported else None


def _jsonrpc_error(value: object) -> str | None:
    """Return a sanitized JSON-RPC error message only for a valid error object."""
    if not isinstance(value, dict):
        return None
    code, message = value.get("code"), value.get("message")
    if not isinstance(code, int) or isinstance(code, bool) or not isinstance(message, str):
        return None
    return _diagnostic(message)


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
    responses = [item for item in wire if isinstance(item, dict) and item.get("id") == request_id
                 and "method" not in item]
    if len(responses) != 1:
        return AcpPromptResult("protocol_invalid", None, "conflicting prompt response identity", (), None)
    response = responses[0]
    response_index = next(index for index, item in enumerate(wire) if item is response)
    notified_usage = _notification_usage(wire, response_index, session_id)
    if ("result" in response) == ("error" in response):
        return AcpPromptResult("protocol_invalid", None, "prompt response must have exactly one result or error", (), None)
    if "error" in response:
        message = _jsonrpc_error(response["error"])
        if message is None:
            return AcpPromptResult("protocol_invalid", None, "prompt error is invalid", (), notified_usage)
        return AcpPromptResult("failed", None, message or "JSON-RPC prompt error", (), notified_usage)
    result = response["result"]
    if not isinstance(result, dict):
        return AcpPromptResult("protocol_invalid", None, "prompt result is not an object", (), None)
    stop_reason = result.get("stopReason")
    # The prompt result is the terminal snapshot when it reports usage.  It
    # replaces, rather than adds to, any earlier update snapshot.
    usage = _usage(result["usage"]) if "usage" in result else notified_usage
    if not isinstance(stop_reason, str):
        return AcpPromptResult("protocol_invalid", None, "prompt stopReason is invalid", (), usage)
    # A caller's negotiated profile is still an input fact, but do not discard
    # independently received terminal data when it is unsupported.
    if air_version != 1:
        return AcpPromptResult("protocol_invalid", stop_reason, "unsupported AIR extension version", (), usage)
    raw_failures: list[object] = []
    for item in wire[:response_index]:
        if not isinstance(item, dict) or item.get("method") != "session/update":
            continue
        params = item.get("params")
        if isinstance(params, dict) and params.get("sessionId") == session_id:
            update = params.get("update")
            if _has(update, "_meta", "jetbrains", "air"):
                problem = _air_version_problem(_at(update, "_meta", "jetbrains", "air"))
                if problem:
                    return AcpPromptResult("protocol_invalid", stop_reason, problem, (), usage)
            if _has(update, "_meta", "jetbrains", "air", "sessionFailure"):
                failure = _at(update, "_meta", "jetbrains", "air", "sessionFailure")
                # A well-formed incident for another prompt is not evidence for this one.
                if isinstance(failure, dict) and isinstance(failure.get("id"), str):
                    if failure["id"].startswith(request_id + ":"):
                        raw_failures.append(failure)
                else:
                    return AcpPromptResult("protocol_invalid", stop_reason,
                                           "malformed correlated AIR update", (), usage)
    if _has(result, "_meta", "jetbrains", "air"):
        problem = _air_version_problem(_at(result, "_meta", "jetbrains", "air"))
        if problem:
            return AcpPromptResult("protocol_invalid", stop_reason, problem, (), usage)
    terminal = _at(result, "_meta", "jetbrains", "air", "sessionFailure")
    if _has(result, "_meta", "jetbrains", "air", "sessionFailure"):
        if not isinstance(terminal, dict) or not isinstance(terminal.get("id"), str) or not terminal["id"].startswith(request_id + ":"):
            return AcpPromptResult("protocol_invalid", stop_reason,
                                   "terminal AIR failure identity conflicts with prompt", (),
                                   usage)
        raw_failures.append(terminal)
    failures: dict[str, AirFailureEvidence] = {}
    for raw in raw_failures:
        evidence, problem = _failure(raw)
        if problem:
            return AcpPromptResult("protocol_invalid", stop_reason, problem, tuple(failures.values()), usage)
        assert evidence is not None
        old = failures.get(evidence.incident_id)
        if old is not None and evidence.revision == old.revision:
            if evidence != old:
                return AcpPromptResult("protocol_invalid", stop_reason, "conflicting AIR failure revision", tuple(failures.values()), usage)
            continue
        if old is None or evidence.revision > old.revision:
            failures[evidence.incident_id] = evidence
    evidence = tuple(failures.values())
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
