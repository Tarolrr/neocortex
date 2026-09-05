from pathlib import Path
from types import SimpleNamespace

import pytest

from nc.adapters import _run, parse_tokens


def test_real_codex_usage(tmp_path, monkeypatch):
    # Verbatim tail of $NC_HOME/runs/critic-neocortex-T005-1_20260905T220048Z/session.log.
    sample = (Path(__file__).parent / "fixtures" / "codex-usage.log").read_text()
    assert parse_tokens(sample) == 26457

    def run(cmd, **kwargs):
        kwargs["stdout"].write(sample)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("nc.adapters.subprocess.run", run)
    assert _run(["codex", "exec"], tmp_path, tmp_path / "session.log", 10).tokens == 26457


@pytest.mark.parametrize(("text", "expected"), [
    ("", None),
    ("session interrupted", None),
    ("We discussed tokens used: 123", None),
    ("tokens used\nunknown\n", None),
    ("tokens used\n1,23\n", None),
    ("tokens used\n0\n", 0),
    ("tokens used\r\n12,345\r\n", 12345),
    ("tokens used\n123\ntokens used\n456\nfinal answer", 456),
])
def test_parse_tokens(text, expected):
    assert parse_tokens(text) == expected
