"""Daily backups with SQLite's backup API; 7 kept. Private data stays encrypted in them, and the master key
is never in the database (docs/SECURITY.md, files and data)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from goulot.db.connection import Database
from goulot.security.fs import ensure_private_dir, make_private, touch_private
from goulot.settings import BACKUPS_KEPT

PREFIX = "goulot-"
SUFFIX = ".sqlite"


def backup(db: Database, backup_dir: Path, keep: int = BACKUPS_KEPT, label: str = "daily") -> Path:
    ensure_private_dir(backup_dir)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    target = backup_dir / f"{PREFIX}{label}-{stamp}{SUFFIX}"
    touch_private(target)
    with db.connect() as src:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode = DELETE")
        finally:
            dst.close()
    make_private(target)
    if label == "daily":
        rotate(backup_dir, keep)
    return target


def list_backups(backup_dir: Path, label: str = "daily") -> list[Path]:
    if not backup_dir.exists():
        return []
    return sorted(backup_dir.glob(f"{PREFIX}{label}-*{SUFFIX}"))


def rotate(backup_dir: Path, keep: int) -> None:
    for old in list_backups(backup_dir)[:-keep]:
        old.unlink()


def last_backup_age(backup_dir: Path, now: float) -> float | None:
    backups = list_backups(backup_dir)
    if not backups:
        return None
    return now - backups[-1].stat().st_mtime
