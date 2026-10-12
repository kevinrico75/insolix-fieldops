#!/usr/bin/env python3
"""Re-open a backup archive and exit non-zero when it does not match its manifest.

    python scripts/verify_backup.py C:\\path\\to\\insolix-backup.tar.gz
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backup_lib import BackupVerificationError, verify_backup


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("Usage: python scripts/verify_backup.py ARCHIVE.tar.gz", file=sys.stderr)
        return 2
    try:
        manifest = verify_backup(args[0])
    except BackupVerificationError as exc:
        print("INSOLIX backup verification failed", file=sys.stderr)
        for item in exc.mismatches:
            print(item, file=sys.stderr)
        return 1
    print(
        f"INSOLIX backup ok files={manifest.get('file_count')} "
        f"tables={len(manifest.get('tables') or {})} created_at={manifest.get('created_at')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
