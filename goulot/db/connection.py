"""SQLite in WAL mode with numbered SQL migrations and `secure_delete` (docs/SPEC.md, section 2)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from goulot.security.fs import ensure_private_dir, make_private

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA secure_delete = ON")
    conn.execute("PRAGMA busy_timeout = 5000")


class Database:
    """Opens a short-lived connection per unit of work; safe across threads."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        ensure_private_dir(self.path.parent)
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=5.0)
        _configure(conn)
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()
            self.fix_permissions()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()
            self.fix_permissions()

    def fix_permissions(self) -> None:
        for suffix in ("", "-wal", "-shm", "-journal"):
            make_private(Path(str(self.path) + suffix))

    def migrate(self, migrations_dir: Path = MIGRATIONS_DIR) -> list[int]:
        """Apply pending migrations in order. Returns the versions applied."""
        applied: list[int] = []
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            for path in sorted(migrations_dir.glob("*.sql")):
                version = int(path.name.split("_", 1)[0])
                if version in done:
                    continue
                script = path.read_text(encoding="utf-8")
                conn.executescript("BEGIN;\n" + script)  # the transaction stays open after the script
                try:
                    conn.execute("INSERT INTO schema_migrations VALUES (?, ?)", (version, utcnow()))
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                applied.append(version)
        return applied

    def pending_migrations(self, migrations_dir: Path = MIGRATIONS_DIR) -> list[int]:
        with self.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")} if exists else set()
        return [int(p.name.split("_", 1)[0]) for p in sorted(migrations_dir.glob("*.sql"))
                if int(p.name.split("_", 1)[0]) not in done]


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, utcnow()),
    )
