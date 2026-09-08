"""Serialize scheduler turns and owner task lifecycle changes within one home.

The lock covers worktree setup through outcome integration, including the gap
between recording a finished run and applying its outcome. It never holds a
SQLite transaction while an agent runs. Busy owners can retry after the turn.
"""

import fcntl
from contextlib import contextmanager
from pathlib import Path

from . import arbiter


class LifecycleBusy(ValueError):
    pass


@contextmanager
def lifecycle_lock(state):
    with state.path.resolve().with_suffix(".lifecycle.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LifecycleBusy("Task lifecycle is busy; retry after the current turn.") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def repository_identity(repo):
    """Worktrees and symlink aliases share their canonical Git common directory."""
    common = Path(arbiter.git(repo, "rev-parse", "--git-common-dir"))
    return (common if common.is_absolute() else repo / common).resolve()


@contextmanager
def repository_lock(repo):
    """Acquire after lifecycle_lock and before SQLite write transactions.

    Nonblocking contention requires explicit retry. Never unlink the lock file:
    kernel ownership releases on exceptions and process exit. The descriptor is
    not inherited by executed agents. Callers cover Git AND dependent state.
    """
    with (repository_identity(repo) / "nc-repository.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LifecycleBusy("Repository is busy; retry after the current operation.") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
