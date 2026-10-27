"""Deleting a material (docs/SPEC.md, section 2): nothing of it is left but the ledger and the public articles, the
ledger still verifies, its private records go with it, and the vault is required. Offline."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from goulot.api.context import AppContext
from goulot.ledger import chain
from goulot.private import records
from tests.authenticator import SoftAuthenticator
from tests.conftest import db_bytes, enroll, login, setup_vault, unlock, unsafe_headers
from tests.private_fixtures import CANARIES, FIXTURE, raw_contains_canary
from tests.test_screens_api import prepared

KEPT = {"ledger", "articles"}  # rule 12, and the public articles until their usual purge


def material_tables(conn: object) -> dict[str, str]:
    """table -> column naming a material, for every table of the base."""
    out = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):  # type: ignore[attr-defined]
        cols = [r[1] for r in conn.execute("SELECT * FROM pragma_table_info(?)", (name,))]  # type: ignore[attr-defined]
        col = next((c for c in ("material_id", "material_ref") if c in cols), "id" if name == "materials" else None)
        if col:
            out[name] = col
    return out


def test_delete_leaves_only_the_ledger_and_public_articles(client: TestClient, ctx: AppContext,
                                                           authenticator: SoftAuthenticator) -> None:
    material_id, _ = prepared(ctx)  # sets the clock: the vault is opened after it
    setup_vault(ctx)
    csrf = login(client, ctx)
    enroll(client, ctx, csrf, authenticator)
    token = unlock(client, csrf, authenticator)
    headers = unsafe_headers(csrf, token)
    assert client.put(f"/api/private/materials/{material_id}/my-chain", json=FIXTURE["chain"],
                      headers=headers).status_code == 200
    assert client.put(f"/api/private/coverage/{material_id}", json=FIXTURE["coverage"],
                      headers=headers).status_code == 200
    with ctx.db.transaction() as conn:
        conn.execute("INSERT INTO llm_calls (at, budget, purpose, model, mode, material_id, cost_usd) "
                     "VALUES ('2026-10-18T12:00:00+00:00', 'watch', 'triage', 'claude-haiku-5-5', 'batch', ?, 0.25)",
                     (material_id,))
        spent = conn.execute("SELECT SUM(cost_usd) FROM llm_calls").fetchone()[0]
        ledger_before = conn.execute("SELECT COUNT(*) FROM ledger WHERE material_id = ?", (material_id,)).fetchone()[0]
        articles_before = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    assert ledger_before > 0

    url = f"/api/private/materials/{material_id}/delete"
    assert client.post(url, json={"name": "acrylonitrile"}, headers=unsafe_headers(csrf)).status_code == 403  # vault
    r = client.post(url, json={"name": "méthanol"}, headers=headers)
    assert r.status_code == 409 and "nom tapé" in r.json()["detail"]
    assert client.post(url, json={"name": "  Acrylonitrile "}, headers=headers).status_code == 200

    with ctx.db.connect() as conn:
        left = {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE {c} = ?", (material_id,)).fetchone()[0]  # noqa: S608  # nosec B608
                for t, c in material_tables(conn).items() if t not in KEPT}
        assert {t: n for t, n in left.items() if n} == {}
        assert conn.execute("SELECT COUNT(*) FROM ledger WHERE material_id = ?", (material_id,)).fetchone()[0] \
            == ledger_before and chain.verify(conn).ok
        assert conn.execute("SELECT SUM(cost_usd) FROM llm_calls").fetchone()[0] == spent  # the month keeps its cost
        assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == articles_before
        assert all(not records.read_all(conn, ctx.vault, kind, material_id) for kind in records.KINDS)
    assert not raw_contains_canary(db_bytes(ctx.settings.db_path))
    assert all(c not in json.dumps(client.get("/api/markets").json()) for c in CANARIES)
    reliability = client.get("/api/reliability").json()
    deleted = [a for a in reliability["advices"] if a["material_deleted"]]
    assert deleted and all(a["material_name"] == "matière supprimée" for a in deleted)
    assert reliability["ledger"]["ok"]
