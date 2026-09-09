from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from nc import arbiter


def _repo(tmp_path, branch="main"):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", branch, "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "nc@test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "nc"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "seed"], cwd=repo, check=True)
    return repo


def test_integrate_leaves_a_clean_repo_after_a_conflict(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "nc@test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "nc"], cwd=repo, check=True)
    (repo / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)

    subprocess.run(["git", "checkout", "-b", "nc/T1"], cwd=repo, check=True,
                   capture_output=True)
    (repo / "README.md").write_text("from worker\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "worker"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True,
                   capture_output=True)
    (repo / "README.md").write_text("from main\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "main"], cwd=repo, check=True)

    with pytest.raises(arbiter.MergeConflict) as raised:
        arbiter.integrate(repo, "nc/T1", "T1")

    assert raised.value.files == ["README.md"]
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout == ""


def test_readiness_uses_detected_non_main_base_and_cleans_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "trunk", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "nc@test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "nc"], cwd=repo, check=True)
    (repo / "marker").write_text("base")
    subprocess.run(["git", "add", "marker"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)

    results = arbiter.readiness_check(repo, "test -f marker", timeout_s=5)

    assert [result.ok for result in results] == [True]
    listing = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=repo,
                             capture_output=True, text=True, check=True).stdout
    assert "nc-readiness-" not in listing


@pytest.mark.parametrize(("command", "timeout"), [("false", 5), ("sleep 1", 0.01)])
def test_readiness_reports_failure_and_timeout_and_cleans_worktree(tmp_path, command, timeout):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "nc@test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "nc"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "seed"], cwd=repo, check=True)

    result = arbiter.readiness_check(repo, command, timeout_s=timeout)[0]

    assert not result.ok
    assert "nc-readiness-" not in subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=repo, capture_output=True,
        text=True, check=True).stdout


@pytest.mark.parametrize("failure", ["allocate", "clear"])
def test_readiness_reports_scratch_setup_errors(tmp_path, monkeypatch, failure):
    repo = _repo(tmp_path)
    if failure == "allocate":
        def fail_mkdtemp(*_args, **_kwargs):
            raise OSError("scratch filesystem unavailable")

        monkeypatch.setattr(arbiter.tempfile, "mkdtemp", fail_mkdtemp)
    else:
        def fail_rmdir(_self):
            raise OSError("cannot clear scratch directory")

        monkeypatch.setattr(arbiter.Path, "rmdir", fail_rmdir)

    result = arbiter.readiness_check(repo, "true", timeout_s=5)[0]

    assert not result.ok
    assert "scratch" in result.output
    listing = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=repo,
                             capture_output=True, text=True, check=True).stdout
    assert "nc-readiness-" not in listing


@pytest.mark.parametrize("mode", ["failure", "timeout"])
def test_readiness_removes_only_its_registration_when_worktree_remove_fails(tmp_path, monkeypatch, mode):
    repo = _repo(tmp_path)
    other = tmp_path / "unrelated-task"
    subprocess.run(["git", "worktree", "add", "-b", "nc/unrelated", str(other)],
                   cwd=repo, check=True, capture_output=True)
    original = arbiter.subprocess.Popen

    def broken_remove(args, *args_, **kwargs):
        if isinstance(args, list) and args[1:3] == ["worktree", "remove"]:
            if mode == "failure":
                return original(["false"], *args_, **kwargs)
            return original(["sleep", "60"], *args_, **kwargs)
        return original(args, *args_, **kwargs)

    monkeypatch.setattr(arbiter.subprocess, "Popen", broken_remove)
    monkeypatch.setattr(arbiter, "CLEANUP_TIMEOUT_S", 0.01)
    assert arbiter.readiness_check(repo, "true", timeout_s=5)[0].ok

    assert not list(repo.parent.glob("nc-readiness-*"))
    listing = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=repo,
                             capture_output=True, text=True, check=True).stdout
    assert "nc-readiness-" not in listing
    assert f"worktree {other}" in listing


def test_readiness_fails_when_remove_and_scoped_registration_cleanup_fail(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    original_popen = arbiter.subprocess.Popen
    original_rmtree = arbiter.shutil.rmtree

    def broken_remove(args, *args_, **kwargs):
        if isinstance(args, list) and args[1:3] == ["worktree", "remove"]:
            return original_popen(["false"], *args_, **kwargs)
        return original_popen(args, *args_, **kwargs)

    def fail_registration_remove(path, *args, **kwargs):
        if "worktrees" in Path(path).parts:
            raise OSError("registration cannot be removed")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(arbiter.subprocess, "Popen", broken_remove)
    monkeypatch.setattr(arbiter.shutil, "rmtree", fail_registration_remove)

    results = arbiter.readiness_check(repo, "true", timeout_s=5)

    assert results[0].ok
    assert not results[-1].ok
    assert results[-1].command == "readiness scratch cleanup"
    assert "cleanup failed" in results[-1].output


def test_readiness_verifies_nc_import_is_from_disposable_worktree(tmp_path):
    repo = _repo(tmp_path)
    (repo / "nc").mkdir()
    (repo / "nc" / "__init__.py").write_text("VALUE = 'scratch'\n")
    subprocess.run(["git", "add", "nc/__init__.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "package"], cwd=repo, check=True)

    results = arbiter.readiness_check(repo, "test -f nc/__init__.py", timeout_s=5,
                                      python=sys.executable)

    assert [(result.command, result.ok) for result in results] == [
        ("test -f nc/__init__.py", True), ("worktree-local import nc", True),
    ]


def test_host_requirements_uses_service_path_and_reports_missing_and_wrong_python(tmp_path,
                                                                                  monkeypatch):
    service = tmp_path / "service-bin"
    login = tmp_path / "login-bin"
    service.mkdir()
    login.mkdir()
    for name in ("python", "git", "sqlite3", "pytest", "ruff", "adapter"):
        path = service / name
        version = "Python 3.12.9" if name == "python" else "ok"
        path.write_text(f"#!/bin/sh\necho '{version}'\n")
        path.chmod(0o755)
    monkeypatch.setattr(arbiter, "SERVICE_PATH", str(service))
    monkeypatch.setenv("PATH", str(login))

    reports, errors, python = arbiter.host_requirements({"adapter", "missing-adapter"})

    assert python == str(service / "python")
    assert f"python: {service / 'python'}" in reports
    assert f"adapter: {service / 'adapter'}" in reports
    assert "missing missing-adapter on service PATH" in errors
    assert "python must be Python 3.13 (found Python 3.12.9)" in errors


def test_host_requirements_reports_missing_python_launcher_and_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(arbiter, "SERVICE_PATH", str(tmp_path))

    reports, errors, python = arbiter.host_requirements({"adapter"})

    assert python is None
    assert reports == [f"service PATH: {tmp_path}"]
    for name in ("python", "git", "sqlite3", "pytest", "ruff", "adapter"):
        assert f"missing {name} on service PATH" in errors
