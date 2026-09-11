"""Pure ACP fact interfaces for a future adapter; this module starts nothing."""

from __future__ import annotations

from typing import NotRequired, TypedDict


class AcpProcessFact(TypedDict):
    """Observed process state, independent from agent-authored role outcome."""

    exit_code: int | None
    signal: int | None
    timed_out: bool
    stderr_available: bool


class AcpPromptFact(TypedDict):
    """One correlated, schema-valid ACP prompt response and ordered AIR evidence."""

    request_id: str
    session_id: str
    prompt_id: str
    prompt_response_valid: bool
    stop_reason: str | None
    jsonrpc_error: NotRequired[dict[str, object]]
    session_failures: list[dict[str, object]]


class AcpCorrelation(TypedDict):
    """Facts retained beside, never instead of, the existing outcome.json."""

    process: AcpProcessFact
    prompt: AcpPromptFact
    outcome_path: str


def is_completion_candidate(prompt: AcpPromptFact) -> bool:
    """Return transport-only eligibility; role parsing makes the final decision."""
    return (
        prompt["prompt_response_valid"]
        and
        "jsonrpc_error" not in prompt
        and prompt.get("stop_reason") == "end_turn"
        and all(failure.get("severity", "error") != "error"
                for failure in prompt["session_failures"])
    )
