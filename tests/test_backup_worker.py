from pathlib import Path

import pytest

from nc.backup_worker import BackupConfigurationError, run_once, status, validate_destination
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
    with pytest.raises(RuntimeError):
        with state.db:
            state._backup_dirty()
            raise RuntimeError("abort")
    assert state.one("SELECT dirty_generation FROM backup_state")[0] == 1


def test_same_device_is_refused_and_status_is_unhealthy(tmp_path):
    home, _state = _state(tmp_path)
    with pytest.raises(BackupConfigurationError):
        validate_destination(home, home)
    result, healthy = status(home, home)
    assert not healthy
    assert result["health"] == "unhealthy"


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
