"""Threat 22 (docs/SECURITY.md): an advice cannot be erased or backdated to embellish the reliability."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from goulot.api.context import AppContext
from goulot.cli.main import main
from goulot.db.connection import Database
from goulot.ledger import chain
from tests.conftest import setup_vault

T0 = datetime(2026, 10, 18, 12, tzinfo=UTC)


def fill(ctx: AppContext, n: int = 5) -> None:
    with ctx.db.transaction() as conn:
        for i in range(n):
            chain.append(conn, "verdict", {"verdict": "pot" if i % 2 else "ras", "x_weeks": i}, material_id="m", at=T0)


def raw(path: object) -> sqlite3.Connection:
    """An attacker with direct access to the file, outside the app."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def test_ledger_append_only(ctx: AppContext) -> None:
    fill(ctx)
    with ctx.db.transaction() as conn:
        assert chain.verify(conn).ok
    for statement in ("UPDATE ledger SET content = '{}' WHERE seq = 2", "DELETE FROM ledger WHERE seq = 2",
                      "UPDATE ledger SET at = '2020-01-01T00:00:00+00:00'"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"), ctx.db.transaction() as conn:
            conn.execute(statement)
    # Migrations run again cannot alter it either: the triggers are part of the schema.
    assert Database(ctx.settings.db_path).migrate() == []
    with ctx.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 5 and chain.verify(conn).ok
    # The code offers no way to change an entry: only append.
    assert not any(name in dir(chain) for name in ("update", "delete", "edit", "remove"))


def test_ledger_tamper_detected(ctx: AppContext, capsys: pytest.CaptureFixture[str]) -> None:
    setup_vault(ctx)
    fill(ctx)
    with ctx.db.transaction() as conn:
        chain.append(conn, "verdict", {"verdict": "ant"}, material_id="m", private={"window_weeks": 1.5},
                     vault=ctx.vault, at=T0)
    db_file = ctx.settings.db_path
    attacker = raw(db_file)
    attacker.execute("DROP TRIGGER ledger_no_update")
    attacker.execute("DROP TRIGGER ledger_no_delete")

    attacker.execute("UPDATE ledger SET content = replace(content, 'pot', 'ant') WHERE seq = 2")
    with ctx.db.connect() as conn:
        result = chain.verify(conn)
    assert not result.ok and result.at_seq == 2 and "modifiée" in result.problem
    attacker.execute("UPDATE ledger SET content = replace(content, 'ant', 'pot') WHERE seq = 2")  # restored

    attacker.execute("DELETE FROM ledger WHERE seq = 3")
    with ctx.db.connect() as conn:
        result = chain.verify(conn)
    assert not result.ok and result.at_seq == 3 and "suppression" in result.problem

    # The CLI says it too, with a failing exit code.
    import os

    os.environ["GOULOT_HOME"] = str(ctx.settings.home)
    try:
        with pytest.raises(SystemExit) as exit_info:
            main(["ledger", "verify"])
    finally:
        del os.environ["GOULOT_HOME"]
    assert "altéré" in str(exit_info.value)


def test_private_fields_tamper_detected(ctx: AppContext) -> None:
    setup_vault(ctx)
    with ctx.db.transaction() as conn:
        chain.append(conn, "verdict", {"verdict": "ant"}, material_id="m", private={"window_weeks": 1.5},
                     vault=ctx.vault, at=T0)
    attacker = raw(ctx.settings.db_path)
    attacker.execute("DROP TRIGGER ledger_no_update")
    blob = bytearray(attacker.execute("SELECT private_blob FROM ledger WHERE seq = 1").fetchone()[0])
    blob[-1] ^= 0x01
    attacker.execute("UPDATE ledger SET private_blob = ? WHERE seq = 1", (bytes(blob),))
    with ctx.db.connect() as conn:
        result = chain.verify(conn)
    assert not result.ok and "privés" in result.problem
