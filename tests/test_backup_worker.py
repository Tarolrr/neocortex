import json
from pathlib import Path

import pytest

from nc.backup_worker import (
    FALLBACK_SECONDS,
    STALE_SECONDS,
    BackupConfigurationError,
    run_once,
    status,
    validate_destination,
)
from nc.cli import main
from nc.snapshot import backup, restore
from nc.state import State


def _state(tmp_path):
    home = tmp_path / "home"
    state = State(home / "state.db")
    state.add_project("p", "P", "/tmp", None)
    return home, state


def test_feedback_marks_generation_and_rollback_does_not(tmp_path):
    _home, state = _state(tmp_path)
    assert state.one("SELECT dirty_generation FROM backup_state")[0] == 0
    state.planner_feedback("p", "save this", "model")
    assert state.one("SELECT dirty_generation FROM backup_state")[0] == 1
    with pytest.raises(RuntimeError), state.db:
        state._backup_dirty()
        raise RuntimeError("abort")
    assert state.one("SELECT dirty_generation FROM backup_state")[0] == 1


def test_same_device_is_refused_and_status_is_unhealthy(tmp_path):
    home, state = _state(tmp_path)
    with pytest.raises(BackupConfigurationError):
        validate_destination(home, home)
    # A mount/configuration failure is a durable attempted backup when state
    # is available, rather than merely a journal-only diagnostic.
    assert not run_once(home, home, now=12)
    row = state.one("SELECT last_attempt_at, last_error FROM backup_state")
    assert row["last_attempt_at"] == 12
    assert "outside NC_HOME" in row["last_error"]
    result, healthy = status(home, home)
    assert not healthy
    assert result["health"] == "unhealthy"
    assert result["last_attempt"] == 12
    assert result["covered_generation"] == 0
    assert result["pending_generation"] == 0


def test_different_device_mount_nested_in_home_is_refused(tmp_path, monkeypatch):
    home, _ = _state(tmp_path)
    nested_mount = home / "mounted-backup"
    nested_mount.mkdir()
    # Model a separately mounted directory: containment must reject it before
    # the device guard can make this otherwise unsafe configuration look valid.
    monkeypatch.setattr("nc.backup_worker._device", lambda path: 1 if path == home else 2)
    with pytest.raises(BackupConfigurationError, match="outside NC_HOME"):
        validate_destination(home, nested_mount)


def test_missing_mount_and_stale_status_are_explicit(tmp_path, monkeypatch):
    home, state = _state(tmp_path)
    missing = tmp_path / "not-mounted"
    assert not run_once(home, missing, now=20)
    row = state.one("SELECT last_attempt_at, last_error FROM backup_state")
    assert row["last_attempt_at"] == 20
    assert "mounted directory" in row["last_error"]
    result, healthy = status(home, missing, now=21)
    assert not healthy
    assert result["last_verified_success"] is None
    assert result["covered_generation"] == 0
    assert result["pending_generation"] == 0
    destination = tmp_path / "destination"; destination.mkdir()
    monkeypatch.setattr("nc.backup_worker.validate_destination", lambda _h, d: d)
    with state.db:
        state.db.execute("UPDATE backup_state SET last_success_at=?, last_error=NULL", (1,))
    result, healthy = status(home, destination, now=1 + STALE_SECONDS + 1)
    assert not healthy
    assert result["error"] == "backup is stale"


def test_success_acknowledges_snapshot_generation_not_later_event(tmp_path, monkeypatch):
    home, state = _state(tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir()
    # The real device rule is covered above; isolate coalescing mechanics here.
    monkeypatch.setattr("nc.backup_worker.validate_destination", lambda _h, d: d)
    state.planner_feedback("p", "first", "model")

    def snapshot(_home, _destination, **_kwargs):
        state.planner_feedback("p", "during snapshot", "model")

    monkeypatch.setattr("nc.backup_worker.backup", snapshot)
    assert run_once(home, destination)
    row = state.one("SELECT dirty_generation, acknowledged_generation FROM backup_state")
    assert tuple(row) == (2, 1)


def test_burst_coalesces_and_fallback_runs_without_new_events(tmp_path, monkeypatch):
    home, state = _state(tmp_path)
    destination = tmp_path / "destination"; destination.mkdir()
    monkeypatch.setattr("nc.backup_worker.validate_destination", lambda _h, d: d)
    calls = []
    monkeypatch.setattr("nc.backup_worker.backup", lambda *_a, **_k: calls.append(1))
    for number in range(5):
        state.planner_feedback("p", f"burst {number}", "model")
    assert run_once(home, destination, now=100)
    assert calls == [1]
    row = state.one("SELECT dirty_generation, acknowledged_generation FROM backup_state")
    assert tuple(row) == (5, 5)
    assert not run_once(home, destination, now=101)
    assert run_once(home, destination, now=100 + FALLBACK_SECONDS)
    assert calls == [1, 1]


def test_storage_failure_retries_without_rolling_back_queue_work(tmp_path, monkeypatch):
    home, state = _state(tmp_path)
    destination = tmp_path / "destination"; destination.mkdir()
    monkeypatch.setattr("nc.backup_worker.validate_destination", lambda _h, d: d)
    state.planner_feedback("p", "queue keeps moving", "model")
    monkeypatch.setattr("nc.backup_worker.backup", lambda *_a, **_k: (_ for _ in ()).throw(
        OSError("disk offline")))
    assert run_once(home, destination, now=50)
    row = state.one("SELECT dirty_generation, acknowledged_generation, last_error FROM backup_state")
    assert tuple(row[:2]) == (1, 0)
    assert "disk offline" in row["last_error"]
    assert state.one("SELECT count(*) FROM message")[0] == 1
    monkeypatch.setattr("nc.backup_worker.backup", lambda *_a, **_k: None)
    assert run_once(home, destination, now=51)
    row = state.one("SELECT dirty_generation, acknowledged_generation, last_error FROM backup_state")
    assert tuple(row) == (1, 1, None)


def test_crash_after_publication_retries_unacknowledged_generation(tmp_path, monkeypatch):
    home, state = _state(tmp_path)
    destination = tmp_path / "destination"; destination.mkdir()
    monkeypatch.setattr("nc.backup_worker.validate_destination", lambda _h, d: d)
    state.planner_feedback("p", "durable before crash", "model")
    # SystemExit is intentionally not a storage failure caught by the worker:
    # it models a process death after publication and before acknowledgement.
    monkeypatch.setattr("nc.backup_worker.backup", lambda *_a, **_k: (_ for _ in ()).throw(
        SystemExit("crash after publish")))
    with pytest.raises(SystemExit):
        run_once(home, destination, now=50)
    assert tuple(state.one("SELECT dirty_generation, acknowledged_generation FROM backup_state")) == (1, 0)
    monkeypatch.setattr("nc.backup_worker.backup", lambda *_a, **_k: None)
    assert run_once(home, destination, now=51)
    assert tuple(state.one("SELECT dirty_generation, acknowledged_generation FROM backup_state")) == (1, 1)


def test_unavailable_state_is_journalled_and_backup_status_exits_unhealthy(tmp_path, caplog, capsys):
    home = tmp_path / "missing-home"
    assert not run_once(home, None)
    assert "backup unavailable" in caplog.text
    assert main(["--home", str(home), "backup-status"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["health"] == "unhealthy"
    assert "state unavailable" in report["error"]


def test_restore_drill_preserves_feedback_and_proposal_revision_lineage(tmp_path):
    home, state = _state(tmp_path)
    specs = [{"project": "p", "title": "task", "objective": "do it", "acceptance": ["$ true"]}]
    original = state.add_proposal("p", "planner", "initial", specs)
    planner, _message = state.planner_feedback(
        None, "please revise", "owner", proposal_id=original
    )
    replacement = state.add_proposal("p", planner, "revised", specs, revision_id=original)
    snapshot = tmp_path / "snapshot"
    backup(home, snapshot)
    restored_home = restore(snapshot, tmp_path / "restored")
    restored = State(restored_home / "state.db", initialize=False)
    try:
        assert restored.one("SELECT count(*) FROM message WHERE kind='feedback'")[0] == 1
        row = restored.one("SELECT original_id, replacement_id FROM proposal_revision")
        assert tuple(row) == (original, replacement)
    finally:
        restored.db.close()


def test_timer_configuration_has_no_more_than_sixty_second_pickup_window():
    timer = (Path(__file__).parents[1] / "deploy" / "neocortex-backup.timer").read_text()
    assert "OnCalendar=*-*-* *:*:00" in timer
    assert "AccuracySec=1us" in timer


def test_all_shared_owner_and_task_event_paths_mark_generation(tmp_path):
    _home, state = _state(tmp_path)
    spec = [{"project": "p", "title": "task", "objective": "do it",
             "acceptance": ["$ true"]}]
    task = state.add_task("p", "direct", "do it", ["done"])
    state.set_task(task, status="done")
    state.set_task(task, merge_commit="abc")
    state.planner_feedback("p", "owner feedback", "model")
    approved = state.add_proposal("p", "planner", "", spec)
    state.approve_proposal(approved)
    rejected = state.add_proposal("p", "planner", "", spec)
    state.reject_proposal(rejected, "no")
    original = state.add_proposal("p", "planner", "", spec)
    state.planner_feedback(None, "revise", "model", proposal_id=original)
    state.add_proposal("p", "planner-p", "", spec, revision_id=original)
    assert state.one("SELECT dirty_generation FROM backup_state")[0] == 10
