"""Verified, portable SQLite snapshots.

Snapshots intentionally contain only the Neocortex database and optional
configuration.  Repositories, worktrees, logs and running processes are not a
transactional part of this format.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from . import __version__

FORMAT = "neocortex-snapshot"
FORMAT_VERSION = 1
MANIFEST = "manifest.json"
DATABASE = "state.db"
CONFIG = "config.json"
BACKUP_TIMEOUT_S = 5.0


class SnapshotError(ValueError):
    """A snapshot is absent, unsafe, corrupt, or incompatible."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _schema_metadata(database: Path) -> tuple[int, str]:
    """Return metadata without initializing or changing an existing database."""
    try:
        # Snapshot databases are immutable while being checked.  Besides being
        # the right access mode, this prevents SQLite from creating -wal/-shm
        # sidecars inside the staged snapshot directory.
        db = sqlite3.connect(
            f"file:{database}?mode=ro&immutable=1", uri=True, timeout=BACKUP_TIMEOUT_S
        )
        try:
            check = db.execute("PRAGMA integrity_check").fetchone()
            if not check or check[0] != "ok":
                raise SnapshotError("SQLite integrity_check failed")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            rows = [tuple(row) for row in db.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE sql IS NOT NULL ORDER BY type, name, tbl_name"
            )]
        finally:
            db.close()
    except sqlite3.Error as exc:
        raise SnapshotError(f"invalid SQLite database: {exc}") from exc
    encoded = json.dumps(rows, ensure_ascii=True, separators=(",", ":")).encode()
    return version, hashlib.sha256(encoded).hexdigest()


def _temporary_sibling(destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=destination.parent))


def _atomic_publish(temp: Path, destination: Path, *, allow_empty_destination: bool = False) -> None:
    if destination.exists():
        if not allow_empty_destination or not destination.is_dir() or any(destination.iterdir()):
            raise SnapshotError(f"destination already exists: {destination}")
        destination.rmdir()
    os.replace(temp, destination)


def _manifest(database: Path, config: Path | None) -> dict[str, object]:
    user_version, schema_sha256 = _schema_metadata(database)
    files = {DATABASE: _sha256(database)}
    if config is not None:
        files[CONFIG] = _sha256(config)
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "application_version": __version__,
        "sqlite_user_version": user_version,
        "schema_sha256": schema_sha256,
        "files": files,
    }


def _supported_schema_metadata() -> tuple[int, str]:
    """Fingerprint a fresh database using this binary's migration path."""
    # Import lazily so the state module remains usable without snapshot support.
    from .state import State

    directory = Path(tempfile.mkdtemp(prefix=".nc-schema-"))
    database = directory / DATABASE
    try:
        state = State(database)
        state.db.close()
        return _schema_metadata(database)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def backup(home: Path, destination: Path) -> Path:
    """Take a consistent SQLite backup and atomically publish *destination*."""
    home, destination = Path(home), Path(destination)
    source_path = home / DATABASE
    if not source_path.is_file():
        raise SnapshotError(f"source database does not exist: {source_path}")
    if destination.exists():
        raise SnapshotError(f"destination already exists: {destination}")
    temp = _temporary_sibling(destination)
    try:
        os.chmod(temp, 0o700)
        copied = temp / DATABASE
        start = time.monotonic()
        try:
            source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=BACKUP_TIMEOUT_S)
            target = sqlite3.connect(copied)
            try:
                def progress(_status: int, _remaining: int, _total: int) -> None:
                    if time.monotonic() - start > BACKUP_TIMEOUT_S:
                        raise SnapshotError("SQLite backup timed out")
                source.backup(target, pages=64, progress=progress, sleep=0.05)
            finally:
                target.close()
                source.close()
        except sqlite3.Error as exc:
            raise SnapshotError(f"SQLite backup failed: {exc}") from exc
        os.chmod(copied, 0o600)
        source_config = home / CONFIG
        config = None
        if source_config.is_file():
            config = temp / CONFIG
            shutil.copyfile(source_config, config)
            os.chmod(config, 0o600)
        manifest = _manifest(copied, config)
        (temp / MANIFEST).write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        os.chmod(temp / MANIFEST, 0o600)
        # Re-read the manifest and its checksums before making it visible.
        _validate(temp, require_compatible=True)
        _atomic_publish(temp, destination)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return destination


def _validate(snapshot: Path, *, require_compatible: bool) -> dict[str, object]:
    snapshot = Path(snapshot)
    if not snapshot.is_dir() or snapshot.is_symlink():
        raise SnapshotError("snapshot must be a directory")
    manifest_path = snapshot / MANIFEST
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise SnapshotError("snapshot has no manifest")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError("invalid snapshot manifest") from exc
    required = {"format", "format_version", "application_version", "sqlite_user_version", "schema_sha256", "files"}
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise SnapshotError("unsupported snapshot manifest")
    # Compare only values with the exact JSON types used by this format.  In
    # particular, ``bool`` is an ``int`` subclass in Python, so ``true`` must
    # not be allowed to masquerade as format version 1 (or user version 0).
    if (
        type(manifest["format"]) is not str
        or type(manifest["format_version"]) is not int
        or type(manifest["application_version"]) is not str
        or type(manifest["sqlite_user_version"]) is not int
        or type(manifest["schema_sha256"]) is not str
        or type(manifest["files"]) is not dict
    ):
        raise SnapshotError("invalid snapshot manifest types")
    if manifest["format"] != FORMAT or manifest["format_version"] != FORMAT_VERSION:
        raise SnapshotError("unsupported snapshot format")
    if require_compatible and manifest["application_version"] != __version__:
        raise SnapshotError("snapshot application version is incompatible")
    if set(manifest["files"]) not in ({DATABASE}, {DATABASE, CONFIG}):
        raise SnapshotError("unsafe snapshot file list")
    # A snapshot is a closed format.  In particular, do not let a file that is
    # absent from the signed file list hitch a ride into a restored home.
    expected_names = {MANIFEST, *manifest["files"]}
    try:
        actual_names = {entry.name for entry in snapshot.iterdir()}
    except OSError as exc:
        raise SnapshotError("cannot inspect snapshot") from exc
    if actual_names != expected_names:
        raise SnapshotError("snapshot contains unsupported payload files")
    for name, checksum in manifest["files"].items():
        # Names are whitelisted above; this guard makes path traversal impossible even if changed.
        if name not in (DATABASE, CONFIG) or not isinstance(checksum, str):
            raise SnapshotError("unsafe snapshot file list")
        path = snapshot / name
        if not path.is_file() or path.is_symlink() or _sha256(path) != checksum:
            raise SnapshotError(f"snapshot checksum failed: {name}")
    user_version, schema_sha256 = _schema_metadata(snapshot / DATABASE)
    if user_version != manifest["sqlite_user_version"] or schema_sha256 != manifest["schema_sha256"]:
        raise SnapshotError("snapshot schema metadata is incompatible")
    if require_compatible and (user_version, schema_sha256) != _supported_schema_metadata():
        raise SnapshotError("snapshot database schema is unsupported")
    return manifest


def restore(snapshot: Path, home: Path) -> Path:
    """Validate *snapshot*, then publish it into a stopped, fresh *home*."""
    snapshot, home = Path(snapshot), Path(home)
    manifest = _validate(snapshot, require_compatible=True)
    if home.exists() and (not home.is_dir() or any(home.iterdir())):
        raise SnapshotError(f"restore home is not empty: {home}")
    temp = _temporary_sibling(home)
    try:
        os.chmod(temp, 0o700)
        # Copy only payload explicitly named by the validated manifest.  The
        # snapshot may be modified after the first validation, so validate the
        # exact staged bytes again immediately before publication.
        files = manifest["files"]
        assert isinstance(files, dict)  # established by _validate
        for name in files:
            shutil.copyfile(snapshot / name, temp / name)
            os.chmod(temp / name, 0o600)
        (temp / MANIFEST).write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        os.chmod(temp / MANIFEST, 0o600)
        _validate(temp, require_compatible=True)
        (temp / MANIFEST).unlink()
        # This is deliberately created before the directory becomes usable.
        (temp / "STOP").write_text(
            "restored snapshot; verify repositories and interrupted runs before nc resume\n"
        )
        os.chmod(temp / "STOP", 0o600)
        _atomic_publish(temp, home, allow_empty_destination=True)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return home
