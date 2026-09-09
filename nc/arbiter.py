"""Deterministic arbitration: worktrees, acceptance checks, merge or rollback.

Nothing here uses an LLM. Counting verdicts, running tests and deciding whether a
branch is merged are decisions that do not need judgement — which is exactly why
the agent that wrote the code must not make them.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Keep this in lockstep with deploy/neocortex.service and bootstrap.sh.  Do
# not consult the invoking login shell: the scheduler is started by systemd.
SERVICE_PATH = "/opt/neocortex-runner/.venv/bin:/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
CLEANUP_TIMEOUT_S = 30


@dataclass
class CheckResult:
    command: str
    ok: bool
    output: str

    def render(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        return f"[{status}] {self.command}\n{self.output.strip()[-1500:]}"


class MergeConflict(RuntimeError):
    def __init__(self, branch: str, files: list[str]):
        self.files = files
        super().__init__(
            f"{branch} conflicts with the base branch in: {', '.join(files)}"
        )


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
        git(repo, "worktree", "remove", "--force", str(path))


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


def run_checks(cwd: Path, commands: list[str], timeout_s: int = 900,
               env: dict[str, str] | None = None) -> list[CheckResult]:
    """Run checks in isolated process groups and disposable temp directories."""
    results = []
    for command in commands:
        with tempfile.TemporaryDirectory(prefix="nc-check-") as tmp:
            check_env = dict(os.environ if env is None else env,
                             TMPDIR=tmp, TMP=tmp, TEMP=tmp)
            proc = subprocess.Popen(command, cwd=cwd, shell=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, env=check_env, start_new_session=True)
            try:
                stdout, _ = proc.communicate(timeout=timeout_s)
                results.append(CheckResult(command, proc.returncode == 0, stdout))
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                results.append(CheckResult(command, False, f"timed out after {timeout_s}s"))
    return results


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.communicate()


def host_requirements(adapter_clis: set[str]) -> tuple[list[str], list[str], str | None]:
    """Resolve runner prerequisites using systemd's PATH, never login PATH."""
    reports, errors = [f"service PATH: {SERVICE_PATH}"], []
    resolved: dict[str, str] = {}
    for name in ("python", "git", "sqlite3", "pytest", "ruff", *sorted(adapter_clis)):
        path = shutil.which(name, path=SERVICE_PATH)
        if path is None:
            errors.append(f"missing {name} on service PATH")
        else:
            resolved[name] = path
            reports.append(f"{name}: {path}")
    python = resolved.get("python")
    if python:
        try:
            version = subprocess.run([python, "--version"], capture_output=True, text=True,
                                     timeout=10, check=False,
                                     env=dict(os.environ, PATH=SERVICE_PATH))
            text = (version.stdout + version.stderr).strip()
            reports.append(f"python version: {text}")
            if version.returncode or not text.startswith("Python 3.13."):
                errors.append(f"python must be Python 3.13 (found {text or 'unusable'})")
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"python cannot be executed: {exc}")
    return reports, errors, resolved.get("python")


def _remove_readiness_worktree(repo: Path, scratch: Path, git_path: str,
                               env: dict[str, str]) -> str | None:
    """Remove only ``scratch`` and its registration; never prune the repository.

    ``git worktree prune`` is intentionally unsuitable here: it can alter
    registrations for unrelated task worktrees.  If git cannot remove this
    disposable worktree, remove its directory and then only the metadata entry
    whose ``gitdir`` points at this exact scratch checkout.
    """
    remove_error = ""
    try:
        cleanup = subprocess.Popen(
            [git_path, "worktree", "remove", "--force", str(scratch)], cwd=repo,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
            start_new_session=True,
        )
        try:
            stdout, stderr = cleanup.communicate(timeout=CLEANUP_TIMEOUT_S)
            if cleanup.returncode:
                remove_error = (stderr or stdout).strip() or "git worktree remove failed"
        except subprocess.TimeoutExpired:
            try:
                os.killpg(cleanup.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            cleanup.communicate()
            remove_error = f"git worktree remove timed out after {CLEANUP_TIMEOUT_S}s"
    except OSError as exc:
        remove_error = str(exc)

    # A failed git removal leaves the checkout and registration behind.  Both
    # fallback operations are scoped by the exact scratch path, not a broad
    # repository prune.
    try:
        if scratch.exists():
            shutil.rmtree(scratch)
        common = subprocess.run([git_path, "rev-parse", "--git-common-dir"], cwd=repo,
                                capture_output=True, text=True, timeout=30,
                                check=False, env=env)
        if common.returncode:
            return f"readiness cleanup failed: {remove_error or common.stderr.strip()}"
        common_dir = Path(common.stdout.strip())
        if not common_dir.is_absolute():
            common_dir = repo / common_dir
        registrations = common_dir / "worktrees"
        for entry in registrations.iterdir() if registrations.is_dir() else ():
            gitdir_file = entry / "gitdir"
            if not gitdir_file.is_file():
                continue
            registered = Path(gitdir_file.read_text().strip()).resolve()
            if registered == (scratch / ".git").resolve():
                shutil.rmtree(entry)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"readiness cleanup failed: {remove_error or exc}"

    # Do not claim success until this particular registration is gone.
    try:
        listing = subprocess.run([git_path, "worktree", "list", "--porcelain"], cwd=repo,
                                 capture_output=True, text=True, timeout=30, check=False, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"readiness cleanup failed: {remove_error or exc}"
    registered_paths = [line[9:] for line in listing.stdout.splitlines()
                        if line.startswith("worktree ")]
    if listing.returncode or str(scratch.resolve()) in registered_paths:
        detail = listing.stderr.strip() or remove_error or "scratch worktree is still registered"
        return f"readiness cleanup failed: {detail}"
    return None


def readiness_check(repo: Path, test_cmd: str, *, timeout_s: int = 900,
                    python: str | None = None) -> list[CheckResult]:
    """Run a project's configured test command from a detached disposable base tree."""
    git_path = shutil.which("git", path=SERVICE_PATH)
    if git_path is None:
        return [CheckResult(test_cmd, False, "git is missing from service PATH")]
    env = dict(os.environ, PATH=SERVICE_PATH)
    # Keep the disposable checkout on the project's filesystem rather than
    # shared /tmp, which can be unavailable even when the runner is healthy.
    scratch: Path | None = None
    results: list[CheckResult] = []
    try:
        # `worktree add` requires a nonexistent destination.  Treat failures
        # allocating or clearing that destination as a normal readiness
        # failure, rather than leaking an exception into `nc doctor`/`nc run`.
        scratch = Path(tempfile.mkdtemp(prefix="nc-readiness-", dir=repo.parent))
        scratch.rmdir()
        # Resolve the base with the same git executable we just validated.
        branches = subprocess.run([git_path, "branch", "--format=%(refname:short)"], cwd=repo,
                                  capture_output=True, text=True, timeout=300, check=False,
                                  env=env)
        names = branches.stdout.splitlines()
        if branches.returncode:
            results.append(CheckResult(test_cmd, False, branches.stderr.strip()))
            return results
        base = "main" if "main" in names else "master" if "master" in names else subprocess.run(
            [git_path, "branch", "--show-current"], cwd=repo, capture_output=True, text=True,
            timeout=300, check=False, env=env).stdout.strip()
        if not base:
            results.append(CheckResult(test_cmd, False, "cannot detect a base branch"))
            return results
        proc = subprocess.run([git_path, "worktree", "add", "--detach", str(scratch), base],
                              cwd=repo, capture_output=True, text=True, timeout=300,
                              check=False, env=env)
        if proc.returncode:
            results.append(CheckResult(test_cmd, False, proc.stderr.strip()))
            return results
        results = run_checks(scratch, [test_cmd], timeout_s, env)
        # A source checkout of Neocortex must import itself, not an unrelated
        # installed copy.  Other projects do not have this package contract.
        if (scratch / "nc").is_dir() and python:
            code = "import nc; from pathlib import Path; assert Path(nc.__file__).resolve().is_relative_to(Path.cwd().resolve())"
            try:
                probe = subprocess.run([python, "-c", code], cwd=scratch, capture_output=True,
                                       text=True, timeout=timeout_s, check=False, env=env)
                results.append(CheckResult("worktree-local import nc", probe.returncode == 0,
                                           probe.stdout + probe.stderr))
            except subprocess.TimeoutExpired:
                results.append(CheckResult("worktree-local import nc", False,
                                           f"timed out after {timeout_s}s"))
        return results
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        results.append(CheckResult(test_cmd, False, str(exc)))
        return results
    finally:
        # `git worktree add` can create a checkout or registration before it
        # reports a failure/timeout.  The mkdtemp directory itself can also
        # survive a failed rmdir.  Clean the exact allocated path regardless
        # of which setup step completed.
        if scratch is not None:
            cleanup_error = _remove_readiness_worktree(repo, scratch, git_path, env)
            if cleanup_error:
                results.append(CheckResult("readiness scratch cleanup", False, cleanup_error))


def integrate(repo: Path, branch: str, task_id: str) -> str:
    """Fast-forward-or-merge the accepted branch into the project's base branch."""
    if (repo / ".git" / "MERGE_HEAD").exists():
        git(repo, "merge", "--abort", check=False)
    base = base_branch(repo)
    current = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if current != base:
        git(repo, "checkout", base)
    proc = subprocess.run(
        ["git", "merge", "--no-ff", "-m", f"{task_id}: accepted by arbiter", branch],
        cwd=repo, capture_output=True, text=True, check=False, timeout=300,
    )
    if proc.returncode != 0:
        files = git(repo, "diff", "--name-only", "--diff-filter=U").splitlines()
        git(repo, "merge", "--abort", check=False)
        if files:
            raise MergeConflict(branch, files)
        raise RuntimeError(f"git merge --no-ff -m {task_id}: accepted by arbiter "
                           f"{branch} failed: {proc.stderr.strip()}")
    return git(repo, "rev-parse", "--short", "HEAD")


def _mirror_tip(repo: Path, ref: str) -> str:
    """Best-effort local object name for a mirror failure report."""
    try:
        return git(repo, "rev-parse", ref)
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return "unknown"


def _remote_mirror_tip(repo: Path, remote: str, ref: str) -> str:
    """Best-effort remote object name for a mirror failure report."""
    try:
        proc = subprocess.run(
            ["git", "ls-remote", "--heads", remote, ref], cwd=repo,
            capture_output=True, text=True, timeout=30, check=False,
        )
        if proc.returncode:
            return "unknown"
        return proc.stdout.split()[0] if proc.stdout.split() else "(missing)"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def mirror(repo: Path, remote: str | None, branch: str | None = None) -> str:
    """Push base and accepted task branch without letting failures block local work."""
    if not remote:
        return ""
    base = base_branch(repo)
    base_ref = f"refs/heads/{base}"
    refs = [f"{base}:{base_ref}"] + ([f"{branch}:refs/heads/{branch}"] if branch else [])
    local_tip = _mirror_tip(repo, base)
    try:
        proc = subprocess.run(
            ["git", "push", remote, *refs], cwd=repo, capture_output=True, text=True,
            timeout=300, check=False,
        )
        if proc.returncode == 0:
            return ""
        reason = (proc.stderr or proc.stdout).strip()[-500:]
    except (OSError, subprocess.TimeoutExpired) as exc:
        reason = str(exc)
    remote_tip = _remote_mirror_tip(repo, remote, base_ref)
    return (f"remote={remote} base={base_ref} local_tip={local_tip} "
            f"remote_tip={remote_tip} reason={reason}")


def revert(repo: Path, commit: str) -> str:
    """Undo an accepted merge, keeping it in history."""
    # Do not abort a pre-existing operation or disturb local changes.
    for marker in ("REVERT_HEAD", "MERGE_HEAD", "CHERRY_PICK_HEAD", "sequencer"):
        path = Path(git(repo, "rev-parse", "--git-path", marker))
        if (path if path.is_absolute() else repo / path).exists():
            raise RuntimeError("repository has an operation in progress")
    if git(repo, "status", "--porcelain"):
        raise RuntimeError("repository must be clean before rollback")
    base = base_branch(repo)
    if git(repo, "rev-parse", "--abbrev-ref", "HEAD") != base:
        git(repo, "checkout", base)
    parents = git(repo, "rev-list", "--parents", "-n", "1", commit).split()
    args = ["revert", "--no-edit", commit]
    if len(parents) > 2:                      # a merge commit: revert onto first parent
        args = ["revert", "--no-edit", "-m", "1", commit]
    try:
        git(repo, *args)
    except (RuntimeError, subprocess.TimeoutExpired):
        marker = Path(git(repo, "rev-parse", "--git-path", "REVERT_HEAD"))
        if (marker if marker.is_absolute() else repo / marker).exists():
            git(repo, "revert", "--abort")
        raise
    return git(repo, "rev-parse", "--short", "HEAD")


def checks_summary(results: list[CheckResult]) -> str:
    if not results:
        return "(no automated checks defined)"
    return "\n".join(r.render() for r in results)


def acceptance_json(acceptance: list[str]) -> str:
    return json.dumps(acceptance, ensure_ascii=False, indent=2)


def quote(command: str) -> str:
    return shlex.quote(command)
