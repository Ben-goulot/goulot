"""Restore of a switch backup without losing the ledger nor the user's inputs (docs/UPDATES.md, rules).

The restored base is built apart, from a copy of the switch backup:
- the ledger entries written since the backup are appended as they are (same rows, same hashes): the backup's
  ledger must be a prefix of the newer one, and the chain is verified afterwards;
- the user's inputs and the security state are carried over as they are now (ciphertexts copied untouched, no
  key needed): settings, security journal, sessions, wrapped keys, passkeys (a revoked passkey stays revoked, a
  new recovery key stays the only one), coverage, Ma chaîne revisions, signals, quotes and purchases. A record
  erased since the backup stays erased;
- one-time tokens, vault tokens and WebAuthn challenges are cleared: the vault is closed after a rollback.
Only then does the restored base replace the live one. On any failure nothing is changed. Articles, events and
computed verdicts written since the backup are lost; the watch catches up. Table names come from the fixed lists
below, column names from the schemas themselves (checked identifiers): SQL is assembled from nothing else.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from goulot.ledger.chain import verify
from goulot.security.fs import ensure_private_dir, make_private, touch_private

CARRIED_TABLES = ("settings", "audit_log", "sessions", "vault_keys", "vault_passkeys", "vault_unlock_failures",
                  "user_coverage", "private_chain", "user_signals", "user_quotes", "user_purchases")
CLEARED_TABLES = ("launch_tokens", "vault_tokens", "vault_challenges")
SIDECARS = ("-wal", "-shm", "-journal")
IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")


class RestoreError(Exception):
    """The rollback stops without changing anything. French message: it reaches the user."""


def _ro(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


def _tables(conn: sqlite3.Connection, schema: str) -> set[str]:
    return {r[0] for r in conn.execute(f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table'")}  # noqa: S608  # nosec B608


def _columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    columns = [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")]
    if not all(IDENTIFIER.fullmatch(c) for c in columns):
        raise RestoreError(f"colonne inattendue dans {table}")
    return columns


def _copy_db(source: Path, target: Path) -> None:
    touch_private(target)
    src = sqlite3.connect(_ro(source), uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode = DELETE")
    finally:
        src.close()
        dst.close()
    make_private(target)


def build_restored(live: Path, switch_backup: Path, target: Path) -> int:
    """Write into `target` the switch backup plus what must survive from `live`. Returns the ledger entries
    carried over."""
    _copy_db(switch_backup, target)
    conn = sqlite3.connect(target.resolve().as_uri(), isolation_level=None, uri=True)
    try:
        conn.execute("ATTACH DATABASE ? AS newer", (_ro(live),))
    except sqlite3.Error:
        conn.close()
        raise
    try:
        conn.execute("BEGIN IMMEDIATE")
        old_head = conn.execute("SELECT seq, hash FROM main.ledger ORDER BY seq DESC LIMIT 1").fetchone()
        if old_head is not None:
            same = conn.execute("SELECT hash FROM newer.ledger WHERE seq = ?", (old_head[0],)).fetchone()
            if same is None or same[0] != old_head[1]:
                raise RestoreError("le registre de la sauvegarde ne correspond pas au registre actuel")
        old_seq = old_head[0] if old_head else 0
        cols = ", ".join(_columns(conn, "main", "ledger"))
        carried = conn.execute(
            f"INSERT INTO main.ledger ({cols}) SELECT {cols} FROM newer.ledger WHERE seq > ? ORDER BY seq",  # noqa: S608  # nosec B608
            (old_seq,),
        ).rowcount
        main_tables, newer_tables = _tables(conn, "main"), _tables(conn, "newer")
        for table in CARRIED_TABLES:
            if table not in main_tables or table not in newer_tables:
                continue
            newer_cols = set(_columns(conn, "newer", table))
            common = ", ".join(c for c in _columns(conn, "main", table) if c in newer_cols)
            conn.execute(f"DELETE FROM main.{table}")  # noqa: S608  # nosec B608
            conn.execute(f"INSERT INTO main.{table} ({common}) SELECT {common} FROM newer.{table}")  # noqa: S608  # nosec B608
        for table in CLEARED_TABLES:
            if table in main_tables:
                conn.execute(f"DELETE FROM main.{table}")  # noqa: S608  # nosec B608
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()
        target.unlink(missing_ok=True)
        raise
    conn.execute("DETACH DATABASE newer")
    conn.row_factory = sqlite3.Row
    try:
        result = verify(conn)
        newer = sqlite3.connect(_ro(live), uri=True)
        newer.row_factory = sqlite3.Row
        try:
            expected = verify(newer)
        finally:
            newer.close()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    if not result.ok or result.head != expected.head or result.entries != expected.entries or integrity != "ok":
        target.unlink(missing_ok=True)
        raise RestoreError("le registre ne se réinscrit pas à l'identique : retour arrière annulé")
    make_private(target)
    return carried


def checkpoint(db_path: Path) -> None:
    """Fold the WAL into the base file (the app is stopped)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def restore(live: Path, switch_backup: Path, keep_dir: Path, abandoned_version: str) -> int:
    """Replace the live base by the restored one; the abandoned base is kept in `keep_dir` (still encrypted)."""
    if not switch_backup.exists():
        raise RestoreError("sauvegarde de bascule introuvable : rien n'a été modifié")
    checkpoint(live)
    work = live.parent / "goulot.sqlite.restored"
    work.unlink(missing_ok=True)
    try:
        carried = build_restored(live, switch_backup, work)
    except sqlite3.Error as exc:
        work.unlink(missing_ok=True)
        raise RestoreError(f"réinscription impossible ({type(exc).__name__}) : rien n'a été modifié") from exc
    ensure_private_dir(keep_dir)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    kept = keep_dir / f"goulot-abandoned-{abandoned_version}-{stamp}.sqlite"
    _copy_db(live, kept)
    for suffix in SIDECARS:
        Path(str(live) + suffix).unlink(missing_ok=True)
    os.replace(work, live)
    make_private(live)
    return carried
