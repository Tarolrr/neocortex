import os
import sys
from pathlib import Path

import pytest

from nc.adapters import (
    SessionResult,
    _category_from_terminal,
    _run,
    adapter_ownership,
    assess_session,
    parse_stream_tokens,
    parse_tokens,
    sanitize_diagnostic,
)


@pytest.mark.parametrize(("diagnostic", "expected"), [
    ("usage limit reached", "unknown"),
    ("plan limit reached", "unknown"),
    ("organization usage limit resets at midnight", "unknown"),
    # HTTP 429 is throttling evidence, but still says nothing about a
    # resettable subscription allowance.
    ("HTTP 429: plan limit reached", "throttled"),
])
def test_generic_usage_or_plan_limit_is_not_a_subscription_limit(diagnostic, expected):
    """Synthetic terminal diagnostics without subscription evidence stay unknown."""
    assert _category_from_terminal(diagnostic) == expected


@pytest.mark.parametrize("diagnostic", [
    "billing service reported an error",
    "billing configuration could not be loaded",
    "credit balance is unavailable",
    "quota exceeded",
    "project quota exceeded",
])
def test_ambiguous_billing_or_quota_diagnostic_is_not_billing_credits(diagnostic):
    """Synthetic terminal diagnostics need explicit API-credit exhaustion."""
    assert _category_from_terminal(diagnostic) == "unknown"


@pytest.mark.parametrize("diagnostic", [
    "API credits exhausted",
    "billing credits have been depleted",
    "no remaining API credits",
])
def test_explicit_api_credit_exhaustion_is_billing_credits(diagnostic):
    """Synthetic terminal diagnostics explicitly establish exhausted credits."""
    assert _category_from_terminal(diagnostic) == "billing_credits"


@pytest.mark.parametrize("diagnostic", [
    "Your subscription limit resets at 17:00 UTC",
    "Your ChatGPT Plus limit resets on 2026-09-11",
    "Codex Pro has a weekly allowance",
])
def test_explicit_resettable_subscription_allowance_is_classified(diagnostic):
    """Synthetic diagnostics must identify both the product and reset/cadence."""
    assert _category_from_terminal(diagnostic) == "subscription_limit"


def test_captured_codex_usage_limit_terminal_envelopes(tmp_path):
    """Owner feedback 245 / T013 run 174, minimally retained and sanitized."""
    path = (Path(__file__).parent / "fixtures" /
            "codex-usage-limit.owner-feedback-245.run-174.captured.jsonl")
    events = path.read_text().splitlines()
    assert len(events) == 2
    for event in events:
        log_path = tmp_path / "session.log"
        log_path.write_text(event + "\n")
        for exit_code in (1, 0):
            assessment = assess_session(SessionResult(exit_code, log_path, None, False), "codex")
            assert (assessment.status, assessment.category) == ("FAILED", "subscription_limit")
            assert "You've hit your usage limit." in assessment.diagnostic


@pytest.mark.parametrize(("diagnostic", "expected"), [
    # Synthetic changed case/spacing variants exercise the grammar, not a
    # second captured provider message.
    (("YOU'VE  HIT your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro) "
      "visit https://chatgpt.com/codex/settings/usage or TRY AGAIN AT later"), "subscription_limit"),
    (("You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro) "
      "visit https://chatgpt.com/codex/settings/usage"), "unknown"),
    ("You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro) try again at later", "unknown"),
    ("You've hit your usage limit. visit https://chatgpt.com/codex/settings/usage try again at later", "unknown"),
])
def test_captured_codex_grammar_is_narrow_synthetic_variants(diagnostic, expected):
    assert _category_from_terminal(diagnostic) == expected


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


@pytest.mark.parametrize(("adapter", "event", "expected"), [
    ("codex", '{"type":"turn.completed","usage":{"input_tokens":120,"cached_input_tokens":80,"output_tokens":30}}', 150),
    ("claude", '{"type":"result","is_error":false,"usage":{"input_tokens":120,"output_tokens":30,"cache_creation_input_tokens":40,"cache_read_input_tokens":50}}', 240),
])
def test_parse_stream_tokens_from_terminal_usage(adapter, event, expected):
    """Synthetic current machine-stream terminal records retain usage."""
    assert parse_stream_tokens(adapter, event + "\n") == expected


@pytest.mark.parametrize(("adapter", "event", "expected"), [
    ("codex", '{"type":"turn.completed","usage":{"total_tokens":77}}', 77),
    ("claude", '{"type":"result","usage":{"total_tokens":88}}', 88),
])
def test_run_retains_terminal_stream_usage(tmp_path, monkeypatch, adapter, event, expected):
    """Both forced machine-stream adapter paths retain terminal token usage."""
    class Proc:
        pid = 123

        def wait(self, timeout=None):
            return 0

    def popen(cmd, **kwargs):
        kwargs["stdout"].write(event + "\n")
        return Proc()

    monkeypatch.setattr("nc.adapters.subprocess.Popen", popen)
    command = [adapter if adapter == "codex" else "/usr/bin/claude", "exec"]
    assert _run(command, tmp_path, tmp_path / "session.log", 10).tokens == expected


def test_stream_usage_ignores_nonterminal_and_truncated_records():
    """Synthetic arbitrary stream-like content is never token evidence."""
    assert parse_stream_tokens("codex", '{"type":"item.completed","usage":{"total_tokens":99}}\n') is None
    assert parse_stream_tokens("claude", '{"type":"result","usage":{"total_tokens":99}}\n{"type":') is None


def test_legacy_text_envelope_is_not_terminal_evidence(tmp_path):
    """Synthetic compatibility text must not pretend to be a CLI event."""
    path = tmp_path / "session.log"
    path.write_text("Codex API Error: rate_limit_exceeded\n")
    assessment = assess_session(SessionResult(1, path, None, False), "codex")
    assert assessment.category == "unknown"
    assert assessment.failed


@pytest.mark.parametrize(("adapter", "fixture", "expected"), [
    ("codex", "codex-terminal-error.synthetic.jsonl", "throttled"),
    ("claude", "claude-terminal-error.synthetic.jsonl", "authentication"),
])
def test_adapter_terminal_stream_is_terminal_evidence(tmp_path, adapter, fixture, expected):
    """Synthetic fixtures exercise the documented terminal event envelopes."""
    path = tmp_path / "session.log"
    path.write_text((Path(__file__).parent / "fixtures" / fixture).read_text())
    assessment = assess_session(SessionResult(0, path, None, False), adapter)
    assert assessment.category == expected
    assert assessment.failed


@pytest.mark.parametrize("adapter", ["codex", "claude"])
def test_fallback_never_searches_prompt_or_truncated_streams(tmp_path, adapter):
    path = tmp_path / "session.log"
    path.write_text('prompt says "rate_limit_exceeded"\n' * 1000 +
                    'agent quoted "Codex API Error: permission_denied"\n')
    assessment = assess_session(SessionResult(1, path, None, False), adapter)
    assert assessment.category == "unknown"


def test_truncated_jsonl_terminal_stream_is_unknown(tmp_path):
    path = tmp_path / "session.log"
    path.write_text('{"type":"error","message":"rate_limit_exceeded"')
    assessment = assess_session(SessionResult(1, path, None, False), "codex")
    assert assessment.category == "unknown"


@pytest.mark.parametrize(("adapter", "event"), [
    ("codex", '{"type":"error","message":"rate_limit_exceeded"}'),
    ("claude", '{"type":"result","is_error":true,"result":"rate_limit_exceeded"}'),
])
def test_trailing_truncated_stream_never_promotes_prior_event(tmp_path, adapter, event):
    path = tmp_path / "session.log"
    path.write_text(event + '\n{"type":')
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
def test_signal_killed_session_is_host_failure_for_both_adapter_paths(tmp_path, adapter):
    """Synthetic process status: a host SIGKILL is not an unknown bad exit."""
    path = tmp_path / "session.log"
    path.write_text('{"type":"error","message":"rate_limit_exceeded"}\n')
    assessment = assess_session(
        SessionResult(-9, path, None, False, "throttled", "rate_limit_exceeded"), adapter,
    )
    assert (assessment.status, assessment.category) == ("FAILED", "host_timeout")
    assert "SIGKILL (9)" in assessment.diagnostic


@pytest.mark.parametrize("adapter", ["codex", "claude"])
def test_run_preserves_signal_exit_code_for_both_adapter_paths(tmp_path, monkeypatch, adapter):
    """Synthetic adapter process verifies the real runner path retains -SIGNUM."""
    class Proc:
        pid = 123

        def wait(self, timeout=None):
            return -9

    def popen(cmd, **kwargs):
        kwargs["stdout"].write("killed by test host\n")
        return Proc()

    monkeypatch.setattr("nc.adapters.subprocess.Popen", popen)
    command = [adapter if adapter == "codex" else "/usr/bin/claude", "exec"]
    result = _run(command, tmp_path, tmp_path / "session.log", 10)
    assessment = assess_session(result, adapter)
    assert result.exit_code == -9
    assert assessment.category == "host_timeout"


@pytest.mark.parametrize("adapter", ["codex", "claude"])
def test_successful_retry_output_is_not_terminal_evidence(tmp_path, adapter):
    """Synthetic recovered stream: only terminal structured evidence can fail a zero exit."""
    path = tmp_path / "session.log"
    path.write_text("Codex API Error: rate_limit_exceeded\nretry succeeded\n")
    assessment = assess_session(SessionResult(0, path, None, False), adapter)
    assert assessment.status == "SUCCESS"


@pytest.mark.parametrize(("adapter", "events"), [
    ("codex", [
        '{"type":"error","message":"rate_limit_exceeded"}',
        '{"type":"turn.completed"}',
    ]),
    ("claude", [
        '{"type":"result","is_error":true,"result":"rate_limit_exceeded"}',
        '{"type":"result","subtype":"success","is_error":false,"result":"ok"}',
    ]),
])
def test_successful_structured_retry_wins_over_intermediate_error(tmp_path, adapter, events):
    path = tmp_path / "session.log"
    path.write_text("\n".join(events) + "\n")
    assert not assess_session(SessionResult(0, path, None, False), adapter).failed


@pytest.mark.parametrize(("adapter", "event", "category"), [
    ("codex", '{"type":"error","message":"insufficient_quota"}', "billing_credits"),
    ("codex", '{"type":"turn.failed","error":{"code":"model_not_found"}}', "invalid_request"),
    ("claude", '{"type":"result","is_error":true,"result":"service overloaded"}', "overloaded"),
    ("claude", '{"type":"result","is_error":true,"result":"permission_denied"}', "permission"),
])
def test_run_populates_terminal_evidence_from_adapter_stream(tmp_path, monkeypatch, adapter, event, category):
    class Proc:
        pid = 123

        def wait(self, timeout=None):
            return 0

    def popen(cmd, **kwargs):
        kwargs["stdout"].write(event + "\n")
        return Proc()

    monkeypatch.setattr("nc.adapters.subprocess.Popen", popen)
    command = [adapter if adapter == "codex" else "/usr/bin/claude", "exec"]
    result = _run(command, tmp_path, tmp_path / "session.log", 10)
    assert (result.exit_code, result.terminal_category) == (0, category)
    assert assess_session(result, adapter).failed


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
        "Bearer abcdefghijklmnop API_KEY=super-secret "
        "ANTHROPIC_API_KEY=plaintextcredential "
        "OPENAI_ACCESS_TOKEN=anotherplaintextcredential "
        "sk-abcdefghijklmnop password: hunter2")
    assert "abcdefghijklmnop" not in diagnostic
    assert "super-secret" not in diagnostic
    assert "hunter2" not in diagnostic
    assert "plaintextcredential" not in diagnostic
    assert "anotherplaintextcredential" not in diagnostic
    assert "[REDACTED]" in diagnostic


@pytest.mark.parametrize("diagnostic", [
    '{"access_token":"abcdefghijklmnop"}',
    '{"api_key": "super-secret"}',
    "{'client_secret': 'plaintextcredential'}",
])
def test_diagnostic_redacts_json_credential_fields(diagnostic):
    """Structured terminal diagnostics must be safe to retain and display."""
    sanitized = sanitize_diagnostic(diagnostic)
    assert "abcdefghijklmnop" not in sanitized
    assert "super-secret" not in sanitized
    assert "plaintextcredential" not in sanitized
    assert "[REDACTED]" in sanitized


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
    cmd = [binary or str(tmp_path / ".local/bin/claude"), "-p", prompt,
           "--output-format", "stream-json", "--verbose",
           "--permission-mode", "bypassPermissions"]
    if model:
        cmd += ["--model", model]
    run.assert_called_once_with(cmd, tmp_path, log, 30)
    assert result is run.return_value


@pytest.mark.parametrize("name", ["codex", "claude"])
def test_restricted_advisory_command_is_exact_for_each_adapter(tmp_path, monkeypatch, name):
    """Planner and plan critic share this exact restricted launch path."""
    from unittest.mock import Mock

    from nc.adapters import ClaudeAdapter, CodexAdapter

    run = Mock()
    monkeypatch.setattr("nc.adapters._run", run)
    monkeypatch.setattr("nc.adapters.shutil.which", lambda _name: None)
    monkeypatch.setattr("nc.adapters.Path.home", lambda: tmp_path)
    log = tmp_path / "session.log"
    adapter = CodexAdapter() if name == "codex" else ClaudeAdapter()
    adapter.run_planner("Plan", tmp_path, "model", log, 30)
    if name == "codex":
        expected = [
            "codex", "--search", "exec", "--json", "--model", "model",
            "--sandbox", "workspace-write", "--config",
            "sandbox_workspace_write.network_access=true", "--skip-git-repo-check", "Plan",
        ]
    else:
        binary = str(tmp_path / ".local/bin/claude")
        expected = [
            binary, "-p", "Plan", "--output-format", "stream-json", "--verbose",
            "--permission-mode", "dontAsk", "--tools",
            "Read,Glob,Grep,Write,WebSearch,WebFetch", "--allowedTools", "Read", "Glob",
            "Grep", "WebSearch", "WebFetch",
            f"Write(//{tmp_path.as_posix().lstrip('/')}/outcome.json)", "--model", "model",
        ]
    run.assert_called_once_with(expected, tmp_path, log, 30)


@pytest.mark.parametrize("name", ["codex", "claude"])
def test_worker_command_remains_unrestricted_and_has_no_research_flags(tmp_path, monkeypatch, name):
    """Web research is intentionally limited to the restricted advisory path."""
    from unittest.mock import Mock

    from nc.adapters import ClaudeAdapter, CodexAdapter

    run = Mock()
    monkeypatch.setattr("nc.adapters._run", run)
    monkeypatch.setattr("nc.adapters.shutil.which", lambda _name: None)
    monkeypatch.setattr("nc.adapters.Path.home", lambda: tmp_path)
    log = tmp_path / "session.log"
    adapter = CodexAdapter() if name == "codex" else ClaudeAdapter()
    adapter.run("Implement", tmp_path, "model", log, 30)
    if name == "codex":
        expected = [
            "codex", "exec", "--json", "--model", "model", "--sandbox",
            "danger-full-access", "--skip-git-repo-check", "Implement",
        ]
    else:
        expected = [
            str(tmp_path / ".local/bin/claude"), "-p", "Implement", "--output-format",
            "stream-json", "--verbose", "--permission-mode", "bypassPermissions",
            "--model", "model",
        ]
    run.assert_called_once_with(expected, tmp_path, log, 30)
