"""Adapters that run one bounded agent session through a coding CLI."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

# codex exec's text footer is two lines: "tokens used\n26,457".
TOKENS_RE = re.compile(
    r"^[ \t]*tokens used[ \t]*:?[ \t]*\r?\n"
    r"[ \t]*([0-9]+(?:,[0-9]{3})*)[ \t]*\r?$",
    re.IGNORECASE | re.MULTILINE,
)

_on_adapter_started: ContextVar[object | None] = ContextVar("on_adapter_started", default=None)


def _adapter_cgroup() -> Path | None:
    """Make a non-delegated cgroup before fork, if the host provides cgroup v2.

    The child joins it in ``preexec_fn``, before untrusted adapter code can
    create descendants.  A process may leave its session/process group, but it
    remains listed in this cgroup.  If this cannot be established, the run is
    deliberately recorded as ownership-uncertain rather than recoverable.
    """
    try:
        line = next(line for line in Path("/proc/self/cgroup").read_text().splitlines()
                    if line.startswith("0::"))
        parent = Path("/sys/fs/cgroup") / line[3:].lstrip("/")
        path = parent / f"neocortex-run-{os.getpid()}-{uuid.uuid4().hex}"
        path.mkdir()
        return path
    except (FileNotFoundError, OSError, StopIteration):
        return None


def _join_cgroup(path: Path) -> None:
    # This executes in the just-forked child, before exec and adapter code.
    (path / "cgroup.procs").write_text(str(os.getpid()))


def _remove_cgroup(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.rmdir()
    except OSError:
        # A survivor is useful ownership evidence for interrupted recovery.
        pass


@contextmanager
def adapter_ownership(callback):
    """Let the host record a real adapter session without changing Adapter's API."""
    token = _on_adapter_started.set(callback)
    try:
        yield
    finally:
        _on_adapter_started.reset(token)


def parse_tokens(text: str) -> int | None:
    """Read the last Codex usage footer, or leave unreported usage unknown."""
    matches = list(TOKENS_RE.finditer(text))
    return int(matches[-1].group(1).replace(",", "")) if matches else None


def _usage_total(usage: object, adapter: str) -> int | None:
    """Return the token total from an adapter-owned terminal usage object.

    The two CLIs use different accounting shapes.  Codex's cached input is a
    detail of its input total, whereas Claude reports cache reads/creation as
    separate billable input fields.  Prefer an explicit total when supplied;
    malformed or incomplete usage remains unknown rather than guessing.
    """
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        return total
    if adapter == "codex":
        fields = ("input_tokens", "output_tokens")
    elif adapter == "claude":
        fields = (
            "input_tokens", "output_tokens", "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    else:
        return None
    values = [usage.get(field) for field in fields]
    if not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
               for value in values[:2]):
        return None
    # Claude's cache fields are optional in older stream-json versions.
    if adapter == "claude":
        values = values[:2] + [value for value in values[2:] if value is not None]
    if not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
               for value in values):
        return None
    return sum(values)


def parse_stream_tokens(adapter: str, text: str) -> int | None:
    """Read usage only from the final machine-stream terminal event.

    This intentionally mirrors terminal-failure parsing: stream records in
    tool output or an earlier failed retry are not session accounting.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        event = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    if adapter == "codex" and event.get("type") not in {"turn.completed", "turn.failed"}:
        return None
    if adapter == "claude" and event.get("type") != "result":
        return None
    if adapter not in {"codex", "claude"}:
        return None
    return _usage_total(event.get("usage"), adapter)


@dataclass
class SessionResult:
    exit_code: int
    log_path: Path
    tokens: int | None
    timed_out: bool
    # Adapters with a structured terminal event may set this.  A log scrape is
    # intentionally only used for a non-zero process exit: normal CLI output
    # can contain quoted provider errors from prompts, tools, or summaries.
    terminal_category: str | None = None
    terminal_diagnostic: str = ""


@dataclass(frozen=True)
class HostAssessment:
    """Independent evidence about the CLI process, not an agent decision."""

    status: str                    # SUCCESS|FAILED
    category: str                  # none|host_timeout|...|unknown
    diagnostic: str = ""

    @property
    def failed(self) -> bool:
        return self.status == "FAILED"


_TERMINAL_CATEGORIES = {
    "subscription_limit", "throttled", "overloaded", "transient",
    "authentication", "permission", "invalid_request", "billing_credits",
    "local_error", "host_timeout", "protocol", "unknown",
}


def sanitize_diagnostic(text: str) -> str:
    """Keep a short printable terminal excerpt suitable for SQLite/UI."""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    # Logs are untrusted and frequently include command-line configuration.
    # Do this before bounding it: a short prefix must never make a credential
    # visible in the run list or the detail view.
    text = re.sub(r"(?i)\b(bearer\s+)[^\s,;]+", r"\1[REDACTED]", text)
    text = re.sub(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", text)
    text = re.sub(
        # Environment variables commonly namespace the credential name, e.g.
        # ANTHROPIC_API_KEY and OPENAI_ACCESS_TOKEN.  ``_`` is a word
        # character, so a boundary before API/ACCESS alone would miss them.
        r"(?i)\b((?:(?:[a-z][a-z0-9]*_)+)?(?:api[_ -]?key|access[_ -]?token|"
        r"authorization|password|secret|token|client[_ -]?secret|"
        r"secret[_ -]?access[_ -]?key))\s*([=:])\s*([^\s,;]+)",
        r"\1\2[REDACTED]", text,
    )
    return " ".join(text.split())[:1000]


def _fallback_terminal(adapter: str, log_path: Path) -> tuple[str, str]:
    """Classify a final, adapter-owned diagnostic after a bad exit.

    stdout and stderr are deliberately combined by ``_run``.  Consequently a
    general search is unsafe: prompts, tool output and final prose may contain
    provider words.  Only a final line in a documented CLI-shaped envelope is
    considered; all other output is unknown evidence.
    """
    try:
        text = log_path.read_text(errors="replace")[-16000:]
    except OSError:
        return "unknown", "terminal diagnostic unavailable"
    structured = _structured_terminal(adapter, text)
    if structured is not None:
        return structured
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # The narrow fallback deliberately reports no provider category.  A
    # non-JSON final line can establish that the launcher failed, but cannot
    # establish whose service failed or what a quoted API-looking phrase means.
    return "unknown", sanitize_diagnostic(lines[-1] if lines else "")


def _structured_terminal_from_log(adapter: str, log_path: Path) -> tuple[str, str] | None:
    try:
        return _structured_terminal(adapter, log_path.read_text(errors="replace")[-16000:])
    except OSError:
        return None


def _error_text(event: dict[str, object]) -> str:
    """Extract only fields owned by a terminal stream event.

    Do not recursively walk an event: tool input/output and assistant prose are
    deliberately arbitrary and must never become provider evidence.
    """
    bits: list[str] = []
    for key in ("code", "message", "error", "result"):
        value = event.get(key)
        if isinstance(value, str):
            bits.append(value)
        elif key == "error" and isinstance(value, dict):
            for error_key in ("code", "message", "type"):
                error_value = value.get(error_key)
                if isinstance(error_value, str):
                    bits.append(error_value)
    return " ".join(bits)


def _category_from_terminal(text: str) -> str:
    """Map a *structured terminal* provider diagnostic to the runner taxonomy."""
    value = text.lower()
    # Ordered from specific product/account states to broad HTTP-style errors.
    # A bare "usage limit" or "plan limit" also occurs for API organization
    # spend limits.  It cannot establish a resettable end-user subscription
    # allowance.  Require both an explicit consumer subscription/product plan
    # and an explicit reset/cadence in the terminal event.
    subscription_product = re.search(
        r"\bsubscription\b|\b(?:chatgpt|codex)\s+"
        r"(?:plus|pro|team|business|enterprise|plan)\b", value,
    )
    resettable_allowance = re.search(
        r"\b(?:limit\s+)?resets?\s+(?:at|on|in)\b|"
        r"\b(?:weekly|monthly)\s+(?:subscription|allowance|limit)\b", value,
    )
    if subscription_product and resettable_allowance:
        return "subscription_limit"
    if any(token in value for token in (
        "insufficient_quota", "billing", "credit balance", "credits exhausted",
        "quota exceeded",
    )):
        return "billing_credits"
    if any(token in value for token in (
        "permission_denied", "permission denied", "forbidden", "not authorized",
    )):
        return "permission"
    if any(token in value for token in (
        "authentication", "unauthenticated", "invalid api key", "login required",
        "not logged in", "expired token",
    )):
        return "authentication"
    if any(token in value for token in (
        "invalid_request", "invalid request", "bad request", "model_not_found",
        "model not found", "unsupported model", "unknown model",
    )):
        return "invalid_request"
    if any(token in value for token in (
        "rate_limit", "rate limit", "too many requests", "http 429", "status 429",
    )):
        return "throttled"
    if any(token in value for token in (
        "overloaded", "overload", "capacity", "http 529", "status 529",
    )):
        return "overloaded"
    if any(token in value for token in (
        "server_error", "internal server error", "connection", "network",
        "transport", "temporarily unavailable", "http 5", "status 5",
    )):
        return "transient"
    return "unknown"


def _structured_terminal(adapter: str, text: str) -> tuple[str, str] | None:
    """Return a final failure event from the requested adapter stream.

    ``stdout`` and ``stderr`` share one log, so accepting a JSON record in the
    middle would let a tool or quoted transcript masquerade as terminal
    evidence.  We accept only a complete JSON object on the final nonblank
    line, with the terminal shape emitted by that adapter.  A later successful
    terminal event therefore wins over a recovered intermediate failure.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        event = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    event_type = event.get("type")
    if adapter == "codex":
        # codex exec --json emits a terminal error record (and turn.failed in
        # newer compatible streams).  item.* records are intentionally absent.
        failed = event_type == "error" or event_type == "turn.failed"
    elif adapter == "claude":
        # Claude -p stream-json's terminal result uses is_error; do not infer
        # failure from assistant/tool records that merely contain error prose.
        failed = event_type == "result" and event.get("is_error") is True
    else:
        return None
    if not failed:
        return None
    diagnostic = _error_text(event)
    return _category_from_terminal(diagnostic), sanitize_diagnostic(diagnostic)


def assess_session(result: SessionResult, _adapter: str) -> HostAssessment:
    """Apply host evidence precedence before an outcome can have effects."""
    if result.timed_out:
        return HostAssessment("FAILED", "host_timeout", "host timeout")
    # On POSIX, subprocess reports a signal-terminated child as -SIGNUM.
    # This is independent host evidence even when the deadline wrapper did not
    # set ``timed_out`` (for example, containment/recovery killed the process).
    # Keep it ahead of adapter diagnostics: the diagnostic may describe an
    # earlier provider error, but it cannot erase the terminal host kill.
    if result.exit_code < 0:
        signum = -result.exit_code
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = f"signal {signum}"
        return HostAssessment(
            "FAILED", "host_timeout",
            sanitize_diagnostic(f"host-killed process terminated by {name} ({signum})"),
        )
    terminal_category = getattr(result, "terminal_category", None)
    if terminal_category:
        category = terminal_category if terminal_category in _TERMINAL_CATEGORIES else "unknown"
        return HostAssessment("FAILED", category,
                              sanitize_diagnostic(getattr(result, "terminal_diagnostic", "")))
    # Older/custom adapters may not have populated SessionResult, but their
    # log is still the requested machine stream.  Preserve terminal failure
    # precedence even when it exited zero.
    structured = _structured_terminal_from_log(_adapter, result.log_path)
    if structured is not None:
        category, diagnostic = structured
        return HostAssessment("FAILED", category, diagnostic)
    if result.exit_code != 0:
        category, diagnostic = _fallback_terminal(_adapter, result.log_path)
        return HostAssessment("FAILED", category, diagnostic or f"CLI exited {result.exit_code}")
    return HostAssessment("SUCCESS", "none", "")


class Adapter:
    name = "adapter"

    def available(self) -> bool:
        raise NotImplementedError

    def run(self, prompt: str, cwd: Path, model: str, log_path: Path,
            timeout_s: int) -> SessionResult:
        raise NotImplementedError


def _run(cmd: list[str], cwd: Path, log_path: Path, timeout_s: int) -> SessionResult:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PATH=f"{Path.home()}/.local/bin:{os.environ.get('PATH', '')}")
    timed_out = False
    cgroup = _adapter_cgroup()
    adapter = Path(cmd[0]).name
    if adapter not in {"codex", "claude"}:
        adapter = ""
    with log_path.open("w") as log:
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                env=env, start_new_session=True,
                preexec_fn=(lambda: _join_cgroup(cgroup)) if cgroup is not None else None,  # noqa: PLW1509 - containment before adapter exec
            )
            callback = _on_adapter_started.get()
            if callback is not None:
                try:
                    callback(proc.pid)
                except BaseException:
                    # A run cannot be finalized as failed while an adapter
                    # launched for it survives unrecorded.  The adapter owns
                    # a fresh session, so kill its complete process group.
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        # Keep the callback error as the reported failure.
                        pass
                    raise
            code = proc.wait(timeout=timeout_s)
        except OSError as exc:
            # Popen's launcher/filesystem errors are host evidence, distinct
            # from an agent-authored FAIL or a provider terminal diagnostic.
            diagnostic = sanitize_diagnostic(str(exc))
            log.write(f"local launcher error: {diagnostic}\n")
            _remove_cgroup(cgroup)
            return SessionResult(127, log_path, None, False, "local_error", diagnostic)
        except subprocess.TimeoutExpired:
            timed_out = True
            code = 124
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait()
    _remove_cgroup(cgroup)
    text = log_path.read_text(errors="replace")
    # Current adapters run in JSON/stream-json mode.  Retain their terminal
    # usage independently of outcome parsing, while preserving old text logs.
    stream_tokens = parse_stream_tokens(adapter, text)
    tokens = stream_tokens if stream_tokens is not None else parse_tokens(text)
    terminal = _structured_terminal(adapter, text[-16000:])
    return SessionResult(
        exit_code=code, log_path=log_path, tokens=tokens, timed_out=timed_out,
        terminal_category=terminal[0] if terminal else None,
        terminal_diagnostic=terminal[1] if terminal else "",
    )


class CodexAdapter(Adapter):
    name = "codex"

    def available(self) -> bool:
        return shutil.which("codex") is not None

    def run(self, prompt: str, cwd: Path, model: str, log_path: Path,
            timeout_s: int) -> SessionResult:
        cmd = [
            "codex", "exec",
            "--json",
            "--model", model,
            "--sandbox", "danger-full-access",
            "--skip-git-repo-check",
            prompt,
        ]
        return _run(cmd, cwd, log_path, timeout_s)

    def run_planner(self, prompt: str, cwd: Path, model: str, log_path: Path,
                    timeout_s: int) -> SessionResult:
        return _run([
            "codex", "exec", "--json", "--model", model, "--sandbox", "workspace-write",
            "--skip-git-repo-check", prompt,
        ], cwd, log_path, timeout_s)


class ClaudeAdapter(Adapter):
    name = "claude"

    def available(self) -> bool:
        return shutil.which("claude") is not None or (Path.home() / ".local/bin/claude").exists()

    def run(self, prompt: str, cwd: Path, model: str, log_path: Path,
            timeout_s: int) -> SessionResult:
        binary = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
        cmd = [binary, "-p", prompt, "--output-format", "stream-json", "--verbose",
               "--permission-mode", "bypassPermissions"]
        if model:
            cmd += ["--model", model]
        return _run(cmd, cwd, log_path, timeout_s)

    def run_planner(self, prompt: str, cwd: Path, model: str, log_path: Path,
                    timeout_s: int) -> SessionResult:
        binary = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
        cmd = [binary, "-p", prompt, "--output-format", "stream-json", "--verbose",
               "--permission-mode", "dontAsk",
               "--tools", "Read,Glob,Grep,Write", "--allowedTools",
               "Read", "Glob", "Grep", f"Write(//{cwd.as_posix().lstrip('/')}/outcome.json)"]
        if model:
            cmd += ["--model", model]
        return _run(cmd, cwd, log_path, timeout_s)


ADAPTERS: dict[str, Adapter] = {a.name: a for a in (CodexAdapter(), ClaudeAdapter())}


def get_adapter(name: str) -> Adapter:
    if name not in ADAPTERS:
        raise KeyError(f"unknown adapter {name!r}; known: {sorted(ADAPTERS)}")
    return ADAPTERS[name]
