"""Consistent INSOLIX backup and verification. Standard library only.

The database is copied with SQLite's online backup API from a read-only
connection. Job uploads and estimator uploads are archived beside that
snapshot. The manifest stores file sizes, SHA-256 checksums, and row counts.
It does not store row contents.

This file is safe to pipe into a production container that does not have the
rest of this branch yet:

    Get-Content -Raw backup_lib.py | railway ssh -- python - --data-dir /data --staging-dir /tmp --stdout-base64
"""
import argparse
import base64
import hashlib
import os
import sqlite3
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path


class BackupError(RuntimeError):
    pass


class BackupVerificationError(BackupError):
    def __init__(self, mismatches):
        self.mismatches = list(mismatches)
        super().__init__("Backup verification failed: " + "; ".join(self.mismatches))


class BackupResult:
    def __init__(self, archive_path, work_dir, manifest, archive_sha256):
        self.archive_path = archive_path
        self.work_dir = work_dir
        self.manifest = manifest
        self.archive_sha256 = archive_sha256


def _within(path, root):
    path = os.path.realpath(path)
    root = os.path.realpath(root)
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _sha256_file(path):
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _sqlite_uri(path):
    return Path(path).resolve().as_uri() + "?mode=ro"


def sqlite_online_backup(src_path, dest_path):
    """Copy src_path with the SQLite backup API. Does not copy the file bytes."""
    if not os.path.isfile(src_path):
        raise BackupError(f"Database not found: {src_path}")
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    if os.path.exists(dest_path):
        os.remove(dest_path)
    source = sqlite3.connect(_sqlite_uri(src_path), uri=True)
    try:
        target = sqlite3.connect(dest_path)
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()
    finally:
        source.close()


def integrity_check(db_path):
    connection = sqlite3.connect(_sqlite_uri(db_path), uri=True)
    try:
        rows = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    finally:
        connection.close()
    if rows == ["ok"]:
        return "ok"
    return "; ".join(str(row) for row in rows) or "empty"


def table_row_counts(db_path):
    connection = sqlite3.connect(_sqlite_uri(db_path), uri=True)
    try:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts = {}
        for name in names:
            if not name.replace("_", "").isalnum() or name[0].isdigit():
                continue
            counts[name] = connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        return counts
    finally:
        connection.close()


def _iter_tree(root, arc_prefix):
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name for name in dirnames if not os.path.islink(os.path.join(dirpath, name))
        )
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            relative = os.path.relpath(full, root).replace(os.sep, "/")
            if relative.startswith("../") or relative == "..":
                continue
            yield f"{arc_prefix}/{relative}", full


def _file_entry(arcname, path):
    digest, size = _sha256_file(path)
    return {"path": arcname, "size": size, "sha256": digest}


def create_backup(data_dir, staging_dir, record_manifest_dir=None, now=None):
    """Build a gzip tar in staging_dir. Refuses to place it inside data_dir."""
    data_dir = os.path.abspath(data_dir)
    staging_dir = os.path.abspath(staging_dir)
    os.makedirs(staging_dir, exist_ok=True)
    if _within(staging_dir, data_dir):
        raise BackupError(
            f"Refusing to stage a backup inside the data directory ({staging_dir} is under {data_dir})"
        )
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    stamp = moment.strftime("%Y%m%dT%H%M%S%fZ")
    work_dir = os.path.join(staging_dir, f"insolix-backup-{stamp}")
    archive_path = os.path.join(staging_dir, f"insolix-backup-{stamp}.tar.gz")
    os.makedirs(work_dir, exist_ok=False)
    snapshot = os.path.join(work_dir, "fieldops.db")
    try:
        sqlite_online_backup(os.path.join(data_dir, "fieldops.db"), snapshot)
        integrity = integrity_check(snapshot)
        if integrity != "ok":
            raise BackupError(f"SQLite integrity_check failed on the snapshot: {integrity}")
        counts = table_row_counts(snapshot)
        files = [_file_entry("fieldops.db", snapshot)]
        for arcname, path in _iter_tree(os.path.join(data_dir, "uploads"), "uploads"):
            files.append(_file_entry(arcname, path))
        for arcname, path in _iter_tree(os.path.join(data_dir, "estimator_uploads"), "estimator_uploads"):
            files.append(_file_entry(arcname, path))
        manifest = {
            "created_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "data_dir": data_dir,
            "file_count": len(files),
            "files": files,
            "tables": counts,
            "integrity_check": integrity,
        }
        manifest_path = os.path.join(work_dir, "manifest.json")
        _write_manifest(manifest_path, manifest)
        if record_manifest_dir:
            os.makedirs(record_manifest_dir, exist_ok=True)
            _write_manifest(os.path.join(record_manifest_dir, f"manifest-{stamp}.json"), manifest)
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(snapshot, arcname="fieldops.db", recursive=False, filter=_tar_filter)
            for arcname, path in _iter_tree(os.path.join(data_dir, "uploads"), "uploads"):
                archive.add(path, arcname=arcname, recursive=False, filter=_tar_filter)
            for arcname, path in _iter_tree(os.path.join(data_dir, "estimator_uploads"), "estimator_uploads"):
                archive.add(path, arcname=arcname, recursive=False, filter=_tar_filter)
            archive.add(manifest_path, arcname="manifest.json", recursive=False, filter=_tar_filter)
        archive_sha, _size = _sha256_file(archive_path)
        return BackupResult(archive_path, work_dir, manifest, archive_sha)
    except Exception:
        if os.path.isdir(work_dir):
            _remove_tree(work_dir)
        if os.path.isfile(archive_path):
            os.remove(archive_path)
        raise


def _tar_filter(info):
    if info.issym() or info.islnk():
        return None
    return info


def _write_manifest(path, manifest):
    import json

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _remove_tree(path):
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def verify_backup(archive_path):
    """Re-open the snapshot, compare checksums and row counts, raise on mismatch."""
    import json
    import tempfile

    mismatches = []
    if not os.path.isfile(archive_path):
        raise BackupVerificationError([f"Archive not found: {archive_path}"])
    with tempfile.TemporaryDirectory(prefix="insolix-verify-") as extracted:
        try:
            with tarfile.open(archive_path, "r:*") as archive:
                archive.extractall(extracted, filter="data")
        except (tarfile.TarError, OSError, ValueError) as exc:
            raise BackupVerificationError([f"Could not read archive: {exc}"]) from exc
        manifest_path = os.path.join(extracted, "manifest.json")
        if not os.path.isfile(manifest_path):
            raise BackupVerificationError(["manifest.json is missing"])
        try:
            with open(manifest_path, encoding="utf-8") as handle:
                manifest = json.load(handle)
        except json.JSONDecodeError as exc:
            raise BackupVerificationError([f"manifest.json is not valid JSON: {exc}"]) from exc
        files = manifest.get("files")
        if not isinstance(files, list):
            mismatches.append("manifest files is not a list")
            files = []
        if manifest.get("file_count") != len(files):
            mismatches.append(
                f"file_count {manifest.get('file_count')} does not match {len(files)} listed files"
            )
        listed = []
        for entry in files:
            if not isinstance(entry, dict) or "path" not in entry:
                mismatches.append("manifest file entry is missing a path")
                continue
            rel = str(entry["path"]).replace("\\", "/")
            if rel.startswith("/") or ".." in rel.split("/"):
                mismatches.append(f"unsafe path in manifest: {rel}")
                continue
            listed.append(rel)
            full = os.path.join(extracted, *rel.split("/"))
            if not os.path.isfile(full):
                mismatches.append(f"missing file: {rel}")
                continue
            digest, size = _sha256_file(full)
            if int(entry.get("size", -1)) != size:
                mismatches.append(f"size mismatch for {rel}: manifest {entry.get('size')} actual {size}")
            if str(entry.get("sha256", "")).lower() != digest:
                mismatches.append(f"sha256 mismatch for {rel}")
        present = []
        for dirpath, _dirs, filenames in os.walk(extracted):
            for name in filenames:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, extracted).replace(os.sep, "/")
                if rel == "manifest.json":
                    continue
                present.append(rel)
        for rel in sorted(set(present) - set(listed)):
            mismatches.append(f"extra file not in manifest: {rel}")
        db_path = os.path.join(extracted, "fieldops.db")
        if not os.path.isfile(db_path):
            mismatches.append("fieldops.db is missing")
        else:
            try:
                integrity = integrity_check(db_path)
            except sqlite3.DatabaseError as exc:
                mismatches.append(f"integrity_check failed: {exc}")
                integrity = "error"
            if integrity != "ok":
                mismatches.append(f"integrity_check returned {integrity}")
            elif manifest.get("integrity_check") != "ok":
                mismatches.append(f"manifest integrity_check is {manifest.get('integrity_check')}")
            try:
                actual_counts = table_row_counts(db_path)
            except sqlite3.DatabaseError as exc:
                mismatches.append(f"row counts failed: {exc}")
                actual_counts = {}
            expected_counts = manifest.get("tables") or {}
            if not isinstance(expected_counts, dict):
                mismatches.append("manifest tables is not an object")
                expected_counts = {}
            if set(expected_counts) != set(actual_counts):
                mismatches.append(
                    "table set mismatch: "
                    f"manifest {sorted(expected_counts)} actual {sorted(actual_counts)}"
                )
            for name, expected in expected_counts.items():
                actual = actual_counts.get(name)
                if actual is not None and int(expected) != int(actual):
                    mismatches.append(f"row count mismatch for {name}: manifest {expected} actual {actual}")
        if mismatches:
            raise BackupVerificationError(mismatches)
        return manifest


def iter_base64_lines(path, chunk_size=57 * 1024):
    if chunk_size % 3 != 0:
        raise ValueError("chunk_size must be a multiple of 3 so the lines concatenate")
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            yield base64.b64encode(chunk).decode("ascii")


def format_stdout_payload(result, chunk_size=57 * 1024):
    lines = [
        f"INSOLIX_BACKUP_SHA256={result.archive_sha256}",
        f"INSOLIX_BACKUP_ARCHIVE={result.archive_path}",
        f"INSOLIX_BACKUP_WORKDIR={result.work_dir}",
        "INSOLIX_BACKUP_BEGIN",
        *iter_base64_lines(result.archive_path, chunk_size=chunk_size),
        "INSOLIX_BACKUP_END",
    ]
    return "\n".join(lines) + "\n"


def parse_stdout_payload(text):
    """Split a captured backup transcript into (sha256, archive bytes)."""
    import re

    match = re.search(
        r"INSOLIX_BACKUP_SHA256=([0-9a-fA-F]{64}).*?INSOLIX_BACKUP_BEGIN\s*(.*?)\s*INSOLIX_BACKUP_END",
        text,
        re.S,
    )
    if not match:
        raise BackupError("Backup transcript is missing INSOLIX_BACKUP_BEGIN / END markers")
    digest = match.group(1).lower()
    compact = re.sub(r"\s+", "", match.group(2))
    try:
        payload = base64.b64decode(compact, validate=True)
    except Exception as exc:
        raise BackupError(f"Backup transcript is not valid base64: {exc}") from exc
    actual = hashlib.sha256(payload).hexdigest()
    if actual != digest:
        raise BackupError(f"Transcript checksum mismatch: header {digest} actual {actual}")
    return digest, payload


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create a consistent INSOLIX backup in a staging directory.")
    parser.add_argument("--data-dir", default=os.environ.get("INSOLIX_DATA_DIR", "/data"))
    parser.add_argument("--staging-dir", default=os.environ.get("INSOLIX_BACKUP_STAGING", "/tmp"))
    parser.add_argument(
        "--record-manifest",
        default="",
        help="Optional directory for a small manifest copy. Leave empty to avoid writing the data volume.",
    )
    parser.add_argument("--stdout-base64", action="store_true", help="Print the archive as base64 markers on stdout.")
    parser.add_argument("--chunk-size", type=int, default=57 * 1024)
    args = parser.parse_args(argv)
    try:
        result = create_backup(
            args.data_dir,
            args.staging_dir,
            record_manifest_dir=args.record_manifest or None,
        )
    except BackupError as exc:
        print(f"INSOLIX backup failed: {exc}", file=sys.stderr)
        return 1
    if args.stdout_base64:
        sys.stdout.write(format_stdout_payload(result, chunk_size=args.chunk_size))
        sys.stdout.flush()
    else:
        print(result.archive_path)
        print(f"INSOLIX_BACKUP_SHA256={result.archive_sha256}")
        print(f"INSOLIX_BACKUP_WORKDIR={result.work_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
