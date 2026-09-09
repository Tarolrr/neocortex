"""Adapters that run one bounded agent session through a coding CLI."""

from __future__ import annotations

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
        r"(?i)\b(api[_ -]?key|access[_ -]?token|authorization|password|secret)"
        r"\s*([=:])\s*([^\s,;]+)", r"\1\2[REDACTED]", text,
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
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    line = lines[-1] if lines else ""
    # These are intentionally distinct.  They are conservative fixture-backed
    # envelopes, not claims that every version emits every form (see docs).
    envelopes = {
        "codex": r"^Codex API Error:\s*(?P<code>[a-z_]+)\s*$",
        "claude": r"^Claude API Error:\s*(?P<code>[a-z_]+)\s*$",
    }
    match = re.match(envelopes.get(adapter, r"(?!x)x"), line, re.IGNORECASE)
    codes = {
        "usage_limit": "subscription_limit", "rate_limit_exceeded": "throttled",
        "overloaded": "overloaded", "server_error": "overloaded",
        "temporarily_unavailable": "transient", "network_error": "transient",
        "authentication_error": "authentication", "permission_denied": "permission",
        "invalid_request": "invalid_request", "invalid_model": "invalid_request",
        "insufficient_quota": "billing_credits",
    }
    if match:
        return codes.get(match.group("code").lower(), "unknown"), sanitize_diagnostic(
            f"{adapter}: {line}")
    return "unknown", sanitize_diagnostic(line)


def assess_session(result: SessionResult, adapter: str) -> HostAssessment:
    """Apply host evidence precedence before an outcome can have effects."""
    if result.timed_out:
        return HostAssessment("FAILED", "host_timeout", "host timeout")
    terminal_category = getattr(result, "terminal_category", None)
    if terminal_category:
        category = terminal_category if terminal_category in _TERMINAL_CATEGORIES else "unknown"
        return HostAssessment("FAILED", category,
                              sanitize_diagnostic(getattr(result, "terminal_diagnostic", "")))
    if result.exit_code != 0:
        category, diagnostic = _fallback_terminal(adapter, result.log_path)
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
    tokens = parse_tokens(text)
    return SessionResult(exit_code=code, log_path=log_path, tokens=tokens, timed_out=timed_out)


class CodexAdapter(Adapter):
    name = "codex"

    def available(self) -> bool:
        return shutil.which("codex") is not None

    def run(self, prompt: str, cwd: Path, model: str, log_path: Path,
            timeout_s: int) -> SessionResult:
        cmd = [
            "codex", "exec",
            "--model", model,
            "--sandbox", "danger-full-access",
            "--skip-git-repo-check",
            prompt,
        ]
        return _run(cmd, cwd, log_path, timeout_s)

    def run_planner(self, prompt: str, cwd: Path, model: str, log_path: Path,
                    timeout_s: int) -> SessionResult:
        return _run([
            "codex", "exec", "--model", model, "--sandbox", "workspace-write",
            "--skip-git-repo-check", prompt,
        ], cwd, log_path, timeout_s)


class ClaudeAdapter(Adapter):
    name = "claude"

    def available(self) -> bool:
        return shutil.which("claude") is not None or (Path.home() / ".local/bin/claude").exists()

    def run(self, prompt: str, cwd: Path, model: str, log_path: Path,
            timeout_s: int) -> SessionResult:
        binary = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
        cmd = [binary, "-p", prompt, "--permission-mode", "bypassPermissions"]
        if model:
            cmd += ["--model", model]
        return _run(cmd, cwd, log_path, timeout_s)

    def run_planner(self, prompt: str, cwd: Path, model: str, log_path: Path,
                    timeout_s: int) -> SessionResult:
        binary = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
        cmd = [binary, "-p", prompt, "--permission-mode", "dontAsk",
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
