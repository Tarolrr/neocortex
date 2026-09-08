"""Independent, coalescing durable backup worker.

It is intentionally invoked by a systemd timer, not the queue: a storage
outage must never delay an accepted task or owner operation.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from .snapshot import SnapshotError, backup

LOG = logging.getLogger(__name__)
FALLBACK_SECONDS = 15 * 60
STALE_SECONDS = 30 * 60


class BackupConfigurationError(ValueError):
    pass


def validate_destination(home: Path, destination: Path | None) -> Path:
    """Refuse implicit directories and the source device for automation."""
    if destination is None:
        raise BackupConfigurationError("automated backup destination is not configured")
    destination = Path(destination)
    if not destination.is_dir():
        raise BackupConfigurationError(
            f"backup destination is not a mounted directory: {destination}"
        )
    if destination.is_symlink():
        raise BackupConfigurationError("backup destination must not be a symlink")
    if home.stat().st_dev == destination.stat().st_dev:
        raise BackupConfigurationError("backup destination is on the same device as NC_HOME")
    return destination


def _connect(home: Path) -> sqlite3.Connection:
    db = sqlite3.connect(home / "state.db", timeout=5)
    db.row_factory = sqlite3.Row
    return db


def run_once(home: Path, destination: Path | None, *, retain: int = 96,
             now: float | None = None) -> bool:
    """Publish one incremental snapshot if dirty or the recovery-point is due.

    Returns whether a snapshot was attempted.  The generation is read before
    staging; a later event remains pending after success.
    """
    home = Path(home); now = time.time() if now is None else now
    try:
        destination = validate_destination(home, destination)
        db = _connect(home)
    except (OSError, sqlite3.Error, BackupConfigurationError) as exc:
        # Journal is the only dependable diagnostics channel when state.db is
        # absent or unreadable.
        LOG.error("backup unavailable: %s", exc)
        return False
    try:
        try:
            row = db.execute("SELECT * FROM backup_state WHERE id=1").fetchone()
        except sqlite3.Error as exc:
            LOG.error("backup state unavailable: %s", exc); return False
        if row is None:
            LOG.error("backup state unavailable: migration has not completed"); return False
        generation = int(row["dirty_generation"])
        due = (row["last_success_at"] is None
               or now - float(row["last_success_at"]) >= FALLBACK_SECONDS)
        if generation <= int(row["acknowledged_generation"]) and not due:
            return False
        db.execute("UPDATE backup_state SET last_attempt_at=?, last_error=NULL WHERE id=1", (now,))
        db.commit()
        try:
            backup(home, destination, incremental=True, retain=retain)
        except (SnapshotError, OSError, sqlite3.Error) as exc:
            # Failure is durable if possible, but never changes foreground state.
            LOG.error("backup failed: %s", exc)
            try:
                db.execute("UPDATE backup_state SET last_error=? WHERE id=1", (str(exc),))
                db.commit()
            except sqlite3.Error:
                LOG.exception("could not record backup failure")
            return True
        # Do not acknowledge a generation that appeared while backup() staged.
        db.execute(
            "UPDATE backup_state SET acknowledged_generation=?, last_success_at=?, last_error=NULL "
            "WHERE id=1 AND acknowledged_generation < ?", (generation, now, generation),
        )
        db.commit()
        return True
    finally:
        db.close()


def status(home: Path, destination: Path | None, *, now: float | None = None) -> tuple[
        dict[str, object], bool]:
    now = time.time() if now is None else now
    result: dict[str, object] = {"destination": str(destination) if destination else "unknown"}
    try:
        validate_destination(Path(home), destination)
    except (OSError, BackupConfigurationError) as exc:
        result.update({"health": "unhealthy", "error": str(exc)})
        return result, False
    try:
        db = _connect(Path(home))
        row = db.execute("SELECT * FROM backup_state WHERE id=1").fetchone(); db.close()
        if row is None: raise sqlite3.Error("backup state unavailable")
    except (OSError, sqlite3.Error) as exc:
        result.update({"health": "unhealthy", "error": f"state unavailable: {exc}"})
        return result, False
    success = row["last_success_at"]
    age = None if success is None else max(0.0, now - float(success))
    pending = int(row["dirty_generation"]) > int(row["acknowledged_generation"])
    healthy = (success is not None and age is not None and age <= STALE_SECONDS
               and not row["last_error"])
    result.update({"last_attempt": row["last_attempt_at"], "last_error": row["last_error"],
                   "last_verified_success": success, "age_seconds": age,
                   "covered_generation": int(row["acknowledged_generation"]),
                   "pending_generation": int(row["dirty_generation"]), "pending": pending,
                   "health": "healthy" if healthy else "unhealthy"})
    return result, healthy
