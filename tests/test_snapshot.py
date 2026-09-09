import errno
import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import nc.snapshot as snapshot_module
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


def test_restore_uses_only_manifest_payload_and_preserves_checked_config(tmp_path):
    home = tmp_path / "home"
    state = State(home / "state.db")
    (home / "config.json").write_text('{"safe": true}')
    state.db.close()
    snapshot = tmp_path / "snapshot"
    backup(home, snapshot)
    restore(snapshot, tmp_path / "restored")
    assert (tmp_path / "restored" / "config.json").read_text() == '{"safe": true}'

    # An unlisted config must not be treated as an optional restore input.
    bare = tmp_path / "bare-home"
    State(bare / "state.db").db.close()
    bare_snapshot = tmp_path / "bare-snapshot"
    backup(bare, bare_snapshot)
    (bare_snapshot / "config.json").write_text('{"injected": true}')
    with pytest.raises(SnapshotError, match="unsupported payload"):
        restore(bare_snapshot, tmp_path / "injected-home")


def test_restore_rechecks_staged_bytes_before_publish(tmp_path, monkeypatch):
    home = tmp_path / "home"
    State(home / "state.db").db.close()
    snapshot = tmp_path / "snapshot"
    backup(home, snapshot)
    original_copyfile = snapshot_module.shutil.copyfile

    def corrupt_staged_database(source, target, *args, **kwargs):
        result = original_copyfile(source, target, *args, **kwargs)
        if str(source).endswith("state.db") and ".restored.tmp-" in str(target):
            with open(target, "ab") as stream:
                stream.write(b"changed after source validation")
        return result

    monkeypatch.setattr(snapshot_module.shutil, "copyfile", corrupt_staged_database)
    restored = tmp_path / "restored"
    with pytest.raises(SnapshotError):
        restore(snapshot, restored)
    assert not restored.exists()


def test_round_trip_preserves_all_state_tables_and_ids(tmp_path):
    home = tmp_path / "home"
    state = State(home / "state.db")
    state.add_project("p", "Project", "/repo", "pytest")
    first = state.add_task("p", "first", "do first", ["ok"])
    second = state.add_task("p", "second", "do second", ["ok"], depends_on=[first])
    state.add_agent("worker-p", "worker", "p", second, "model")
    now = 1.0
    message_id = state.db.execute(
        "INSERT INTO message(kind,sender,recipient,task_id,payload,created_at) VALUES(?,?,?,?,?,?)",
        ("feedback", "owner", "worker-p", second, "{}", now),
    ).lastrowid
    proposal_id = state.db.execute(
        "INSERT INTO proposal(project_id,source,rationale,spec,status,created_at,findings) "
        "VALUES(?,?,?,?,?,?,?)", ("p", "worker-p", "why", "[]", "pending", now, "[]"),
    ).lastrowid
    state.db.execute(
        "INSERT INTO plan_review(proposal_id,spec,status,findings,recommendation) VALUES(?,?,?,?,?)",
        (proposal_id, "[]", "done", "[]", "ship"),
    )
    state.db.execute(
        "INSERT INTO proposal_revision(original_id,feedback_id,planner_id) VALUES(?,?,?)",
        (proposal_id, message_id, "worker-p"),
    )
    state.db.execute(
        "INSERT INTO run(agent_id,task_id,role,model,outcome,detail,started_at,ended_at) "
        "VALUES(?,?,?,?,?,?,?,?)", ("worker-p", second, "worker", "model", "DONE", "ok", now, now)),
    state.db.execute("INSERT INTO incident(kind,detail,created_at) VALUES(?,?,?)", ("test", "detail", now))
    state.db.commit()
    before = {
        table: [tuple(row) for row in state.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in ("project", "task", "task_seq", "agent", "message", "proposal", "plan_review",
                      "proposal_revision", "run", "incident")
    }
    state.db.close()
    snapshot = tmp_path / "snapshot"
    backup(home, snapshot)
    restored = tmp_path / "restored"
    restore(snapshot, restored)
    db = sqlite3.connect(restored / "state.db")
    after = {
        table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in before
    }
    db.close()
    assert after == before


def test_backup_rejects_overwrite_and_interrupted_publication(tmp_path, monkeypatch):
    home = tmp_path / "home"
    State(home / "state.db").db.close()
    destination = tmp_path / "snapshot"
    destination.mkdir()
    with pytest.raises(SnapshotError, match="already exists"):
        backup(home, destination)
    destination.rmdir()

    def interrupted(*_args, **_kwargs):
        raise OSError("simulated publication interruption")

    monkeypatch.setattr(snapshot_module, "_atomic_publish", interrupted)
    with pytest.raises(OSError, match="interruption"):
        backup(home, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".snapshot.tmp-*"))


def test_backup_timeout_does_not_publish_snapshot_or_staging(tmp_path, monkeypatch):
    home = tmp_path / "home"
    State(home / "state.db").db.close()
    destination = tmp_path / "snapshot"
    real_connect = snapshot_module.sqlite3.connect

    class BusySource:
        def backup(self, _target, *, pages, progress, sleep):
            # Simulate a SQLite backup which remains busy until its deadline.
            assert pages == 64
            assert sleep == 0.05
            progress(sqlite3.SQLITE_BUSY, 1, 1)

        def close(self):
            pass

    def connect_busy_source(database, *args, **kwargs):
        if str(database).endswith("state.db?mode=ro"):
            return BusySource()
        return real_connect(database, *args, **kwargs)

    timestamps = iter((0.0, snapshot_module.BACKUP_TIMEOUT_S + 0.01))
    monkeypatch.setattr(snapshot_module.sqlite3, "connect", connect_busy_source)
    monkeypatch.setattr(snapshot_module.time, "monotonic", lambda: next(timestamps))
    with pytest.raises(SnapshotError, match="SQLite backup timed out"):
        backup(home, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".snapshot.tmp-*"))


def test_restore_rejects_corrupt_and_incompatible_metadata(tmp_path):
    home = tmp_path / "home"
    State(home / "state.db").db.close()
    snapshot = tmp_path / "snapshot"
    backup(home, snapshot)
    with open(snapshot / "state.db", "ab") as stream:
        stream.write(b"corruption")
    with pytest.raises(SnapshotError, match="checksum"):
        restore(snapshot, tmp_path / "corrupt")

    backup(home, snapshot := tmp_path / "incompatible")
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["application_version"] = "not-this-version"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(SnapshotError, match="application version"):
        restore(snapshot, tmp_path / "incompatible-home")


@pytest.mark.parametrize(
    ("field", "value"),
    [("format_version", True), ("sqlite_user_version", False)],
)
def test_restore_rejects_boolean_manifest_version_metadata(tmp_path, field, value):
    home = tmp_path / "home"
    State(home / "state.db").db.close()
    snapshot = tmp_path / "snapshot"
    backup(home, snapshot)
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(SnapshotError, match="manifest types"):
        restore(snapshot, tmp_path / "restored")


def test_incremental_snapshots_reuse_blocks_and_restore_identical_rows(tmp_path):
    home = tmp_path / "home"
    state = State(home / "state.db")
    state.db.execute(
        "INSERT INTO incident(kind, detail, created_at) VALUES(?,?,?)",
        ("large", "x" * (3 * 1024 * 1024), 1.0),
    )
    state.db.commit()
    before = [tuple(row) for row in state.db.execute("SELECT * FROM incident ORDER BY id")]
    store = tmp_path / "store"
    first = backup(home, store, incremental=True)
    state.db.execute(
        "INSERT INTO incident(kind, detail, created_at) VALUES(?,?,?)", ("small", "changed", 2.0)
    )
    state.db.commit()
    second = backup(home, store, incremental=True)
    state.db.close()

    first_manifest = json.loads((first / "manifest.json").read_text())
    second_manifest = json.loads((second / "manifest.json").read_text())
    assert first_manifest["new_bytes"] == first_manifest["logical_bytes"]
    assert 0 < second_manifest["reused_bytes"] < second_manifest["logical_bytes"]
    assert second_manifest["new_bytes"] + second_manifest["reused_bytes"] == second_manifest["logical_bytes"]

    restored = tmp_path / "restored"
    restore(second, restored)
    db = sqlite3.connect(restored / "state.db")
    assert [tuple(row) for row in db.execute("SELECT * FROM incident ORDER BY id")][:-1] == before
    assert db.execute("SELECT detail FROM incident WHERE kind='small'").fetchone()[0] == "changed"
    db.close()


def test_unchanged_incremental_snapshot_writes_no_blocks(tmp_path):
    home = tmp_path / "home"
    state = State(home / "state.db")
    state.db.close()
    store = tmp_path / "store"
    first = backup(home, store, incremental=True)
    block_count = len(list((store / "blocks").iterdir()))
    second = backup(home, store, incremental=True)

    first_manifest = json.loads((first / "manifest.json").read_text())
    second_manifest = json.loads((second / "manifest.json").read_text())
    assert len(list((store / "blocks").iterdir())) == block_count
    assert second_manifest["new_bytes"] == 0
    assert second_manifest["reused_bytes"] == second_manifest["logical_bytes"]
    assert first_manifest["logical_bytes"] == second_manifest["logical_bytes"]


def test_incremental_restore_rejects_missing_or_corrupt_shared_block(tmp_path):
    home = tmp_path / "home"
    State(home / "state.db").db.close()
    snapshot = backup(home, tmp_path / "store", incremental=True)
    manifest = json.loads((snapshot / "manifest.json").read_text())
    block = next(iter(manifest["files"]["state.db"]["blocks"]))["sha256"]
    path = tmp_path / "store" / "blocks" / block
    path.unlink()
    with pytest.raises(SnapshotError, match="shared block"):
        restore(snapshot, tmp_path / "missing")

    snapshot = backup(home, tmp_path / "store", incremental=True)
    manifest = json.loads((snapshot / "manifest.json").read_text())
    path = tmp_path / "store" / "blocks" / manifest["files"]["state.db"]["blocks"][0]["sha256"]
    path.write_bytes(b"corrupt")
    with pytest.raises(SnapshotError, match="shared block"):
        restore(snapshot, tmp_path / "corrupt")


def test_incremental_failure_before_manifest_does_not_prune_snapshot(tmp_path, monkeypatch):
    home = tmp_path / "home"
    State(home / "state.db").db.close()
    store = tmp_path / "store"
    previous = backup(home, store, incremental=True, retain=1)

    def interrupted(*_args, **_kwargs):
        raise OSError("simulated full disk")

    monkeypatch.setattr(snapshot_module, "_atomic_manifest", interrupted)
    with pytest.raises(OSError, match="full disk"):
        backup(home, store, incremental=True, retain=1)
    assert (previous / "manifest.json").exists()
    assert restore(previous, tmp_path / "restored")


def test_incremental_full_disk_while_writing_block_keeps_previous_snapshot(tmp_path, monkeypatch):
    home = tmp_path / "home"
    state = State(home / "state.db")
    (home / "config.json").write_text('{"version": 1}')
    store = tmp_path / "store"
    previous = backup(home, store, incremental=True, retain=1)
    (home / "config.json").write_text('{"version": 2}')

    real_fsync = snapshot_module.os.fsync

    def full_disk(fd):
        if "/blocks/.block-" in os.readlink(f"/proc/self/fd/{fd}"):
            raise OSError(errno.ENOSPC, "simulated full disk")
        return real_fsync(fd)

    monkeypatch.setattr(snapshot_module.os, "fsync", full_disk)
    with pytest.raises(OSError, match="full disk"):
        backup(home, store, incremental=True, retain=1)

    assert (previous / "manifest.json").exists()
    assert len(list((store / "snapshots" / snapshot_module._source_id(home)).iterdir())) == 1
    assert restore(previous, tmp_path / "restored")
    assert not list((store / "blocks").glob(".block-*"))
    state.db.close()


def test_incremental_pruning_keeps_blocks_referenced_by_other_source(tmp_path):
    home_a = tmp_path / "home-a"
    home_b = tmp_path / "home-b"
    state_a = State(home_a / "state.db")
    state_b = State(home_b / "state.db")
    state_a.db.close()
    state_b.db.close()
    store = tmp_path / "store"

    (home_a / "config.json").write_text('{"value": "unique"}')
    first = backup(home_a, store, incremental=True, retain=1)
    unique = json.loads((first / "manifest.json").read_text())["files"]["config.json"]["blocks"][0]["sha256"]
    (home_a / "config.json").write_text('{"value": "shared"}')
    second = backup(home_a, store, incremental=True, retain=1)
    assert not (store / "blocks" / unique).exists()

    (home_b / "config.json").write_text('{"value": "shared"}')
    other = backup(home_b, store, incremental=True, retain=1)
    shared = json.loads((second / "manifest.json").read_text())["files"]["config.json"]["blocks"][0]["sha256"]
    assert shared == json.loads((other / "manifest.json").read_text())["files"]["config.json"]["blocks"][0]["sha256"]
    (home_a / "config.json").write_text('{"value": "new"}')
    backup(home_a, store, incremental=True, retain=1)

    assert (store / "blocks" / shared).is_file()
    assert len(list((store / "snapshots" / snapshot_module._source_id(home_a)).iterdir())) == 1
    assert restore(other, tmp_path / "restored-other")


def test_incremental_concurrent_backup_and_retention_are_serialized(tmp_path):
    home = tmp_path / "home"
    state = State(home / "state.db")
    state.db.close()
    store = tmp_path / "store"
    barrier = threading.Barrier(2)

    def take_backup():
        barrier.wait()
        return backup(home, store, incremental=True, retain=1)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _item: take_backup(), range(2)))

    assert len(results) == 2
    snapshots = store / "snapshots" / snapshot_module._source_id(home)
    complete = [path for path in snapshots.iterdir() if (path / "manifest.json").is_file()]
    assert len(complete) == 1
    assert restore(complete[0], tmp_path / "restored")
