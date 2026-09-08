import json
import os
import sqlite3

import pytest

from nc.snapshot import SnapshotError, backup, restore
from nc.state import State


def test_backup_restore_includes_wal_and_starts_stopped(tmp_path):
    home = tmp_path / "home"
    state = State(home / "state.db")
    state.add_project("p", "P", "/repo", None)
    # A separate, still-open WAL writer represents the scheduler/UI case.
    writer = sqlite3.connect(home / "state.db")
    writer.execute("INSERT INTO task_seq VALUES(?,?)", ("p", 42))
    writer.commit()
    snapshot = tmp_path / "snapshot"
    backup(home, snapshot)
    writer.close()
    state.db.close()

    restored = tmp_path / "restored"
    restore(snapshot, restored)
    check = sqlite3.connect(restored / "state.db")
    assert check.execute("SELECT last FROM task_seq WHERE project_id='p'").fetchone()[0] == 42
    check.close()
    assert (restored / "STOP").exists()
    assert oct(os.stat(snapshot).st_mode & 0o777) == "0o700"


def test_restore_rejects_tampering_and_nonempty_home(tmp_path):
    home = tmp_path / "home"
    State(home / "state.db").db.close()
    snapshot = tmp_path / "snapshot"
    backup(home, snapshot)
    (snapshot / "manifest.json").write_text(json.dumps({"files": {"../evil": "x"}}))
    with pytest.raises(SnapshotError):
        restore(snapshot, tmp_path / "new-home")

    backup(home, snapshot := tmp_path / "snapshot2")
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep").write_text("keep")
    with pytest.raises(SnapshotError):
        restore(snapshot, occupied)


def test_missing_source_does_not_create_database(tmp_path):
    missing_home = tmp_path / "missing"
    with pytest.raises(SnapshotError):
        backup(missing_home, tmp_path / "snapshot")
    assert not missing_home.exists()
