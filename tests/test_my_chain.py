"""Phase 4: Ma chaîne, its checks, exposure, hidden concentration, CSV import, local matching, in-memory watch rules,
personal verdict and its outcomes; the « Ma chaîne » cases of docs/SPEC.md, section 13."""

from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx2
import pytest

from goulot.api.context import AppContext
from goulot.collect.runner import DISRUPTION_TERMS, run_cycle
from goulot.ledger import chain as ledger_chain
from goulot.ledger import personal as ledger_personal
from goulot.ledger.rules import UNRESOLVED, Series
from goulot.net.allowlist import Allowlist
from goulot.net.client import build_client
from goulot.pipeline import assess
from goulot.pipeline.assess import Allowance
from goulot.pipeline.verdict import ANTICIPATE, POTENTIAL, RAS, EventInput
from goulot.pipeline.visibility import is_public
from goulot.private import cities, matching, records
from goulot.private import refresh as private_refresh
from goulot.private import service as private_service
from goulot.private.mychain import (
    CSV_TEMPLATE,
    Model,
    MyChain,
    check,
    market_concentration,
    my_concentration,
    parse_csv,
)
from goulot.private.personal import PersonalEvent, active_signals, compute, expiring
from goulot.private.watch import build
from goulot.settings import REPO_ROOT
from tests.private_fixtures import chain_data, doc, with_my_chain

NOW = datetime(2026, 11, 3, 9, tzinfo=UTC)
IMPACT: dict[str, Any] = json.loads(
    (REPO_ROOT / "schemas" / "examples" / "impact_assessment_v1.example.json").read_text(encoding="utf-8"))
COMMODITIES = {"acrylonitrile", "propylene", "ammonia"}


def impact(severity: int, commodity: str = "acrylonitrile", duration_high: float = 30) -> dict[str, Any]:
    a = copy.deepcopy(IMPACT)
    ci = a["commodity_impacts"][0]
    ci["commodity"], ci["severity"], ci["net_balance_effect"] = commodity, severity, "tightening"
    ci["expected_duration_days"]["high"] = duration_high
    a["hypotheses"] = []
    return a


def model_for(ctx: AppContext, material_id: str, shares: tuple[float, float] | None = None) -> Model:
    with ctx.db.connect() as conn:
        return Model(doc(shares), private_service.typical(conn, material_id))


def add_event(ctx: AppContext, material_id: str, *, nodes: tuple[str, ...] = (), entities: tuple[str, ...] = (),
              place: str = "", lat: float | None = None, lon: float | None = None, public_rule: bool = False,
              event_type: str = "industrial_outage", severity_hint: int = 2, title: str = "Incendie dans une usine",
              analysis: dict[str, Any] | None = None) -> str:
    event_id = f"EVT-2026-11-02-{uuid.uuid4().hex[:4]}"
    seen = (NOW - timedelta(days=1)).isoformat(timespec="seconds")
    with ctx.db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO articles (source, kind, url, url_hash, domain, title, first_seen_at, collected_at, rules_hit, "
            "materials, status) VALUES ('gdelt_doc', 'news', ?, ?, 'reuters.com', ?, ?, ?, ?, ?, 'grouped')",
            (f"https://reuters.com/{event_id}", uuid.uuid4().hex, title, seen, seen,
             "[1]" if public_rule else "[]", json.dumps([material_id])))
        conn.execute(
            "INSERT INTO triage (article_id, material_id, custom_id, model, prompt_version, schema_version, state, "
            "result, created_at) VALUES (?, ?, ?, 'm', 'p', 's', 'done', ?, ?)",
            (cur.lastrowid, material_id, f"tri_{event_id}", json.dumps({
                "relevant": True, "place": place or "unknown", "country_iso2": "unknown",
                "entities": [{"name": n, "kind": "site"} for n in entities]}), seen))
        conn.execute(
            "INSERT INTO events (id, material_id, event_type, event_status, origin, title, place, lat, lon, "
            "first_seen_at, last_seen_at, confirmation, severity_hint, best_lineage_reliability, updated_at) VALUES "
            "(?, ?, ?, 'ongoing', 'news', ?, ?, ?, ?, ?, ?, 'confirmed', ?, 0.9, ?)",
            (event_id, material_id, event_type, title, place, lat, lon, seen, seen, severity_hint, seen))
        conn.execute("INSERT INTO event_articles (event_id, article_id, lineage_key, reliability) VALUES (?, ?, 'l', "
                     "0.9)", (event_id, cur.lastrowid))
        for node in nodes:
            conn.execute("INSERT INTO event_links (event_id, material_id, node_key) VALUES (?, ?, ?)",
                         (event_id, material_id, node))
        if analysis is not None:
            conn.execute(
                "INSERT INTO analyses (id, event_id, material_id, model, prompt_version, schema_version, mode, state, "
                "signature, result, created_at) VALUES (?, ?, ?, 'm', 'p', 's', 'standard', 'done', 'x', ?, ?)",
                (str(uuid.uuid4()), event_id, material_id, json.dumps(analysis), seen))
    return event_id


def personal_for(ctx: AppContext, material_id: str, model: Model | None, signals: list[dict[str, Any]],
                 events: list[PersonalEvent], today: date = NOW.date(),  # noqa: B008
                 cover: dict[str, Any] | None = None) -> Any:
    with ctx.db.connect() as conn:
        typical = private_service.typical(conn, material_id)
    return compute(events, signals, model, 1, cover, commodities=COMMODITIES, instruments=set(),
                   delays=typical["delays"], last_prices={}, today=today)


# --- document and checks ----------------------------------------------------------------------------------------
def test_checks_report_and_never_correct(ctx: AppContext) -> None:
    material_id = with_my_chain(ctx)
    with ctx.db.connect() as conn:
        typical = private_service.typical(conn, material_id)
    assert check(doc(), typical).ok
    over = chain_data((80, 36.583))
    assert any("dépassent 100 %" in e for e in check(MyChain.model_validate(over), typical).errors)
    orphan = chain_data()
    orphan["sites"].append({"id": "lost", "name": "Site isolé", "city": "Lyon"})
    assert any("aucun chemin" in e for e in check(MyChain.model_validate(orphan), typical).errors)
    loop = chain_data()
    loop["links"].append({"source": "alpha", "target": "beta", "share_pct": 10})
    assert any("tourne en rond" in e for e in check(MyChain.model_validate(loop), typical).errors)
    unknown = chain_data()
    unknown["sites"][2]["node_key"] = "no_such_node"
    assert not check(MyChain.model_validate(unknown), typical).ok
    gap = chain_data()
    gap["sites"][2]["country_iso2"] = "FR"  # the adopted node is in Germany
    result = check(MyChain.model_validate(gap), typical)
    assert result.ok and any("écart signalé" in w for w in result.warnings)
    assert MyChain.model_validate(gap).sites[2].country_iso2 == "FR"  # reported, never corrected
    with pytest.raises(ValueError):
        MyChain.model_validate({"links": [{"source": "a", "target": "me", "share_pct": 120}]})


def test_first_entry_needs_a_configured_vault(ctx: AppContext) -> None:
    from tests.private_fixtures import validated_material

    material_id = validated_material(ctx)
    ctx.vault.create()  # recovery key not confirmed yet
    with pytest.raises(private_service.NotReady):
        private_service.save(ctx.db, ctx.vault, material_id, doc())


def test_revisions_are_append_only(ctx: AppContext) -> None:
    material_id = with_my_chain(ctx)
    revision, _ = private_service.save(ctx.db, ctx.vault, material_id, doc((80, 20)))
    assert revision == 2
    with ctx.db.connect() as conn:
        assert [r["revision"] for r in private_service.revisions(conn, ctx.vault, material_id)] == [1, 2]
        with pytest.raises(Exception, match="append-only"):
            conn.execute("UPDATE private_chain SET created_at = 'x'")


def test_exposure_flows_and_hidden_concentration(ctx: AppContext) -> None:
    material_id = with_my_chain(ctx)
    m = model_for(ctx, material_id)
    assert m.exposure["beta"] == pytest.approx(0.63417) and m.exposure["europe"] == pytest.approx(0.36583)
    assert m.weighs("beta") and m.weighs("europe")
    flows = {(f["source"], f["target"]): f["flow_pct"] for f in m.link_flows()}
    assert flows[("beta", "alpha")] == pytest.approx(63.42, abs=0.01)
    market, mine = market_concentration(m), my_concentration(m)
    assert market["top_country"] == "CN" and market["hhi"] == 40 * 40 + 15 * 15
    assert mine["top_country"] == "CN" and mine["top_share_pct"] == pytest.approx(63.4, abs=0.1)
    assert mine["hhi"] > market["hhi"]  # « le marché est diversifié, toi non »


def test_csv_template_and_errors() -> None:
    parsed, errors = parse_csv(CSV_TEMPLATE)
    assert not errors and parsed is not None
    assert [s.name for s in parsed.sites] == ["Distributeur Exemple", "Usine Exemple"]
    assert {(link.source, link.target) for link in parsed.links} == {
        ("distributeur-exemple", "me"), ("usine-exemple", "distributeur-exemple")}
    bad = "site;fournit;part_pct\nUsine;Inconnu;50\nAutre;moi;abc\n"
    _, errors = parse_csv(bad)
    assert any(e.startswith("Ligne 2") for e in errors) and any(e.startswith("Ligne 3") for e in errors)
    assert parse_csv("nom;x\n1;2\n")[0] is None


def test_city_lookup_is_offline() -> None:
    rows = [["Ningbo", "Ningbo", ["宁波"], "CN", 29.87, 121.55, 7_000_000],
            ["Anvers", "Antwerpen", ["Antwerp"], "BE", 51.22, 4.4, 530_000]]
    assert cities.search("antw", cities=rows)[0]["name"] == "Anvers"
    assert cities.search("宁波", cities=rows)[0]["country_iso2"] == "CN"
    assert cities.search("ningbo", "BE", cities=rows) == []


# --- in-memory watch rules ---------------------------------------------------------------------------------------
def test_private_rules_send_only_the_chosen_level(ctx: AppContext) -> None:
    material_id = with_my_chain(ctx)
    chains = {material_id: doc()}
    with ctx.db.connect() as conn:
        watch = build(conn, chains, DISRUPTION_TERMS)
    sent = " ".join(q for q, _ in watch.queries)
    assert "Anvers" in sent and "crylonitrile" in sent  # city and material, the default level
    for secret in ("CANARY", "Alpha Fictif", "Distributeur Alpha", "fictif"):
        assert secret not in sent  # « aléas seulement » sends nothing; no site or company name by default
    assert any(r.kind == "zone" for r in watch.rules)  # the hazard zone is compared on the Mac
    assert all(r.id < 0 for r in watch.rules) and "Anvers" not in repr(watch)
    company = chain_data()
    company["sites"][1]["watch_level"] = "city_material_company"
    with ctx.db.connect() as conn:
        watch = build(conn, {material_id: MyChain.model_validate(company)}, DISRUPTION_TERMS)
    assert any("Alpha Fictif SA" in q for q, _ in watch.queries)  # only once the user allowed it


def test_article_found_through_my_chain_keeps_no_rule(ctx: AppContext) -> None:
    material_id = with_my_chain(ctx)
    company = chain_data()
    company["sites"][1]["watch_level"] = "city_material_company"  # the user allowed the company name
    assert private_service.save(ctx.db, ctx.vault, material_id, MyChain.model_validate(company))[0] == 2
    seen: list[str] = []
    found = {"anvers": {"url": "https://reuters.com/anvers-fire", "title": "Fire near Anvers acrylonitrile depot"},
             "alpha": {"url": "https://reuters.com/alpha-strike", "title": "Strike at Alpha Fictif SA warehouse"}}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        if request.url.host != "api.gdeltproject.org":
            return httpx2.Response(404, content=b"{}")
        hits = [a for key, a in found.items() if ("Alpha" if key == "alpha" else "Anvers") in str(request.url)]
        return httpx2.Response(200, json={"articles": [{**a, "seendate": "20261103T060000Z", "domain": "reuters.com",
                                                        "language": "English"} for a in hits]})

    client = build_client(Allowlist({"api.gdeltproject.org"}), transport=httpx2.MockTransport(handler))
    from goulot.pipeline.cycle import private_watch

    run_cycle(ctx.db, client, lambda _: None, NOW, private=private_watch(ctx))
    with ctx.db.connect() as conn:
        rows = {r["url"]: r for r in conn.execute("SELECT url, rules_hit, materials FROM articles")}
    # Naming the material is a public rule; the company alone is found only through Ma chaîne and keeps no rule.
    assert all(i > 0 for i in json.loads(rows["https://reuters.com/anvers-fire"]["rules_hit"]))
    alpha = rows["https://reuters.com/alpha-strike"]
    assert alpha["rules_hit"] == "[]" and json.loads(alpha["materials"]) == [material_id]
    assert not any("CANARY" in u for u in seen)


def test_private_only_events_stay_out_of_public_views(ctx: AppContext) -> None:
    material_id = with_my_chain(ctx)
    private_id = add_event(ctx, material_id, entities=("CANARY-SITE-7F3A",))
    public_id = add_event(ctx, material_id, nodes=("us_acrylonitrile",))
    keyword_id = add_event(ctx, material_id, public_rule=True)
    with ctx.db.connect() as conn:
        assert not is_public(conn, private_id) and is_public(conn, public_id) and is_public(conn, keyword_id)
        market = {e.id for e in assess.event_inputs(conn, material_id, NOW)}
    assert private_id not in market and public_id in market


# --- matching, trigger and personal verdict (docs/SPEC.md, section 13) -----------------------------------------
def test_small_private_supplier_on_fire_triggers_analysis_and_personal_verdict(ctx: AppContext) -> None:
    """0.3 % of world supply, 80 % of the user's volume, touched by a fire."""
    material_id = with_my_chain(ctx, (80, 20))
    fire = add_event(ctx, material_id, entities=("CANARY-COMPANY-5K8M",), place="CANARY-CITY-9Q2X, Zhejiang",
                     severity_hint=2)
    with ctx.db.transaction() as conn:  # what the triggered analysis says: tiny for the market, three months long
        conn.execute("INSERT INTO analyses (id, event_id, material_id, model, prompt_version, schema_version, mode, "
                     "state, signature, result, created_at) VALUES (?, ?, ?, 'm', 'p', 's', 'standard', 'done', 'x', "
                     "?, ?)", (str(uuid.uuid4()), fire, material_id, json.dumps(impact(0, duration_high=90)),
                               NOW.isoformat()))
    with ctx.db.connect() as conn:
        models = private_service.safe_models(conn, ctx.vault)
        assert fire not in {e["id"] for e in assess.due_events(conn, Allowance())}  # the market would not look
    with ctx.db.transaction() as conn:
        weighs = matching.refresh(conn, ctx.vault, models, NOW)
        links = records.read_links(conn, ctx.vault, material_id)
    assert fire in weighs and links[fire]["sites"] == ["beta"] and set(links[fire]["how"]) == {"nom", "ville"}
    with ctx.db.connect() as conn:
        assert fire in {e["id"] for e in assess.due_events(conn, Allowance(), weighs)}
        assert fire in {e["id"] for e in assess.due_events(conn, Allowance(min_severity=3), weighs)}  # crisis reserve
    changed = private_refresh.refresh(ctx.db, ctx.vault, NOW, models, verified=True, last_ok_cycle=None)
    (mid, p), = changed
    assert mid == material_id and p.verdict.verdict in (POTENTIAL, ANTICIPATE)
    assert p.exposures[fire]["exposure_pct"] == 80.0 and p.exposures[fire]["s_perso"] == 3
    assert p.verdict.x_weeks == round((35 + 37.29) / 7, 1)  # sea to Antwerp, then the distributor's stock
    assert p.plan_b and p.plan_b[0]["status"] == "qualifiée"
    with ctx.db.connect() as conn:
        market = conn.execute("SELECT 1 FROM verdicts WHERE scope = 'market' AND event_ids LIKE ?",
                              (f"%{fire}%",)).fetchone()
    assert market is None  # the market verdict never sees it


def test_hazard_near_a_private_site_touches_it(ctx: AppContext) -> None:
    material_id = with_my_chain(ctx)
    quake = add_event(ctx, material_id, lat=29.9, lon=121.6, event_type="natural_disaster", title="Séisme")
    far = add_event(ctx, material_id, lat=31.2, lon=121.5, event_type="natural_disaster", title="Séisme lointain")
    with ctx.db.transaction() as conn:
        matching.refresh(conn, ctx.vault, private_service.safe_models(conn, ctx.vault), NOW)
        links = records.read_links(conn, ctx.vault, material_id)
    assert links[quake]["how"] == ["zone"] and far not in links  # 150 km away


def test_outage_off_the_users_paths_gives_no_physical_rule(ctx: AppContext) -> None:
    material_id = with_my_chain(ctx, (80, 20))
    event = EventInput("us", True, 0.9, ["us_acrylonitrile"], impact(3))
    off = PersonalEvent(event, "industrial_outage", "ongoing", "Arrêt aux États-Unis")
    p = personal_for(ctx, material_id, model_for(ctx, material_id, (80, 20)), [], [off])
    assert p.exposures["us"]["exposure_pct"] == 0.0 and p.verdict.verdict == RAS
    # The same event counts for someone whose undeclared supply flows like the market.
    p = personal_for(ctx, material_id, None, [], [off])
    assert p.verdict.verdict != RAS


def test_announced_rise_signal_gives_anticipate_with_x_until_it_applies(ctx: AppContext) -> None:
    material_id = with_my_chain(ctx)
    signal = {"id": "s1", "type": "price_increase", "pct": 8, "date": "2026-11-03", "effective_date": "2026-12-01",
              "site_id": "alpha"}
    p = personal_for(ctx, material_id, model_for(ctx, material_id), [signal], [])
    assert p.verdict.verdict == ANTICIPATE and p.verdict.x_weeks == 4.0 and p.verdict.pressure == 2
    assert p.verdict.event_ids == ["s1"]


def test_force_majeure_upstream_counts_its_delay_not_zero(ctx: AppContext) -> None:
    material_id = with_my_chain(ctx, (80, 20))
    m = model_for(ctx, material_id, (80, 20))
    upstream = {"id": "s2", "type": "force_majeure", "date": "2026-11-02", "site_id": "beta"}
    p = personal_for(ctx, material_id, m, [upstream], [])
    assert p.verdict.x_weeks == round((35 + 37.29) / 7, 1) and p.verdict.x_weeks > 0
    assert p.exposures["s2"]["s_perso"] == 3
    direct = {"id": "s3", "type": "force_majeure", "date": "2026-11-02", "site_id": "alpha"}
    assert personal_for(ctx, material_id, m, [direct], []).verdict.x_weeks == 0.0
    assert personal_for(ctx, material_id, m, [direct], []).verdict.verdict == ANTICIPATE


def test_longer_lead_signal_replaces_the_link_for_the_window(ctx: AppContext) -> None:
    material_id = with_my_chain(ctx, (80, 20))
    m = model_for(ctx, material_id, (80, 20))
    small = {"id": "d1", "type": "delay", "date": "2026-11-02", "site_id": "alpha", "old_lead_days": 7,
             "new_lead_days": 10}  # 3 days, under 2 weeks and 50 %: only noted
    big = {**small, "id": "d2", "new_lead_days": 63}
    assert personal_for(ctx, material_id, m, [small], []).verdict.window_weeks == -1.0  # 0 − 7 days
    assert personal_for(ctx, material_id, m, [big], []).verdict.window_weeks == -9.0  # 0 − 63 days


def test_signals_expire_and_close() -> None:
    old = {"id": "a", "type": "allocation", "date": "2026-07-01", "site_id": "alpha"}
    fresh = {"id": "b", "type": "allocation", "date": "2026-10-20", "site_id": "beta"}
    closed = {"id": "c", "type": "rupture", "date": "2026-10-01", "site_id": "alpha"}
    normal = {"id": "d", "type": "back_to_normal", "date": "2026-10-15", "site_id": "alpha"}
    live = active_signals([old, fresh, closed, normal], NOW.date())
    assert [s["id"] for s in live] == ["b"]
    assert [s["id"] for s in expiring([old, fresh], NOW.date())] == ["a"]


# --- outcomes of personal advices ----------------------------------------------------------------------------------
def test_signal_never_validates_the_advice_it_caused() -> None:
    entry = {"verdict": "ant", "at": "2026-11-03T09:00:00+00:00"}
    private = {"x_weeks": 4.0, "threshold_pct": 5.0, "event_ids": ["s1"], "signals": ["s1"]}
    caused = [{"id": "s1", "type": "price_increase", "date": "2026-11-04"}]
    late = date(2027, 1, 30)  # window ended 15 Dec, plus 14 days for an answer
    outcome, _ = ledger_personal.judge(entry, private, None, None, None, caused, late)
    assert outcome.result == UNRESOLVED  # silence is never a success, and the signal does not count
    other = [*caused, {"id": "s9", "type": "allocation", "date": "2026-11-20"}]
    outcome, basis = ledger_personal.judge(entry, private, None, None, None, other, late)
    assert outcome.result == "confirmed" and basis == "signal fournisseur"
    outcome, basis = ledger_personal.judge(entry, private, None, None, "none", caused, late)
    assert outcome.result == "false_alarm" and basis == "ta réponse"
    pending, _ = ledger_personal.judge(entry, private, None, None, None, caused, date(2026, 12, 20))
    assert pending.result == "pending"  # 14 days to answer


def test_private_series_resolves_a_personal_advice() -> None:
    entry = {"verdict": "pot", "at": "2026-11-03T09:00:00+00:00"}
    private = {"x_weeks": 3.0, "threshold_pct": 5.0, "event_ids": [], "signals": []}
    mine = Series([(date(2026, 10, 1), 1000.0), (date(2026, 11, 20), 1080.0)], rank=1)
    outcome, basis = ledger_personal.judge(entry, private, None, mine, None, [], date(2027, 1, 30))
    assert outcome.result == "confirmed" and basis == "série privée"


def test_personal_entries_are_kept_apart_in_the_ledger(ctx: AppContext) -> None:
    material_id = with_my_chain(ctx, (80, 20))
    add_event(ctx, material_id, entities=("CANARY-SITE-7F3A",), analysis=impact(0, duration_high=90))
    with ctx.db.connect() as conn:
        models = private_service.safe_models(conn, ctx.vault)
    with ctx.db.transaction() as conn:
        matching.refresh(conn, ctx.vault, models, NOW)
    private_refresh.refresh(ctx.db, ctx.vault, NOW, models, verified=True, last_ok_cycle=None)
    with ctx.db.connect() as conn:
        assert ledger_chain.entries(conn) == []  # the public reading sees no personal entry
        (entry,) = ledger_chain.entries(conn, scope="personal", kinds=("verdict",))
        assert set(entry) == {"seq", "at", "material_id", "kind", "scope", "verdict", "pressure", "inputs_digest"}
        private = ledger_chain.private_fields(conn, ctx.vault, entry["seq"])
        assert private["x_weeks"] is not None and private["exposure_pct"] == 80.0
        assert ledger_chain.verify(conn).ok
        later = NOW + timedelta(weeks=13)  # X 10.3 weeks, plus 2
        assert ledger_personal.questions(conn, ctx.vault, later.date())[0]["seq"] == entry["seq"]
    with ctx.db.transaction() as conn:
        ledger_personal.record_feedback(conn, ctx.vault, material_id, entry["seq"], "tension", at=later)
        assert ledger_personal.resolve(conn, ctx.vault, later.date(), lambda _: None, at=later) >= 1
        outcome = next(e for e in ledger_personal.entries(conn, ctx.vault) if e["kind"] == "outcome")
    assert outcome["result"] == "confirmed" and outcome["basis"] == "ta réponse"


def test_city_dump_keeps_the_names_people_type(tmp_path: Any) -> None:
    import io
    import zipfile

    line = "\t".join(["2803138", "Antwerpen", "Antwerpen", "ANR,Amberes,Anvers,Antwerp,Антверпен,安特卫普,Anvers 2", "51.21989",
                      "4.40346", "P", "PPLA", "BE", "", "", "", "", "", "459805"])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("cities15000.txt", line + "\n")
    rows = cities.reduce_dump(buf.getvalue())
    assert rows[0][2] == ["Amberes", "Anvers", "Antwerp", "安特卫普"]  # no airport code, no Cyrillic, no digit
    path = tmp_path / "cities.json.gz"
    cities.write(rows, path)
    assert cities.write(rows, path) == path.stat().st_size and path.read_bytes()[:2] == b"\x1f\x8b"
    assert cities.search("anvers", cities=cities._cities(path))[0]["name"] == "Antwerpen"
