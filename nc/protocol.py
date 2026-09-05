"""Turn outcomes and inter-agent messages.

Agents never talk to each other in free text. A turn ends by writing a single
JSON object to the outcome file the scheduler passed in; anything else is a
protocol violation and is recorded as such.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DONE = "DONE"
ASK = "ASK"
YIELD = "YIELD"
FAIL = "FAIL"
NO_OUTCOME = "NO_OUTCOME"

VALID_OUTCOMES = {DONE, ASK, YIELD, FAIL}

# Message kinds exchanged through the state's message table.
QUESTION = "question"
ANSWER = "answer"
REVIEW_REQUEST = "review_request"
REVIEW_VERDICT = "review_verdict"
INCIDENT = "incident"


@dataclass
class Outcome:
    kind: str
    summary: str = ""
    memo: str = ""
    # ASK
    to: str = "owner"
    question: str = ""
    # DONE (critic)
    verdict: str = ""
    findings: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.kind in VALID_OUTCOMES


def read_outcome(path: Path) -> Outcome:
    """Read and validate the outcome file written by an agent turn."""
    if not path.exists():
        return Outcome(kind=NO_OUTCOME, summary="agent wrote no outcome file")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return Outcome(kind=NO_OUTCOME, summary=f"outcome file is not valid JSON: {exc}")
    if not isinstance(data, dict):
        return Outcome(kind=NO_OUTCOME, summary="outcome file is not a JSON object")

    kind = str(data.get("outcome", "")).upper()
    if kind not in VALID_OUTCOMES:
        return Outcome(kind=NO_OUTCOME, summary=f"unknown outcome {kind!r}", raw=data)

    findings = data.get("findings") or []
    if not isinstance(findings, list):
        findings = [str(findings)]
    return Outcome(
        kind=kind,
        summary=str(data.get("summary", ""))[:2000],
        memo=str(data.get("memo", ""))[:2000],
        to=str(data.get("to", "owner")),
        question=str(data.get("question", ""))[:2000],
        verdict=str(data.get("verdict", "")).lower(),
        findings=[str(f)[:500] for f in findings][:20],
        raw=data,
    )


OUTCOME_CONTRACT = """\
## How this turn must end

Your turn has a hard time limit. Before you run out, write a single JSON object
to {outcome_path} — that file is the ONLY thing the scheduler reads from you.
Nothing you print is parsed. If the file is missing, the turn counts as a failure.

Exactly one of:

  {{"outcome": "DONE",  "summary": "<what you produced>", "memo": "<what the next turn must know>"}}
  {{"outcome": "ASK",   "to": "owner", "question": "<one specific blocking question>", "memo": "..."}}
  {{"outcome": "YIELD", "summary": "<progress so far>", "memo": "<exact next step>"}}
  {{"outcome": "FAIL",  "summary": "<why this cannot be done>"}}

Rules:
- ASK only for a genuine blocker you cannot resolve yourself. You will be
  suspended until an answer arrives; state loss is your problem, so put
  everything you need to resume into `memo`.
- YIELD when the work is sound but unfinished. `memo` is your entire memory of
  this turn: the next turn starts from the brief plus `memo`, not from your
  transcript.
- Never mark your own work accepted. DONE means "ready for review".
"""
