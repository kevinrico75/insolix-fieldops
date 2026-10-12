"""Persistent paths, verified backups, and the session-reset script.

These tests use a temporary data directory. They do not open the workspace
database, production, or Railway.
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as fieldops
from backup_lib import (
    BackupError,
    BackupVerificationError,
    create_backup,
    format_stdout_payload,
    parse_stdout_payload,
    verify_backup,
)
from main import app
from storage_paths import StorageError, log_storage_paths, resolve_storage

ROOT = Path(__file__).resolve().parents[1]
SECRET_NAME = "CustomerSecretNameXYZ"
SECRET_TOKEN = "SECRETTOKENVALUE"


@pytest.fixture
def client():
    fieldops.clear_login_failures()
    with TestClient(app) as test_client:
        yield test_client
    fieldops.clear_login_failures()


def _run(args, **kwargs):
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        **kwargs,
    )


def _synthetic(tmp_path, *, read_only=True):
    data = tmp_path / "data"
    (data / "uploads").mkdir(parents=True)
    (data / "estimator_uploads" / "nested").mkdir(parents=True)
    (data / "uploads" / "plan.txt").write_text("plan-bytes", encoding="utf-8")
    (data / "estimator_uploads" / "nested" / "sheet.bin").write_bytes(b"estimator-bytes")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("outside-secret", encoding="utf-8")
    os.symlink(outside, data / "uploads" / "link.txt")
    db_path = data / "fieldops.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT)")
    connection.execute("INSERT INTO customers(name) VALUES (?)", (SECRET_NAME,))
    connection.execute(
        "CREATE TABLE sessions(token TEXT PRIMARY KEY, user_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    connection.execute(
        "INSERT INTO sessions(token, user_id, created_at) VALUES (?, 1, '2026-01-01 00:00:00')",
        (SECRET_TOKEN,),
    )
    connection.commit()
    connection.close()
    if read_only:
        os.chmod(db_path, 0o444)
    return data


def test_data_dir_absolute_paths_stay_on_the_volume():
    paths = resolve_storage("/app", {"INSOLIX_DATA_DIR": "/data", "RAILWAY_ENVIRONMENT": "production"})
    assert paths.data_dir == "/data"
    assert paths.db == "/data/fieldops.db"
    assert paths.uploads == "/data/uploads"
    assert paths.estimator_uploads == "/data/estimator_uploads"
    assert paths.backups == "/data/backups"
    assert paths.explicit is True
    assert paths.railway is True


def test_relative_data_dir_uses_the_app_directory_not_the_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths = resolve_storage("/app", {"INSOLIX_DATA_DIR": "var/data"})
    assert paths.data_dir == "/app/var/data"
    assert paths.db == "/app/var/data/fieldops.db"


def test_railway_without_data_dir_fails_closed():
    with pytest.raises(StorageError, match="INSOLIX_DATA_DIR is unset"):
        resolve_storage("/app", {"RAILWAY_ENVIRONMENT": "production"})


def test_railway_data_dir_inside_the_app_directory_fails_closed(tmp_path):
    with pytest.raises(StorageError, match="ephemeral"):
        resolve_storage(str(tmp_path), {"RAILWAY_ENVIRONMENT": "production", "INSOLIX_DATA_DIR": "data"})


def test_unset_data_dir_warns_and_stays_local(capsys):
    paths = resolve_storage("/app", {})
    assert paths.data_dir == "/app"
    assert paths.explicit is False
    log_storage_paths(paths)
    captured = capsys.readouterr()
    assert "INSOLIX WARNING" in captured.err
    assert "geometry_renders=memory" in captured.out


def test_live_modules_resolve_under_the_configured_data_dir():
    import agent_feature
    import estimator_feature
    import geometry_feature
    import opportunity_feature

    root = os.path.realpath(fieldops.DATA_DIR)
    labeled = {
        "app.db": fieldops.DB,
        "app.uploads": fieldops.UPLOADS,
        "app.backups": fieldops.BACKUPS,
        "estimator.db": estimator_feature.DB,
        "estimator.uploads": estimator_feature.UPLOAD_ROOT,
        "geometry.db": geometry_feature.DB,
        "geometry.uploads": geometry_feature.UPLOAD_ROOT,
        "opportunity.db": opportunity_feature.DB,
        "agent.db": agent_feature.DB,
    }
    for path in labeled.values():
        assert os.path.commonpath([root, os.path.realpath(path)]) == root
    assert estimator_feature.DB == geometry_feature.DB == opportunity_feature.DB == agent_feature.DB == fieldops.DB
    assert estimator_feature.UPLOAD_ROOT == geometry_feature.UPLOAD_ROOT
    fieldops.audit_runtime_paths(fieldops.PATHS)
    geometry = (ROOT / "geometry_feature.py").read_text(encoding="utf-8")
    assert 'tobytes("png")' in geometry
    assert "geometry_renders" not in geometry


def test_startup_log_prints_paths_without_secrets(client, capsys):
    assert client.get("/health").status_code == 200
    fieldops.startup()
    captured = capsys.readouterr().out
    assert "INSOLIX storage" in captured
    assert f"data_dir={fieldops.DATA_DIR}" in captured
    assert f"db={fieldops.DB}" in captured
    assert f"uploads={fieldops.UPLOADS}" in captured
    assert f"estimator_uploads={fieldops.PATHS.estimator_uploads}" in captured
    assert "geometry_renders=memory" in captured
    assert "admin123" not in captured
    assert SECRET_TOKEN not in captured


def test_online_backup_includes_wal_pages_a_raw_copy_misses(tmp_path):
    data = tmp_path / "wal-data"
    data.mkdir()
    db_path = data / "fieldops.db"
    held = sqlite3.connect(db_path)
    held.execute("PRAGMA journal_mode=WAL")
    held.execute("CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT)")
    held.execute("INSERT INTO customers(name) VALUES ('only-in-wal')")
    held.commit()
    assert (data / "fieldops.db-wal").exists()
    raw = tmp_path / "raw-copy.db"
    raw.write_bytes(db_path.read_bytes())
    with pytest.raises(sqlite3.OperationalError):
        sqlite3.connect(raw).execute("SELECT COUNT(*) FROM customers").fetchone()
    result = create_backup(data, tmp_path / "wal-staging")
    manifest = verify_backup(result.archive_path)
    assert manifest["tables"]["customers"] == 1
    held.close()


def test_backup_and_base64_roundtrip_match_the_manifest(tmp_path):
    data = _synthetic(tmp_path)
    source = data / "fieldops.db"
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    staging = tmp_path / "staging"
    result = create_backup(data, staging)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert not (data / "fieldops.db-journal").exists()
    assert not (data / "fieldops.db-wal").exists()
    assert os.path.commonpath([os.path.realpath(data), os.path.realpath(result.archive_path)]) != os.path.realpath(data)
    assert SECRET_NAME not in json.dumps(result.manifest)
    assert SECRET_TOKEN not in json.dumps(result.manifest)
    assert result.manifest["integrity_check"] == "ok"
    assert result.manifest["tables"]["customers"] == 1
    assert result.manifest["tables"]["sessions"] == 1
    names = [item["path"] for item in result.manifest["files"]]
    assert "fieldops.db" in names
    assert "uploads/plan.txt" in names
    assert "estimator_uploads/nested/sheet.bin" in names
    assert "uploads/link.txt" not in names
    assert "outside-secret" not in json.dumps(result.manifest)

    transcript = format_stdout_payload(result, chunk_size=3)
    header, _base64 = transcript.split("INSOLIX_BACKUP_BEGIN", 1)
    assert SECRET_NAME not in header
    assert SECRET_TOKEN not in header
    digest, payload = parse_stdout_payload(transcript)
    assert digest == result.archive_sha256
    restored = tmp_path / "roundtrip.tar.gz"
    restored.write_bytes(payload)
    manifest = verify_backup(restored)
    assert manifest["file_count"] == result.manifest["file_count"]
    assert manifest["tables"] == result.manifest["tables"]


def test_stdin_backup_script_and_cli_verifier(tmp_path):
    data = _synthetic(tmp_path)
    staging = tmp_path / "stdin-staging"
    script = (ROOT / "backup_lib.py").read_text(encoding="utf-8")
    piped = subprocess.run(
        [
            sys.executable,
            "-",
            "--data-dir",
            str(data),
            "--staging-dir",
            str(staging),
            "--stdout-base64",
            "--chunk-size",
            "57",
        ],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    assert piped.returncode == 0, piped.stderr
    _digest, payload = parse_stdout_payload(piped.stdout)
    archive = tmp_path / "from-stdin.tar.gz"
    archive.write_bytes(payload)
    verified = _run(["scripts/verify_backup.py", str(archive)])
    assert verified.returncode == 0, verified.stderr
    assert verified.stdout.startswith("INSOLIX backup ok")

    cli = _run(
        [
            "scripts/backup.py",
            "--data-dir",
            str(data),
            "--staging-dir",
            str(tmp_path / "cli-staging"),
            "--stdout-base64",
        ]
    )
    assert cli.returncode == 0, cli.stderr
    _digest, cli_payload = parse_stdout_payload(cli.stdout)
    cli_archive = tmp_path / "from-cli.tar.gz"
    cli_archive.write_bytes(cli_payload)
    assert verify_backup(cli_archive)["tables"]["customers"] == 1


def test_backup_refuses_to_stage_inside_the_data_directory(tmp_path):
    data = _synthetic(tmp_path, read_only=False)
    with pytest.raises(BackupError, match="inside the data directory"):
        create_backup(data, data / "backups")


def test_verify_fails_on_tampered_file_and_corrupt_database(tmp_path):
    data = _synthetic(tmp_path)
    result = create_backup(data, tmp_path / "staging")
    changed = tmp_path / "changed.tar.gz"
    _repack(result.archive_path, changed, lambda root: (root / "uploads" / "plan.txt").write_text("tampered", encoding="utf-8"))
    with pytest.raises(BackupVerificationError, match="sha256 mismatch"):
        verify_backup(changed)
    cli = _run(["scripts/verify_backup.py", str(changed)])
    assert cli.returncode == 1
    assert "sha256 mismatch" in cli.stderr

    corrupt = tmp_path / "corrupt.tar.gz"

    def smash(root):
        db_path = root / "fieldops.db"
        db_path.write_bytes(b"this is not a sqlite database")
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        digest = hashlib.sha256(db_path.read_bytes()).hexdigest()
        for entry in manifest["files"]:
            if entry["path"] == "fieldops.db":
                entry["sha256"] = digest
                entry["size"] = db_path.stat().st_size
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    _repack(result.archive_path, corrupt, smash)
    with pytest.raises(BackupVerificationError, match="integrity_check"):
        verify_backup(corrupt)


def test_admin_backup_route_verifies_and_keeps_only_a_manifest(client, tmp_path):
    with fieldops.db() as connection:
        connection.execute("INSERT INTO customers(name,company) VALUES (?, 'GC')", (SECRET_NAME,))
    assert client.post(
        "/login",
        data={"email": "admin@local", "password": "admin123"},
        follow_redirects=False,
    ).status_code == 303
    response = client.get("/backup")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/gzip")
    archive = tmp_path / "admin.tar.gz"
    archive.write_bytes(response.content)
    manifest = verify_backup(archive)
    assert manifest["tables"]["customers"] >= 1
    saved = sorted(Path(fieldops.BACKUPS).glob("manifest-*.json"))
    assert saved
    body = saved[-1].read_text(encoding="utf-8")
    assert SECRET_NAME not in body
    assert "sha256" in body
    assert not any(Path(fieldops.DATA_DIR).glob("insolix-backup-*.tar.gz"))


def test_session_script_is_dry_run_until_a_fresh_backup_is_confirmed(tmp_path):
    data = _synthetic(tmp_path, read_only=False)
    db_path = data / "fieldops.db"
    fresh = create_backup(data, tmp_path / "fresh-staging")
    stale = create_backup(
        data,
        tmp_path / "stale-staging",
        now=datetime.now(timezone.utc) - timedelta(hours=3),
    )

    dry = _run(["scripts/invalidate_sessions.py", "--db", str(db_path)])
    assert dry.returncode == 0
    assert "mode=dry-run" in dry.stdout
    assert "sessions_count=1" in dry.stdout
    assert "oldest_created_at=2026-01-01 00:00:00" in dry.stdout
    assert SECRET_TOKEN not in dry.stdout
    assert SECRET_TOKEN not in dry.stderr
    assert _session_count(db_path) == 1

    refused = _run(["scripts/invalidate_sessions.py", "--db", str(db_path), "--execute", "--backup", fresh.archive_path])
    assert refused.returncode == 2
    assert _session_count(db_path) == 1

    missing = _run(
        [
            "scripts/invalidate_sessions.py",
            "--db",
            str(db_path),
            "--execute",
            "--confirm",
            "DELETE_SESSIONS",
        ]
    )
    assert missing.returncode == 1
    assert _session_count(db_path) == 1

    old = _run(
        [
            "scripts/invalidate_sessions.py",
            "--db",
            str(db_path),
            "--execute",
            "--confirm",
            "DELETE_SESSIONS",
            "--backup",
            stale.archive_path,
            "--max-age-minutes",
            "60",
        ]
    )
    assert old.returncode == 1
    assert "minutes old" in old.stderr
    assert _session_count(db_path) == 1

    done = _run(
        [
            "scripts/invalidate_sessions.py",
            "--db",
            str(db_path),
            "--execute",
            "--confirm",
            "DELETE_SESSIONS",
            "--backup",
            fresh.archive_path,
            "--max-age-minutes",
            "60",
        ]
    )
    assert done.returncode == 0, done.stderr
    assert "mode=executed" in done.stdout
    assert "sessions_count=0" in done.stdout
    assert SECRET_TOKEN not in done.stdout
    assert _session_count(db_path) == 0
    assert _customer_count(db_path) == 1


def test_runbooks_match_this_repository():
    railway = json.loads((ROOT / "railway.json").read_text(encoding="utf-8"))
    assert "Dockerfile" not in {path.name for path in ROOT.iterdir()}
    assert railway["build"]["builder"] == "RAILPACK"
    assert railway["deploy"]["startCommand"] == "uvicorn main:app --host 0.0.0.0 --port $PORT"
    windows = (ROOT / "docs" / "BACKUP_RUNBOOK_WINDOWS.md").read_text(encoding="utf-8")
    security = (ROOT / "docs" / "SECURITY_RUNBOOK.md").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "age" not in requirements.lower()
    assert "cryptography" not in requirements.lower()
    for snippet in (
        "insolix-web",
        "/data",
        "--staging-dir /tmp",
        "FromBase64String",
        "INSOLIX_BACKUP_BEGIN",
        "FiloSottile.age",
        "python -",
        "/app/scripts/backup.py",
        "uvicorn main:app",
        "AGE_NOT_IN_IMAGE",
        "Connection.backup",
        "**STOP.",
        "Do not use PowerShell `>`",
    ):
        assert snippet in windows
    assert "DELETE FROM sessions" not in windows
    assert "docs/BACKUP_RUNBOOK_WINDOWS.md" in security or "BACKUP_RUNBOOK_WINDOWS.md" in security
    assert "DELETE_SESSIONS" in security
    assert "git history" in security.lower()
    assert "optional" in security.lower()


def _session_count(db_path):
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    finally:
        connection.close()


def _customer_count(db_path):
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    finally:
        connection.close()


def _repack(source_archive, dest_archive, mutate):
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with tarfile.open(source_archive, "r:*") as archive:
            archive.extractall(root, filter="data")
        mutate(root)
        with tarfile.open(dest_archive, "w:gz") as archive:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    archive.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
