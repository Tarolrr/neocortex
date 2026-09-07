"""Process-level lock identity, contention and crash recovery."""
import multiprocessing
import subprocess

import pytest

from nc.lifecycle import LifecycleBusy, repository_identity, repository_lock


def hold_repository(repo, ready, release):
    with repository_lock(repo):
        ready.set()
        release.wait(10)


def test_repository_alias_contention_and_process_exit(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    alias = tmp_path / 'alias'
    alias.symlink_to(repo, target_is_directory=True)
    assert repository_identity(repo) == repository_identity(alias)
    ctx = multiprocessing.get_context('spawn')
    ready, release = ctx.Event(), ctx.Event()
    process = ctx.Process(target=hold_repository, args=(repo, ready, release))
    process.start()
    try:
        assert ready.wait(10)
        with pytest.raises(LifecycleBusy, match='retry'), repository_lock(alias):
            pytest.fail('overlapping ownership')
        process.terminate()
        process.join(10)
        assert not process.is_alive()
        with repository_lock(alias):
            pass
    finally:
        if process.is_alive():
            process.terminate()
        process.join(10)


def test_repository_exception_releases(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    with pytest.raises(RuntimeError), repository_lock(tmp_path):
        raise RuntimeError('turn failed')
    with repository_lock(tmp_path):
        pass
