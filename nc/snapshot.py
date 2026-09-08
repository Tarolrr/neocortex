"""Verified, portable SQLite snapshots.

Snapshots intentionally contain only the Neocortex database and optional
configuration.  Repositories, worktrees, logs and running processes are not a
transactional part of this format.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from . import __version__

FORMAT = "neocortex-snapshot"
FORMAT_VERSION = 1
INCREMENTAL_FORMAT_VERSION = 2
MANIFEST = "manifest.json"
DATABASE = "state.db"
CONFIG = "config.json"
BACKUP_TIMEOUT_S = 5.0
BLOCK_SIZE = 1024 * 1024
DEFAULT_RETENTION = 96
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_ID = re.compile(r"[0-9a-f]{24}\Z")


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


def _stage_database(home: Path, temp: Path) -> tuple[Path, Path | None]:
    """Make the complete, consistent local copy used by both formats."""
    source_path = home / DATABASE
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
            target.close(); source.close()
    except sqlite3.Error as exc:
        raise SnapshotError(f"SQLite backup failed: {exc}") from exc
    os.chmod(copied, 0o600)
    config = None
    if (home / CONFIG).is_file():
        config = temp / CONFIG
        shutil.copyfile(home / CONFIG, config)
        os.chmod(config, 0o600)
    return copied, config


def backup(home: Path, destination: Path, *, incremental: bool = False,
           retain: int = DEFAULT_RETENTION) -> Path:
    """Take a consistent SQLite backup and atomically publish *destination*."""
    home, destination = Path(home), Path(destination)
    source_path = home / DATABASE
    if not source_path.is_file():
        raise SnapshotError(f"source database does not exist: {source_path}")
    if incremental:
        if type(retain) is not int or retain <= 0:
            raise SnapshotError("retention must be a positive count")
        return _incremental_backup(home, destination, retain)
    if destination.exists():
        raise SnapshotError(f"destination already exists: {destination}")
    temp = _temporary_sibling(destination)
    try:
        os.chmod(temp, 0o700)
        copied, config = _stage_database(home, temp)
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


def _source_id(home: Path) -> str:
    return hashlib.sha256(str(home.resolve()).encode()).hexdigest()[:24]


def _blocks(path: Path) -> list[dict[str, object]]:
    result = []
    with path.open("rb") as stream:
        while data := stream.read(BLOCK_SIZE):
            result.append({"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    return result


def _copy_blocks(path: Path, blocks_dir: Path) -> tuple[list[dict[str, object]], int, int]:
    blocks = _blocks(path); new = 0; reused = 0
    with path.open("rb") as stream:
        for block in blocks:
            data = stream.read(int(block["size"]))
            target = blocks_dir / str(block["sha256"])
            if target.is_file() and not target.is_symlink() and _sha256(target) == block["sha256"]:
                reused += len(data); continue
            if target.exists():
                raise SnapshotError("corrupt shared block")
            fd, name = tempfile.mkstemp(prefix=".block-", dir=blocks_dir)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(data); output.flush(); os.fsync(output.fileno())
                os.chmod(name, 0o600); os.replace(name, target); new += len(data)
            except Exception:
                Path(name).unlink(missing_ok=True)
                raise
    return blocks, new, reused


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and bool(_DIGEST.fullmatch(value))


def _is_source_id(value: object) -> bool:
    return isinstance(value, str) and bool(_SOURCE_ID.fullmatch(value))


def _incremental_manifest(snapshot: Path, *, source: str | None = None) -> dict[str, object] | None:
    """Read a fully-published v2 manifest without dereferencing store links."""
    manifest_path = snapshot / MANIFEST
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        return None
    required = {"format", "format_version", "application_version", "sqlite_user_version",
                "schema_sha256", "source", "files", "logical_bytes", "new_bytes", "reused_bytes"}
    if not isinstance(manifest, dict) or set(manifest) != required:
        return None
    if (manifest.get("format") != FORMAT or manifest.get("format_version") != INCREMENTAL_FORMAT_VERSION
            or not isinstance(manifest.get("application_version"), str)
            or type(manifest.get("sqlite_user_version")) is not int
            or not _is_digest(manifest.get("schema_sha256"))
            or not _is_source_id(manifest.get("source"))
            or (source is not None and manifest["source"] != source)):
        return None
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) not in ({DATABASE}, {DATABASE, CONFIG}):
        return None
    logical = 0
    for name, entry in files.items():
        if name not in (DATABASE, CONFIG) or not isinstance(entry, dict):
            return None
        checksum, size, pieces = entry.get("sha256"), entry.get("size"), entry.get("blocks")
        if not _is_digest(checksum) or type(size) is not int or size < 0 or not isinstance(pieces, list):
            return None
        total = 0
        for piece in pieces:
            if (not isinstance(piece, dict) or set(piece) != {"sha256", "size"}
                    or not _is_digest(piece.get("sha256")) or type(piece.get("size")) is not int
                    or piece["size"] <= 0):
                return None
            total += piece["size"]
        if total != size:
            return None
        logical += size
    if (type(manifest.get("logical_bytes")) is not int or type(manifest.get("new_bytes")) is not int
            or type(manifest.get("reused_bytes")) is not int or manifest["logical_bytes"] != logical
            or manifest["new_bytes"] < 0 or manifest["reused_bytes"] < 0
            or manifest["new_bytes"] + manifest["reused_bytes"] != logical):
        return None
    return manifest


def _atomic_manifest(path: Path, manifest: dict[str, object]) -> None:
    """Publish a manifest only after its complete bytes are durable."""
    fd, temporary = tempfile.mkstemp(prefix=".manifest-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(manifest, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _incremental_backup(home: Path, store: Path, retain: int) -> Path:
    """Publish a manifest last; blocks are content addressed and immutable."""
    if store.is_symlink():
        raise SnapshotError("incremental destination must be a directory")
    store.mkdir(parents=True, exist_ok=True)
    if not store.is_dir(): raise SnapshotError("incremental destination must be a directory")
    os.chmod(store, 0o700)
    lock = store / ".lock"
    if lock.is_symlink():
        raise SnapshotError("incremental store lock is unsafe")
    with lock.open("a+") as guard:
        os.chmod(lock, 0o600); fcntl.flock(guard, fcntl.LOCK_EX)
        blocks_dir = store / "blocks"
        if blocks_dir.is_symlink(): raise SnapshotError("shared block store is unsafe")
        blocks_dir.mkdir(exist_ok=True)
        if not blocks_dir.is_dir(): raise SnapshotError("shared block store is unsafe")
        os.chmod(blocks_dir, 0o700)
        source = _source_id(home); snapshots = store / "snapshots" / source
        if (store / "snapshots").is_symlink() or snapshots.is_symlink():
            raise SnapshotError("snapshot namespace is unsafe")
        snapshots.mkdir(parents=True, exist_ok=True)
        if not snapshots.is_dir(): raise SnapshotError("snapshot namespace is unsafe")
        os.chmod(snapshots, 0o700)
        stage = _temporary_sibling(store / "stage")
        try:
            db, config = _stage_database(home, stage)
            files: dict[str, object] = {}
            new = reused = 0
            for name, path in ((DATABASE, db), (CONFIG, config)):
                if path is None: continue
                parts, added, old = _copy_blocks(path, blocks_dir)
                files[name] = {"sha256": _sha256(path), "size": path.stat().st_size, "blocks": parts}
                new += added; reused += old
            user_version, schema_sha256 = _schema_metadata(db)
            manifest = {"format": FORMAT, "format_version": INCREMENTAL_FORMAT_VERSION,
                "application_version": __version__, "sqlite_user_version": user_version,
                "schema_sha256": schema_sha256, "source": source, "files": files,
                "logical_bytes": sum(int(x["size"]) for x in files.values()),
                "new_bytes": new, "reused_bytes": reused}
            leaf = snapshots / f"{time.time_ns():020d}"
            leaf.mkdir(mode=0o700)
            manifest_path = leaf / MANIFEST
            _atomic_manifest(manifest_path, manifest)  # intentionally the last publication
            _prune_store(store, source, retain)
            return leaf
        finally:
            shutil.rmtree(stage, ignore_errors=True)


def _prune_store(store: Path, source: str, retain: int) -> None:
    directory = store / "snapshots" / source
    recognized = [p for p in directory.iterdir() if p.is_dir() and not p.is_symlink()
                  and _incremental_manifest(p, source=source) is not None]
    recognized.sort(key=lambda p: p.name, reverse=True)
    for old in recognized[retain:]: shutil.rmtree(old)
    referenced: set[str] = set()
    namespace = store / "snapshots"
    if namespace.is_symlink() or not namespace.is_dir():
        raise SnapshotError("snapshot namespace is unsafe")
    for owner in namespace.iterdir():
        if not owner.is_dir() or owner.is_symlink() or not _is_source_id(owner.name):
            continue
        for snapshot in owner.iterdir():
            if not snapshot.is_dir() or snapshot.is_symlink():
                continue
            data = _incremental_manifest(snapshot, source=owner.name)
            if data is not None:
                for f in data["files"].values():
                    referenced.update(b["sha256"] for b in f["blocks"])
    for block in (store / "blocks").iterdir():
        if (block.is_file() and not block.is_symlink() and _is_digest(block.name)
                and block.name not in referenced):
            block.unlink()


def _materialize_incremental(snapshot: Path, output: Path) -> None:
    """Check every shared block and make a private v1-shaped staging copy."""
    if not snapshot.is_dir() or snapshot.is_symlink() or len(snapshot.parents) < 3:
        raise SnapshotError("incremental snapshot must be a directory")
    manifest = _incremental_manifest(snapshot)
    if manifest is None:
        raise SnapshotError("unsupported incremental snapshot manifest")
    files = manifest["files"]
    if not isinstance(files, dict) or set(files) not in ({DATABASE}, {DATABASE, CONFIG}):
        raise SnapshotError("unsafe snapshot file list")
    store = snapshot.parents[2]
    if store.is_symlink() or not store.is_dir():
        raise SnapshotError("shared block store is unavailable")
    blocks_dir = store / "blocks"
    if blocks_dir.is_symlink() or not blocks_dir.is_dir(): raise SnapshotError("shared block store is unavailable")
    plain_files: dict[str, str] = {}
    for name, entry in files.items():
        if name not in (DATABASE, CONFIG) or not isinstance(entry, dict): raise SnapshotError("unsafe snapshot file list")
        checksum, size, pieces = entry.get("sha256"), entry.get("size"), entry.get("blocks")
        if not isinstance(checksum, str) or type(size) is not int or size < 0 or not isinstance(pieces, list):
            raise SnapshotError("invalid shared block metadata")
        target = output / name
        with target.open("wb") as stream:
            total = 0
            for piece in pieces:
                if not isinstance(piece, dict) or set(piece) != {"sha256", "size"} or not isinstance(piece["sha256"], str) or type(piece["size"]) is not int:
                    raise SnapshotError("invalid shared block metadata")
                block = blocks_dir / piece["sha256"]
                if not block.is_file() or block.is_symlink() or block.name != piece["sha256"] or _sha256(block) != piece["sha256"] or block.stat().st_size != piece["size"]:
                    raise SnapshotError("shared block checksum failed")
                with block.open("rb") as input: shutil.copyfileobj(input, stream)
                total += piece["size"]
        os.chmod(target, 0o600)
        if total != size or _sha256(target) != checksum: raise SnapshotError("shared payload checksum failed")
        plain_files[name] = checksum
    plain = {k: manifest[k] for k in ("format", "application_version", "sqlite_user_version", "schema_sha256")}
    plain.update({"format_version": FORMAT_VERSION, "files": plain_files})
    (output / MANIFEST).write_text(json.dumps(plain, sort_keys=True, indent=2) + "\n")
    os.chmod(output / MANIFEST, 0o600)


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
    # An incremental manifest is a complete recipe, not a chain: materialize
    # it from its verified shared blocks before following the normal v1 path.
    try:
        version = json.loads((snapshot / MANIFEST).read_text()).get("format_version")
    except (OSError, ValueError):
        version = None
    materialized = None
    if version == INCREMENTAL_FORMAT_VERSION:
        materialized = _temporary_sibling(home)
        try:
            os.chmod(materialized, 0o700)
            _materialize_incremental(snapshot, materialized)
            snapshot = materialized
        except Exception:
            shutil.rmtree(materialized, ignore_errors=True)
            raise
    try:
        manifest = _validate(snapshot, require_compatible=True)
    except Exception:
        if materialized is not None:
            shutil.rmtree(materialized, ignore_errors=True)
        raise
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
    finally:
        if materialized is not None:
            shutil.rmtree(materialized, ignore_errors=True)
    return home
