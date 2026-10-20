"""Phase 4, threats 23 to 26 (docs/SECURITY.md): Ma chaîne, supplier signals, quotes and personal verdicts never
leave the vault; a closed vault shows nothing that would betray the user's suppliers; the CSV import stays in
memory."""

from __future__ import annotations

import json
import logging
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from goulot.api.context import AppContext
from goulot.collect.runner import run_cycle
from goulot.db.backup import backup
from goulot.llm.batch import ReplayBatchBackend
from goulot.llm.budget import watch_mode
from goulot.net.allowlist import Allowlist
from goulot.net.client import build_client
from goulot.pipeline import cycle
from goulot.private.mychain import CSV_TEMPLATE
from goulot.security.logs import setup_logging
from tests.conftest import api_routes, db_bytes, fill_path, unsafe_headers
from tests.private_fixtures import CANARIES, FIXTURE, raw_contains_canary, validated_material
from tests.test_my_chain import add_event, impact

NOW = datetime(2026, 11, 3, 9, tzinfo=UTC)
CHAIN_MARKS = re.compile(r"ta cha[iî]ne|ma cha[iî]ne|tes fournisseurs|ton fournisseur", re.IGNORECASE)


def _private_world(client: TestClient, ctx: AppContext, opened: tuple[str, str]) -> dict[str, Any]:
    """Ma chaîne, coverage, a quote, a signal and two events (one found only through Ma chaîne), through the API."""
    csrf, token = opened
    headers = unsafe_headers(csrf, token)
    material_id = validated_material(ctx)
    r = client.put(f"/api/private/materials/{material_id}/my-chain", json=FIXTURE["chain"], headers=headers)
    assert r.status_code == 200 and r.json()["revision"] == 1, r.text
    assert client.put(f"/api/private/coverage/{material_id}", json=FIXTURE["coverage"],
                      headers=headers).status_code == 200
    assert client.post(f"/api/private/materials/{material_id}/quotes", json=FIXTURE["quote"],
                       headers=headers).status_code == 200
    signal = {"type": "allocation", "site_id": "beta", "date": "2026-11-02", "comment": "CANARY-NOTE-8P4R"}
    assert client.post(f"/api/private/materials/{material_id}/signals", json=signal, headers=headers).status_code == 200
    private_event = add_event(ctx, material_id, lat=29.9, lon=121.6, event_type="natural_disaster",
                              title="Séisme sur la côte", analysis=impact(0, duration_high=90))
    public_event = add_event(ctx, material_id, nodes=("de_acrylonitrile",), analysis=impact(2, duration_high=60),
                             title="Arrêt d'une usine en Allemagne")
    return {"material_id": material_id, "private_event": private_event, "public_event": public_event,
            "headers": headers}


def test_private_canary_never_leaves(client: TestClient, ctx: AppContext, opened: tuple[str, str],
                                     caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    log_file = setup_logging(ctx.settings.log_dir, level=logging.DEBUG)
    world = _private_world(client, ctx, opened)
    # Outgoing requests of a collection cycle, with the in-memory rules of Ma chaîne.
    sent: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(f"{request.method} {request.url} {request.content.decode('utf-8', 'replace')}")
        if request.url.host == "api.gdeltproject.org":
            return httpx2.Response(200, json={"articles": []})
        return httpx2.Response(404, content=b"{}")

    hosts = {"api.gdeltproject.org", "news.google.com", "www.gdacs.org", "earthquake.usgs.gov",
             "www.nhc.noaa.gov", "services9.arcgis.com", "www.pegelonline.wsv.de", "www.federalregister.gov"}
    http = build_client(Allowlist(hosts), transport=httpx2.MockTransport(handler))
    run_cycle(ctx.db, http, lambda _: None, NOW, private=cycle.private_watch(ctx))
    assert any("Anvers" in s for s in sent)  # the private rules did run, at the « ville et matière » level
    # The rest of the cycle: matching, analysis of the event that weighs, verdicts, outcomes, alerts.
    replay = ReplayBatchBackend()
    ctx.llm_batch = lambda: replay
    cycle.process(ctx, NOW)
    prompts = json.dumps([req for batch in replay.batches.values() for req in batch], ensure_ascii=False)
    assert world["private_event"] in prompts  # it weighs for the user: it was sent to the analysis…
    with ctx.db.connect() as conn:
        alerts = " ".join(r[0] for r in conn.execute("SELECT text FROM alerts"))
        personal = conn.execute("SELECT * FROM verdicts WHERE scope = 'personal' AND current = 1").fetchone()
    assert personal is not None and personal["x_weeks"] is None and personal["explanation"] == ""
    assert "pour ton approvisionnement" in alerts and "Motif dans l'app" in alerts
    cycle.notify(ctx, NOW, [], watch_mode(ctx.budget))
    # …with nothing private: not in the requests, the prompts, the alerts, the logs, the public ledger.
    reliability = client.get("/api/reliability").text
    for where, text in (("requests", " ".join(sent)), ("prompts", prompts), ("alerts", alerts),
                        ("log records", caplog.text), ("log file", log_file.read_text(encoding="utf-8")),
                        ("public ledger", reliability)):
        assert not raw_contains_canary(text), where
    # Nor in clear at rest: database, WAL, backup.
    with ctx.db.connect() as holder:
        holder.execute("SELECT 1")
        assert not raw_contains_canary(db_bytes(ctx.settings.db_path))
        backup_file = backup(ctx.db, ctx.settings.backup_dir)
    assert not raw_contains_canary(backup_file.read_bytes())
    # The vault, open, does give them back.
    mine = client.get(f"/api/private/materials/{world['material_id']}/my-chain", headers=world["headers"]).text
    assert "CANARY-SITE-7F3A" in mine and "63.417" in mine


def test_locked_view_leaks_nothing(client: TestClient, ctx: AppContext, opened: tuple[str, str]) -> None:
    world = _private_world(client, ctx, opened)
    cycle.process(ctx, NOW)
    params = {"material_id": world["material_id"], "event_id": world["private_event"], "instrument_id": "eia:x",
              "case_id": "00000000-0000-0000-0000-000000000000", "job_id": "00000000-0000-0000-0000-000000000000",
              "node_key": "beta", "quote_id": "x", "ref": world["material_id"]}
    routes = sorted({r.path for r in api_routes(client.app) if "GET" in r.methods and r.path.startswith("/api/")
                     and not r.path.startswith("/api/private/")})
    assert "/api/markets" in routes and "/api/materials/{material_id}/sheet" in routes
    for vault_open in (False, True):
        headers = {"X-Vault-Token": opened[1]} if vault_open else {}
        if not vault_open:
            client.post("/api/vault/lock", headers=unsafe_headers(opened[0]))
        for path in routes:
            url = fill_path(path, params)
            r = client.get(url, headers=headers)
            body = r.text
            assert not raw_contains_canary(body), (url, vault_open)
            assert world["private_event"] not in body, (url, vault_open)
            assert not CHAIN_MARKS.search(body), (url, vault_open)
    # What a closed vault does show: the personal verdict's label and window, « motif dans le coffre », no X.
    sheet = client.get(f"/api/materials/{world['material_id']}/sheet").json()
    personal = sheet["personal"]
    assert personal is not None and personal["reason"] == "motif dans le coffre"
    assert set(personal) == {"verdict", "label", "window_weeks", "reason"} and "semaine" not in personal["label"]
    assert world["public_event"] in json.dumps(sheet)  # public events stay visible
    # And every private route refuses without the vault token.
    assert client.get(f"/api/private/materials/{world['material_id']}/personal").status_code == 403


def test_csv_import_in_memory(client: TestClient, ctx: AppContext, opened: tuple[str, str],
                              monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    csrf, token = opened
    material_id = validated_material(ctx)

    def no_file(*_: object, **__: object) -> None:
        raise AssertionError("the CSV import must never touch the disk")

    for name in ("SpooledTemporaryFile", "TemporaryFile", "NamedTemporaryFile", "mkstemp", "mkdtemp"):
        monkeypatch.setattr(tempfile, name, no_file)
    csv_text = CSV_TEMPLATE.replace("Usine Exemple", "CANARY-SITE-7F3A")
    r = client.post(f"/api/private/materials/{material_id}/my-chain/import", json={"csv": csv_text},
                    headers=unsafe_headers(csrf, token))
    assert r.status_code == 200, r.text
    preview = r.json()
    assert not preview["errors"] and preview["chain"]["sites"][1]["name"] == "CANARY-SITE-7F3A"
    assert preview["check"]["errors"] == []
    # A preview saves nothing: the user checks it, then saves it like any edit.
    with ctx.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM private_chain").fetchone()[0] == 0
    assert not raw_contains_canary(db_bytes(ctx.settings.db_path))
    # More than 1 MB is refused before being read; a multipart upload is not accepted at all.
    big = {"csv": "a;b\n" * 300_000}
    assert client.post(f"/api/private/materials/{material_id}/my-chain/import", json=big,
                       headers=unsafe_headers(csrf, token)).status_code == 413
    files = {"file": ("ma_chaine.csv", CSV_TEMPLATE.encode(), "text/csv")}
    upload = client.post(f"/api/private/materials/{material_id}/my-chain/import", files=files,
                         headers=unsafe_headers(csrf, token))
    assert upload.status_code == 422
    assert set(CANARIES) >= {"CANARY-SITE-7F3A"}
