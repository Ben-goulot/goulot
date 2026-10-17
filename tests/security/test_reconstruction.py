"""Phase 2 threats 6 and 9 (docs/SECURITY.md): booby-trapped pages in the research, user data in prompts."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from goulot.api.context import AppContext
from goulot.chain import service, store
from goulot.chain.identify import identify
from goulot.llm.backend import ReplayBackend
from tests import chain_fixtures as fx
from tests.conftest import unsafe_headers
from tests.test_chain import mock_http

CANARIES = ("CANARY-QUOTE-4217.93", "CANARY-SITE-7F3A", "37.29", "63.417", "613")


def _reconstruct(ctx: AppContext, backend: ReplayBackend) -> str:
    identity = identify(mock_http(), fx.CAS)
    with ctx.db.transaction() as conn:
        material_id = store.create_material(conn, identity.as_dict())
    service.reconstruct(service.Deps(ctx.db, ctx.budget, backend, None), material_id,
                        service.start_job(ctx.db, material_id))
    return material_id


def test_prompt_injection_fixtures(client: TestClient, ctx: AppContext, opened: tuple[str, str]) -> None:
    csrf, _ = opened
    trapped = fx.structured_chain()
    trapped["injection_suspected"] = True
    trapped["nodes"][6]["source_ids"] = ["src77"]  # a source that was never provided
    trapped["nodes"][6]["confidence"] = "high"
    backend = ReplayBackend(responses=fx.replay_responses(trapped))
    material_id = _reconstruct(ctx, backend)
    (_, research), (_, structuring), (_, context) = backend.requests
    # Research: the only tool is web search, capped; no client tool, nothing with side effects.
    assert research["tools"] == [{"type": "web_search_20260318", "name": "web_search", "max_uses": 20,
                                  "allowed_callers": ["direct"]}]
    # Structuring calls: no tool at all; the trapped page cannot close the report tag nor reach them as a URL.
    for params in (structuring, context):
        assert "tools" not in params
        user_text = params["messages"][0]["content"]
        assert user_text.count("</research_report>") == 1 and user_text.count("<research_report>") == 1
        assert "&lt;/research_report&gt;" in user_text and "<system>" not in user_text
        assert "http" not in user_text and "phish" not in user_text
    # Sources: only https citations, kept by the code; the model never wrote a URL.
    with ctx.db.connect() as conn:
        urls = [r[0] for r in conn.execute("SELECT url FROM sources WHERE material_id = ?", (material_id,))]
        chain = store.load_chain(conn, material_id)
    assert urls and all(u.startswith("https://") for u in urls) and "http://insecure.example/x" not in urls
    # A node citing an unknown source drops to low confidence, with the reason shown.
    cn = next(n for n in chain["nodes"] if n["node_key"] == "cn_acrylonitrile")
    assert cn["confidence"] == "low" and any("source inconnue" in a for a in cn["anomalies"])
    # injection_suspected quarantines the chain: no validation without an explicit, informed confirmation.
    assert chain["injection_suspected"] is True
    h = unsafe_headers(csrf)
    r = client.post(f"/api/materials/{material_id}/validate", json={}, headers=h)
    assert r.status_code == 409 and "piégé" in r.json()["detail"]
    with ctx.db.connect() as conn:
        assert store.get_material(conn, material_id)["status"] == "review"  # type: ignore[index]
    r = client.post(f"/api/materials/{material_id}/validate", json={"acknowledge_injection": True}, headers=h)
    assert r.status_code == 200
    # Model texts are returned as data, never as markup to render.
    assert client.get(f"/api/materials/{material_id}/chain").headers["content-type"].startswith("application/json")


def test_no_user_data_in_prompts(client: TestClient, ctx: AppContext, opened: tuple[str, str]) -> None:
    csrf, vault_token = opened
    # Private coverage with canary values, and a non-private setting, exist before the reconstruction.
    r = client.put("/api/private/coverage/acrylonitrile",
                   json={"stock_weeks": 37.29, "supplier_lead_time_days": 613, "price_threshold_pct": 63.417},
                   headers=unsafe_headers(csrf, vault_token))
    assert r.status_code == 200, r.text
    with ctx.db.transaction() as conn:
        conn.execute("INSERT INTO settings VALUES ('buyer_note', 'CANARY-SITE-7F3A', 'now')")
    backend = ReplayBackend(responses=fx.replay_responses())
    _reconstruct(ctx, backend)
    assert len(backend.requests) == 3
    for _, params in backend.requests:
        sent = json.dumps(params, ensure_ascii=False)
        for canary in CANARIES:
            assert canary not in sent, canary
    # The material block holds exactly the whitelisted fields.
    material_block = backend.requests[0][1]["messages"][0]["content"]
    fields = [line.split(":", 1)[0] for line in material_block.splitlines()[1:-1]]
    assert fields == ["name", "cas", "formula", "purchased_form"]
    # The builder ignores any extra field it is given.
    extra = service.material_for_prompt({"name": "x", "cas": fx.CAS, "formula": "", "stock_weeks": 37.29,
                                         "quote": "CANARY-QUOTE-4217.93"})
    assert set(extra) == {"name", "cas", "formula", "purchased_form"}
