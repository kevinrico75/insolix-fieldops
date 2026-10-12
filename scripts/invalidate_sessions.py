#!/usr/bin/env python3
"""Show or delete rows in the sessions table.

Dry-run is the default. It prints the row count and the oldest and newest
created_at values. It never prints session tokens.

    python scripts/invalidate_sessions.py --db /data/fieldops.db

Deleting requires all of the following, and this script does not run them
unless you pass them yourself:

    python scripts/invalidate_sessions.py --db /data/fieldops.db --execute \\
        --confirm DELETE_SESSIONS --backup /tmp/insolix-backup-STAMP.tar.gz

--execute re-opens that backup, checks its manifest is newer than
--max-age-minutes (default 60), and only then deletes sessions inside a
transaction. Sessions are random database tokens, not signed cookies, so
there is no signing secret to rotate.
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backup_lib import BackupError, BackupVerificationError, verify_backup


CONFIRM = "DELETE_SESSIONS"


def session_summary(db_path):
    connection = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchone()
        if not exists:
            return 0, None, None
        count, oldest, newest = connection.execute(
            "SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM sessions"
        ).fetchone()
        return int(count), oldest, newest
    finally:
        connection.close()


def format_summary(count, oldest, newest, mode):
    return (
        f"sessions_count={count}\n"
        f"oldest_created_at={oldest or 'none'}\n"
        f"newest_created_at={newest or 'none'}\n"
        f"mode={mode}\n"
    )


def _parse_created_at(value):
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError) as exc:
        raise BackupError(f"Backup manifest created_at is not UTC ISO-8601: {value}") from exc
    return parsed.replace(tzinfo=timezone.utc)


def manifest_is_fresh(manifest, max_age_minutes, now=None):
    created = _parse_created_at(manifest.get("created_at"))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age_seconds = (current - created).total_seconds()
    if age_seconds < -300:
        raise BackupError("Backup manifest created_at is more than 5 minutes in the future")
    if age_seconds > max_age_minutes * 60:
        raise BackupError(
            f"Backup manifest is {int(age_seconds // 60)} minutes old; "
            f"limit is {max_age_minutes} minutes"
        )
    return created


def delete_sessions(db_path):
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute("DELETE FROM sessions")
        deleted = cursor.rowcount
        connection.commit()
        return deleted
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Dry-run or delete INSOLIX session rows.")
    parser.add_argument("--db", required=True, help="Path to fieldops.db. There is no default.")
    parser.add_argument("--execute", action="store_true", help="Delete session rows. Dry-run is the default.")
    parser.add_argument("--confirm", default="", help=f"Must be exactly {CONFIRM} together with --execute.")
    parser.add_argument("--backup", default="", help="Backup .tar.gz to verify before --execute.")
    parser.add_argument("--max-age-minutes", type=int, default=60)
    args = parser.parse_args(argv)
    if not os.path.isfile(args.db):
        print(f"Database not found: {args.db}", file=sys.stderr)
        return 1
    count, oldest, newest = session_summary(args.db)
    if not args.execute:
        sys.stdout.write(format_summary(count, oldest, newest, "dry-run"))
        return 0
    if args.confirm != CONFIRM:
        print(
            f"Refusing to delete sessions. Pass --confirm {CONFIRM} with --execute.",
            file=sys.stderr,
        )
        sys.stdout.write(format_summary(count, oldest, newest, "refused"))
        return 2
    if not args.backup:
        print("Refusing to delete sessions. Pass --backup pointing at a verified archive.", file=sys.stderr)
        sys.stdout.write(format_summary(count, oldest, newest, "refused"))
        return 1
    try:
        manifest = verify_backup(args.backup)
        manifest_is_fresh(manifest, args.max_age_minutes)
        deleted = delete_sessions(args.db)
    except (BackupError, BackupVerificationError, sqlite3.Error) as exc:
        print(f"Session delete was not run: {exc}", file=sys.stderr)
        count, oldest, newest = session_summary(args.db)
        sys.stdout.write(format_summary(count, oldest, newest, "refused"))
        return 1
    print(f"sessions_deleted={deleted}")
    count, oldest, newest = session_summary(args.db)
    sys.stdout.write(format_summary(count, oldest, newest, "executed"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
