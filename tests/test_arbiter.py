from __future__ import annotations

import subprocess

import pytest

from nc import arbiter


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
