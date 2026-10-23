"""Copies the database to BACKUP_DIR (default: data/backups) and keeps the newest 14.

Uses SQLite's backup API, so it's safe while the site and bot are running.
The copy holds the same encrypted secrets as the live database: it's only
readable with the encryption key, so keep the key backed up somewhere else.

Run: python backup.py   (the server runs it nightly)
"""
import os
import sqlite3
import time
from pathlib import Path

import config

KEEP = 14


def backup() -> Path:
    folder = Path(os.environ.get("BACKUP_DIR") or config.DB_PATH.parent / "backups")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"buffsquad-{time.strftime('%Y-%m-%d-%H%M%S')}.db"
    source = sqlite3.connect(config.DB_PATH)
    dest = sqlite3.connect(target)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()
    for old in sorted(folder.glob("buffsquad-*.db"))[:-KEEP]:
        old.unlink()
    return target


if __name__ == "__main__":
    print(f"Backed up to {backup()}")
