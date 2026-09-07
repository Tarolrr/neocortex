"""Adapters that run one bounded agent session through a coding CLI."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
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
    with log_path.open("w") as log:
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                env=env, start_new_session=True,
            )
            callback = _on_adapter_started.get()
            if callback is not None:
                callback(proc.pid)
            code = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            code = 124
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait()
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
