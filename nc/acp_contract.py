"""Pure ACP fact interfaces for a future adapter; this module starts nothing."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal, NotRequired, TypedDict

AirFailureCategory = Literal[
    "connection", "access", "limit", "request", "service", "unknown",
]
AirFailureSeverity = Literal["warning", "error"]
# AIR permits extension action strings.  The decoder preserves them on its
# effective record, so this boundary must not re-interpret a warning extension
# as a malformed failure after decoding.
AirFailureAction = str
TimeoutPhase = Literal["cancel_response", "session_close", "term_grace", "kill_grace"]


class AirSessionFailure(TypedDict):
    """The complete AIR v1 sessionFailure record emitted by pinned codex-acp."""

    id: str
    revision: int
    category: AirFailureCategory
    severity: AirFailureSeverity
    title: str
    actions: list[AirFailureAction]
    details: NotRequired[str]


class AcpProcessFact(TypedDict):
    """Observed process state, independent from agent-authored role outcome."""

    pid: int
    exit_code: int | None
    signal: int | None
    # ``timed_out`` is true when the prompt budget or a cleanup bound expired.
    # ``timeout_phases`` records only cleanup bounds, so it may be empty.
    timed_out: bool
    timeout_phases: list[TimeoutPhase]
    stderr_available: bool


class AcpPromptFact(TypedDict):
    """One correlated ACP prompt response and lossless ordered AIR evidence."""

    request_id: str
    session_id: str
    prompt_id: str
    prompt_response_valid: bool
    stop_reason: str | None
    # The last complete structured usage snapshot observed for this prompt.
    # It is independent of a terminal response, so local transport failure
    # cannot erase provider accounting already received.
    usage: NotRequired[dict[str, int]]
    jsonrpc_result: NotRequired[dict[str, object]]
    jsonrpc_error: NotRequired[dict[str, object]]
    # Raw values from every discovered AIR sessionFailure in wire order.
    # ``session_failures`` is the separately validated, complete typed view.
    air_observations: list[object]
    session_failures: list[AirSessionFailure]


class AcpCorrelation(TypedDict):
    """Facts retained beside, never instead of, the existing outcome.json."""

    process: AcpProcessFact
    prompt: AcpPromptFact
    outcome_path: str


def is_air_session_failure(value: object) -> bool:
    """Validate a decoder-normalized effective AIR record.

    Categories unknown to AIR v1 are normalized to ``unknown`` by the
    decoder; action extensions remain strings.  Raw wire observations are not
    accepted here and never decide completion independently.
    """
    if not isinstance(value, dict):
        return False
    required = {"id", "revision", "category", "severity", "title", "actions"}
    if not required <= value.keys():
        return False
    if not isinstance(value["id"], str) or not value["id"]:
        return False
    if (not isinstance(value["revision"], int) or isinstance(value["revision"], bool)
            or value["revision"] < 1):
        return False
    if (not isinstance(value["category"], str)
            or value["category"] not in {"connection", "access", "limit", "request", "service", "unknown"}):
        return False
    if not isinstance(value["severity"], str) or value["severity"] not in {"warning", "error"}:
        return False
    if not isinstance(value["title"], str):
        return False
    if not isinstance(value["actions"], list):
        return False
    if not all(isinstance(action, str) for action in value["actions"]):
        return False
    return "details" not in value or isinstance(value["details"], str)


def is_effective_completion(stop_reason: str | None, severities: Iterable[str]) -> bool:
    """The single AIR completion rule for decoder-effective records.

    Callers must validate and revision-reconcile records before invoking this;
    it intentionally does not inspect raw wire metadata.
    """
    # AIR has no settled scheduler policy yet.  A decoded sessionFailure is
    # evidence of a provider condition, not evidence that the turn completed
    # successfully.  Keep this deliberately conservative until the mapping
    # layer owns category/action policy.  The pure decoder separately retains
    # canonical end_turn facts for recoverable warnings.
    return stop_reason == "end_turn" and not tuple(severities)


def is_completion_candidate(prompt: AcpPromptFact) -> bool:
    """Return transport-only eligibility; role parsing makes the final decision.

    ``air_observations`` is a lossless raw audit trail.  It is deliberately
    not a second completion validator: duplicate and stale revisions have
    already been reconciled into ``session_failures`` by the decoder.
    """
    observations = prompt.get("air_observations")
    failures = prompt.get("session_failures")
    if not isinstance(observations, list) or not isinstance(failures, list):
        return False
    # These are captured when the prompt is dispatched, rather than inferred
    # from a response-looking object.  The bridge uses the prompt JSON-RPC id
    # for both names, so a mismatch is evidence from a different request.
    request_id = prompt.get("request_id")
    prompt_id = prompt.get("prompt_id")
    session_id = prompt.get("session_id")
    if (not isinstance(request_id, str) or not request_id
            or not isinstance(prompt_id, str) or not prompt_id
            or not isinstance(session_id, str) or not session_id
            or request_id != prompt_id):
        return False
    result = prompt.get("jsonrpc_result")
    return (
        prompt.get("prompt_response_valid") is True
        # Require the canonical terminal value itself, not merely a separately
        # copied stop_reason field.
        and isinstance(result, dict)
        and result.get("stopReason") == "end_turn"
        and "jsonrpc_error" not in prompt
        # ``failures`` is the decoder's revision-reconciled effective view.
        # Do not revalidate raw observations here: stale records and unknown
        # extensions are intentionally decoder semantics, not a second route
        # to completion or rejection.
        and all(is_air_session_failure(failure) for failure in failures)
        and is_effective_completion(prompt.get("stop_reason"),
                                    (failure["severity"] for failure in failures))
    )
