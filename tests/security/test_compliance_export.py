"""Phase 6, threat 23 (docs/SECURITY.md): a compliance report never leaks what the vault holds, a report on Ma
chaîne needs the vault open and leaves no copy in clear, and an export makes no network call and no AI call."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from goulot.api.context import AppContext
from goulot.compliance import report as compliance_report
from goulot.compliance import templates
from goulot.db.backup import backup
from tests.conftest import api_routes, db_bytes, login, unsafe_headers
from tests.private_fixtures import FIXTURE, raw_contains_canary, validated_material

PROFILE_CANARY = "CANARY-SIGNATORY-4N7T"


def _body(material_id: str, basis: str = "typical") -> dict[str, Any]:
    return {"template": "article24", "material_ids": [material_id], "basis": basis, "company": "Fictive",
            "signatory_name": "A", "signatory_role": "B", "reviewed": True}


def _private_world(client: TestClient, ctx: AppContext, opened: tuple[str, str]) -> tuple[str, dict[str, str]]:
    csrf, token = opened
    headers = unsafe_headers(csrf, token)
    material_id = validated_material(ctx)
    assert client.put(f"/api/private/materials/{material_id}/my-chain", json=FIXTURE["chain"],
                      headers=headers).status_code == 200
    assert client.put(f"/api/private/coverage/{material_id}", json=FIXTURE["coverage"],
                      headers=headers).status_code == 200
    profile = {"company": PROFILE_CANARY, "signatory_name": PROFILE_CANARY, "signatory_role": "x",
               "measures": {material_id: {"choices": ["autre"], "comment": "CANARY-NOTE-8P4R"}}}
    assert client.put("/api/private/compliance/profile", json=profile, headers=headers).status_code == 200
    return material_id, headers


def test_private_export_requires_vault(client: TestClient, ctx: AppContext, opened: tuple[str, str]) -> None:
    material_id, headers = _private_world(client, ctx, opened)
    csrf = opened[0]
    private_routes = [r for r in api_routes(client.app) if r.path.startswith("/api/private/compliance")]
    assert {r.path for r in private_routes} == {"/api/private/compliance/profile", "/api/private/compliance/preview",
                                               "/api/private/compliance/export"}
    for r in private_routes:
        for method in r.methods:
            res = client.request(method, r.path, json=_body(material_id, "my_chain"), headers=unsafe_headers(csrf))
            assert res.status_code == 403, (method, r.path)
            assert not raw_contains_canary(res.text)
    # The public routes refuse Ma chaîne, even with a vault token sent along.
    for path in ("/api/compliance/preview", "/api/compliance/export"):
        res = client.post(path, json=_body(material_id, "my_chain"), headers=headers)
        assert res.status_code == 403 and not raw_contains_canary(res.text)
    with ctx.db.connect() as conn, pytest.raises(PermissionError):
        compliance_report.build(conn, compliance_report.Request(
            template=templates.get("article24"), material_ids=[material_id], basis="my_chain"), datetime.now(UTC))
    with ctx.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM compliance_exports WHERE basis = 'my_chain'").fetchone()[0] == 0
    # Vault open: the report on Ma chaîne is marked « Confidentiel » and holds the user's sites.
    res = client.post("/api/private/compliance/export", json=_body(material_id, "my_chain"), headers=headers)
    assert res.status_code == 200, res.text
    assert res.headers["cache-control"] == "no-store"
    report = res.json()["report"]
    assert report["confidential"] is True and report["confidential_label"].startswith("Confidentiel")
    assert "CANARY-SITE-7F3A" in json.dumps(report, ensure_ascii=False)


def test_export_no_private_fields(client: TestClient, ctx: AppContext, opened: tuple[str, str]) -> None:
    material_id, headers = _private_world(client, ctx, opened)
    # Public export (typical chain): no mitigation, coverage, profile nor Ma chaîne value, in any file.
    res = client.post("/api/compliance/export", json=_body(material_id), headers=unsafe_headers(opened[0]))
    assert res.status_code == 200, res.text
    assert not raw_contains_canary(res.text) and PROFILE_CANARY not in res.text
    public = res.json()
    assert public["report"]["materials"][0]["mitigation"]["status"] == "coffre fermé"
    assert "my_chain" not in public["report"]["materials"][0]
    # Vault open, Ma chaîne: produced on the fly; only its fingerprint is kept.
    res = client.post("/api/private/compliance/export", json=_body(material_id, "my_chain"), headers=headers)
    assert res.status_code == 200
    private = res.json()
    assert raw_contains_canary(private["files"]["json"]["content"])  # the report does hold them…
    fingerprint = private["report"]["fingerprint"]
    with ctx.db.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM compliance_exports")]
        ledger_rows = [dict(r) for r in conn.execute("SELECT seq, content FROM ledger WHERE kind = 'export'")]
    assert [r["fingerprint"] for r in rows][-1] == fingerprint
    kept = json.dumps(rows) + json.dumps(ledger_rows)
    assert not raw_contains_canary(kept) and PROFILE_CANARY not in kept  # …the database keeps none of them
    assert json.loads(ledger_rows[-1]["content"])["scope"] == "personal"  # not in the market column
    reliability = client.get("/api/reliability").text
    assert fingerprint not in reliability
    # Nor in clear at rest: database, WAL, backup, the app's home directory.
    with ctx.db.connect() as holder:
        holder.execute("SELECT 1")
        raw = db_bytes(ctx.settings.db_path)
        assert not raw_contains_canary(raw) and PROFILE_CANARY.encode() not in raw
        backup_file = backup(ctx.db, ctx.settings.backup_dir)
    assert not raw_contains_canary(backup_file.read_bytes())
    for path in ctx.settings.home.rglob("*"):
        if path.is_file() and path != ctx.settings.db_path and not path.name.endswith(("-wal", "-shm")):
            assert not raw_contains_canary(path.read_bytes()), path


def test_export_offline(client: TestClient, ctx: AppContext, opened: tuple[str, str],
                        monkeypatch: pytest.MonkeyPatch) -> None:
    material_id, headers = _private_world(client, ctx, opened)

    def forbidden(*_: object, **__: object) -> Any:
        raise AssertionError("network or AI call during an export")

    ctx.llm_backend = forbidden
    ctx.llm_batch = forbidden
    ctx.http_client = forbidden
    monkeypatch.setattr("goulot.net.client.build_client", forbidden)
    monkeypatch.setattr("goulot.llm.client.build_anthropic_client", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    before = ctx.db
    with before.connect() as conn:
        calls = conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
    for path, basis, h in (("/api/compliance/preview", "typical", unsafe_headers(opened[0])),
                           ("/api/compliance/export", "typical", unsafe_headers(opened[0])),
                           ("/api/private/compliance/preview", "my_chain", headers),
                           ("/api/private/compliance/export", "my_chain", headers)):
        res = client.post(path, json=_body(material_id, basis), headers=h)
        assert res.status_code == 200, (path, res.text)
    with before.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == calls


def test_compliance_routes_need_a_session(client: TestClient, ctx: AppContext) -> None:
    for method, path in (("GET", "/api/compliance/options"), ("POST", "/api/compliance/preview"),
                         ("POST", "/api/compliance/export")):
        assert client.request(method, path, json={}).status_code in (401, 403)  # no session, or no Origin
    csrf = login(client, ctx)
    assert client.post("/api/compliance/export", json={}, headers={"X-CSRF-Token": "bad"}).status_code == 403
    assert client.post("/api/compliance/export", json={"template": "article24", "material_ids": ["x" * 36]},
                       headers=unsafe_headers(csrf)).status_code == 404
