import os
import sys
from pathlib import Path

import pytest

from nc.adapters import (
    SessionResult,
    _run,
    adapter_ownership,
    assess_session,
    parse_tokens,
    sanitize_diagnostic,
)


def test_real_codex_usage(tmp_path, monkeypatch):
    # Verbatim tail of $NC_HOME/runs/critic-neocortex-T005-1_20260905T220048Z/session.log.
    sample = (Path(__file__).parent / "fixtures" / "codex-usage.log").read_text()
    assert parse_tokens(sample) == 26457

    class Proc:
        pid = 123

        def wait(self, timeout=None):
            return 0

    def popen(cmd, **kwargs):
        kwargs["stdout"].write(sample)
        return Proc()

    monkeypatch.setattr("nc.adapters.subprocess.Popen", popen)
    assert _run(["codex", "exec"], tmp_path, tmp_path / "session.log", 10).tokens == 26457


def test_registration_failure_kills_and_reaps_untracked_adapter(tmp_path):
    """A DB failure after Popen must not leave a live, unrecorded session."""
    pid = None

    def reject(new_pid):
        nonlocal pid
        pid = new_pid
        raise RuntimeError("simulated ownership recording failure")

    with adapter_ownership(reject), pytest.raises(RuntimeError, match="recording"):
        _run([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path,
             tmp_path / "session.log", 30)
    assert pid is not None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


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


@pytest.mark.parametrize(("adapter", "line", "expected"), [
    ("codex", "Codex API Error: rate_limit_exceeded", "throttled"),
    ("claude", "Claude API Error: insufficient_quota", "billing_credits"),
    ("codex", "Codex API Error: permission_denied", "permission"),
    ("claude", "Claude API Error: authentication_error", "authentication"),
    ("codex", "Codex API Error: invalid_model", "invalid_request"),
    ("claude", "Claude API Error: temporarily_unavailable", "transient"),
    ("codex", "Codex API Error: overloaded", "overloaded"),
    ("claude", "Claude API Error: usage_limit", "subscription_limit"),
])
def test_adapter_specific_final_terminal_fixture(tmp_path, adapter, line, expected):
    """Synthetic fixtures; these envelopes are deliberately not live evidence."""
    path = tmp_path / "session.log"
    path.write_text("ordinary output\n" + line + "\n")
    assessment = assess_session(SessionResult(1, path, None, False), adapter)
    assert assessment.category == expected
    assert assessment.failed


@pytest.mark.parametrize("adapter", ["codex", "claude"])
def test_fallback_never_searches_prompt_or_truncated_streams(tmp_path, adapter):
    path = tmp_path / "session.log"
    path.write_text('prompt says "rate_limit_exceeded"\n' * 1000 +
                    'agent quoted "Codex API Error: permission_denied"\n')
    assessment = assess_session(SessionResult(1, path, None, False), adapter)
    assert assessment.category == "unknown"


@pytest.mark.parametrize(("exit_code", "timed_out", "category", "expected"), [
    (1, False, None, "unknown"), (0, True, None, "host_timeout"),
    (0, False, "overloaded", "overloaded"), (0, False, None, "none"),
])
def test_host_assessment_precedence(tmp_path, exit_code, timed_out, category, expected):
    path = tmp_path / "session.log"
    path.write_text("agent recovered from rate_limit_exceeded\n")
    assessment = assess_session(SessionResult(exit_code, path, None, timed_out, category), "codex")
    assert assessment.category == expected


@pytest.mark.parametrize("adapter", ["codex", "claude"])
def test_successful_retry_output_is_not_terminal_evidence(tmp_path, adapter):
    """Synthetic recovered stream: only terminal structured evidence can fail a zero exit."""
    path = tmp_path / "session.log"
    path.write_text("Codex API Error: rate_limit_exceeded\nretry succeeded\n")
    assessment = assess_session(SessionResult(0, path, None, False), adapter)
    assert assessment.status == "SUCCESS"


@pytest.mark.parametrize("category", [
    "subscription_limit", "throttled", "overloaded", "transient",
    "authentication", "permission", "invalid_request", "billing_credits",
    "local_error", "protocol", "not-a-provider-category",
])
def test_structured_terminal_category_beats_zero_exit(tmp_path, category):
    """Synthetic structured terminal evidence, including unsupported/unknown evidence."""
    path = tmp_path / "session.log"
    assessment = assess_session(
        SessionResult(0, path, None, False, category, "terminal failure"), "codex",
    )
    assert assessment.failed
    assert assessment.category == (category if category != "not-a-provider-category" else "unknown")


def test_diagnostic_redacts_credentials_and_is_bounded():
    diagnostic = sanitize_diagnostic(
        "Bearer abcdefghijklmnop API_KEY=super-secret sk-abcdefghijklmnop password: hunter2")
    assert "abcdefghijklmnop" not in diagnostic
    assert "super-secret" not in diagnostic
    assert "hunter2" not in diagnostic
    assert "[REDACTED]" in diagnostic


@pytest.mark.parametrize("binary", ["/usr/bin/claude", None])
@pytest.mark.parametrize("model", ["sonnet", ""])
def test_claude_command(tmp_path, monkeypatch, binary, model):
    from unittest.mock import Mock

    from nc.adapters import ClaudeAdapter

    monkeypatch.setattr("nc.adapters.shutil.which", lambda name: binary)
    monkeypatch.setattr("nc.adapters.Path.home", lambda: tmp_path)
    run = Mock()
    monkeypatch.setattr("nc.adapters._run", run)
    prompt = "Review this diff.\nReport findings."
    log = tmp_path / "session.log"
    result = ClaudeAdapter().run(prompt, tmp_path, model, log, 30)
    cmd = [binary or str(tmp_path / ".local/bin/claude"),
           "-p", prompt, "--permission-mode", "bypassPermissions"]
    if model:
        cmd += ["--model", model]
    run.assert_called_once_with(cmd, tmp_path, log, 30)
    assert result is run.return_value


@pytest.mark.parametrize("name", ["codex", "claude"])
def test_planner_adapter_restricts_writes(tmp_path, monkeypatch, name):
    from unittest.mock import Mock

    from nc.adapters import get_adapter

    run = Mock()
    monkeypatch.setattr("nc.adapters._run", run)
    get_adapter(name).run_planner("Plan", tmp_path, "model", tmp_path / "session.log", 30)
    cmd = run.call_args.args[0]
    if name == "codex":
        assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    else:
        assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
        assert cmd[cmd.index("--tools") + 1] == "Read,Glob,Grep,Write"
        assert f"Write(//{tmp_path.as_posix().lstrip('/')}/outcome.json)" in cmd
    assert run.call_args.args[1] == tmp_path
