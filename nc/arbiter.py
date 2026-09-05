"""Deterministic arbitration: worktrees, acceptance checks, merge or rollback.

Nothing here uses an LLM. Counting verdicts, running tests and deciding whether a
branch is merged are decisions that do not need judgement — which is exactly why
the agent that wrote the code must not make them.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CheckResult:
    command: str
    ok: bool
    output: str

    def render(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        return f"[{status}] {self.command}\n{self.output.strip()[-1500:]}"


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=300,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def base_branch(repo: Path) -> str:
    for candidate in ("main", "master"):
        if git(repo, "rev-parse", "--verify", "--quiet", candidate, check=False):
            return candidate
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD")


def ensure_worktree(repo: Path, work_root: Path, task_id: str) -> tuple[Path, str]:
    """One task, one branch, one worktree. Rollback is `git worktree remove`."""
    branch = f"nc/{task_id}"
    path = work_root / task_id
    if path.exists():
        return path, branch
    path.parent.mkdir(parents=True, exist_ok=True)
    base = base_branch(repo)
    exists = git(repo, "rev-parse", "--verify", "--quiet", branch, check=False)
    if exists:
        git(repo, "worktree", "add", str(path), branch)
    else:
        git(repo, "worktree", "add", "-b", branch, str(path), base)
    return path, branch


def remove_worktree(repo: Path, path: Path) -> None:
    if path.exists():
        git(repo, "worktree", "remove", "--force", str(path), check=False)


def has_commits(repo: Path, worktree: Path, branch: str) -> bool:
    base = base_branch(repo)
    out = git(worktree, "rev-list", "--count", f"{base}..{branch}", check=False)
    return bool(out) and out != "0"


def parse_acceptance(acceptance: list[str]) -> tuple[list[str], list[str]]:
    """Split criteria into machine-checkable shell commands and prose criteria."""
    commands, prose = [], []
    for item in acceptance:
        stripped = item.strip()
        if stripped.startswith("$"):
            commands.append(stripped[1:].strip())
        else:
            prose.append(stripped)
    return commands, prose


def run_checks(cwd: Path, commands: list[str], timeout_s: int = 900) -> list[CheckResult]:
    results = []
    for command in commands:
        try:
            proc = subprocess.run(
                command, cwd=cwd, shell=True, capture_output=True, text=True,
                timeout=timeout_s, check=False,
            )
            results.append(CheckResult(command, proc.returncode == 0,
                                       (proc.stdout + proc.stderr)))
        except subprocess.TimeoutExpired:
            results.append(CheckResult(command, False, f"timed out after {timeout_s}s"))
    return results


def integrate(repo: Path, branch: str, task_id: str) -> str:
    """Fast-forward-or-merge the accepted branch into the project's base branch."""
    base = base_branch(repo)
    current = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if current != base:
        git(repo, "checkout", base)
    git(repo, "merge", "--no-ff", "-m", f"{task_id}: accepted by arbiter", branch)
    return git(repo, "rev-parse", "--short", "HEAD")


def mirror(repo: Path, remote: str | None, branch: str | None = None) -> str:
    """Push accepted work to a review remote. Failure here never blocks the queue."""
    if not remote:
        return ""
    base = base_branch(repo)
    # The mirror is a review surface, not the project's upstream: the runner's own
    # history lands on nc/<base> so it can never fight the forge's real base branch.
    refs = [f"{base}:refs/heads/nc/{base}"] + ([branch] if branch else [])
    proc = subprocess.run(
        ["git", "push", remote, *refs], cwd=repo, capture_output=True, text=True,
        timeout=300, check=False,
    )
    return "" if proc.returncode == 0 else (proc.stderr or proc.stdout).strip()[-500:]


def revert(repo: Path, commit: str) -> str:
    """Undo an accepted merge, keeping it in history."""
    base = base_branch(repo)
    if git(repo, "rev-parse", "--abbrev-ref", "HEAD") != base:
        git(repo, "checkout", base)
    parents = git(repo, "rev-list", "--parents", "-n", "1", commit).split()
    args = ["revert", "--no-edit", commit]
    if len(parents) > 2:                      # a merge commit: revert onto first parent
        args = ["revert", "--no-edit", "-m", "1", commit]
    git(repo, *args)
    return git(repo, "rev-parse", "--short", "HEAD")


def checks_summary(results: list[CheckResult]) -> str:
    if not results:
        return "(no automated checks defined)"
    return "\n".join(r.render() for r in results)


def acceptance_json(acceptance: list[str]) -> str:
    return json.dumps(acceptance, ensure_ascii=False, indent=2)


def quote(command: str) -> str:
    return shlex.quote(command)
