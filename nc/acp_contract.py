"""Pure ACP fact interfaces for a future adapter; this module starts nothing."""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

AirFailureCategory = Literal[
    "connection", "access", "limit", "request", "service", "unknown",
]
AirFailureSeverity = Literal["warning", "error"]
AirFailureAction = Literal["retry", "new_session", "login"]
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
    # ``timed_out`` is true exactly when one or more ordered bounds expired.
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
    """Validate the complete pinned AIR record; malformed metadata is not benign."""
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
    if not all(isinstance(action, str) and action in {"retry", "new_session", "login"}
               for action in value["actions"]):
        return False
    return "details" not in value or isinstance(value["details"], str)


def is_completion_candidate(prompt: AcpPromptFact) -> bool:
    """Return transport-only eligibility; role parsing makes the final decision."""
    observations = prompt.get("air_observations")
    failures = prompt.get("session_failures")
    if not isinstance(observations, list) or not isinstance(failures, list):
        return False
    if not all(is_air_session_failure(observation) for observation in observations):
        return False
    # The adapter must not omit an observed error from its validated view.
    if failures != observations or not all(is_air_session_failure(failure) for failure in failures):
        return False
    return (
        prompt.get("prompt_response_valid") is True
        and isinstance(prompt.get("jsonrpc_result"), dict)
        and "jsonrpc_error" not in prompt
        and prompt.get("stop_reason") == "end_turn"
        and all(failure["severity"] != "error" for failure in failures)
    )
