"""Serialize scheduler turns and owner task lifecycle changes within one home.

The lock covers worktree setup through outcome integration, including the gap
between recording a finished run and applying its outcome. It never holds a
SQLite transaction while an agent runs. Busy owners can retry after the turn.
"""

import fcntl
from contextlib import contextmanager


class LifecycleBusy(ValueError):
    pass


@contextmanager
def lifecycle_lock(state):
    with state.path.with_suffix(".lifecycle.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LifecycleBusy("Task lifecycle is busy; retry after the current turn.") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
