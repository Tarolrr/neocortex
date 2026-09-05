import json

import pytest

from nc import protocol


def write(tmp_path, text):
    path = tmp_path / "outcome.json"
    path.write_text(text)
    return path


def test_missing_file_is_a_protocol_failure(tmp_path):
    outcome = protocol.read_outcome(tmp_path / "nope.json")
    assert outcome.kind == protocol.NO_OUTCOME
    assert not outcome.ok


@pytest.mark.parametrize("text", ["not json", "[1, 2]", '{"outcome": "MAYBE"}', "{}"])
def test_malformed_outcomes_are_rejected(tmp_path, text):
    assert protocol.read_outcome(write(tmp_path, text)).kind == protocol.NO_OUTCOME


def test_valid_outcomes(tmp_path):
    path = write(tmp_path, json.dumps({"outcome": "ask", "to": "owner",
                                       "question": "which port?", "memo": "m"}))
    outcome = protocol.read_outcome(path)
    assert outcome.ok
    assert (outcome.kind, outcome.question, outcome.memo) == (protocol.ASK, "which port?", "m")


def test_critic_verdict_is_normalised(tmp_path):
    path = write(tmp_path, json.dumps({"outcome": "DONE", "verdict": "Rework",
                                       "findings": ["criterion 2 unmet"]}))
    outcome = protocol.read_outcome(path)
    assert (outcome.kind, outcome.verdict, outcome.findings) == (
        protocol.DONE, "rework", ["criterion 2 unmet"])


def test_long_fields_are_truncated(tmp_path):
    path = write(tmp_path, json.dumps({"outcome": "DONE", "summary": "x" * 9000,
                                       "findings": ["y" * 9000] * 50}))
    outcome = protocol.read_outcome(path)
    assert len(outcome.summary) == 2000
    assert len(outcome.findings) == 20
    assert all(len(f) == 500 for f in outcome.findings)


def test_contract_renders_with_path():
    text = protocol.OUTCOME_CONTRACT.format(outcome_path="/tmp/o.json")
    assert "/tmp/o.json" in text
    assert '{"outcome": "DONE"' in text
