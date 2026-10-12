#!/usr/bin/env python3
"""Create a consistent backup. Run from the repository root or by absolute path.

    python /app/scripts/backup.py --data-dir /data --staging-dir /tmp --stdout-base64

The archive is written under the staging directory, not under the data directory.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backup_lib import main


if __name__ == "__main__":
    raise SystemExit(main())
