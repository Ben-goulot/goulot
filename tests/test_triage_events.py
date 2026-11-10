"""Phase 3, lot 2: triage in Batch, corroboration, lineages and events (recorded answers, no network)."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from typing import Any

import pytest

from goulot.api.context import AppContext, MissingAPIKey
from goulot.llm.batch import ReplayBatchBackend
from goulot.llm.budget import Budget
from goulot.pipeline import grouping, triage
from goulot.pipeline.corroborate import ArticleRef, article_reliability, corroborate, domain_reliability
from tests import chain_fixtures as fx
from tests.test_collect import NOW, http, validated_chain

TRIAGE_EXAMPLE: dict[str, Any] = json.loads(
    (fx.REPO_ROOT / "schemas" / "examples" / "triage_v2.example.json").read_text(encoding="utf-8"))


def triage_message(result: dict[str, Any], in_tokens: int = 2_500, out_tokens: int = 300) -> dict[str, Any]:
    return {"id": "msg_t", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
            "stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "usage": fx.usage(in_tokens, out_tokens)}


def result(**changes: Any) -> dict[str, Any]:
    r = copy.deepcopy(TRIAGE_EXAMPLE)
    r.update(changes)
    return r


def collect_cycle(ctx: AppContext) -> None:
    from goulot.collect.runner import run_cycle

    run_cycle(ctx.db, http(), lambda name: "k" * 32 if name == "nasa_firms" else None, NOW)


def answers_for(ctx: AppContext, make: Any) -> dict[str, dict[str, Any]]:
    """One recorded answer per pending custom id, built by `make(article_row)`."""
    with ctx.db.connect() as conn:
        rows = conn.execute("SELECT t.custom_id, a.* FROM triage t JOIN articles a ON a.id = t.article_id").fetchall()
    return {r["custom_id"]: triage_message(make(r)) for r in rows}


# --- reliability and corroboration ------------------------------------------------------------------------
def test_reliability_classes() -> None:
    assert domain_reliability("www.reuters.com") == 0.7
    assert domain_reliability("ec.europa.eu") == 0.9 and domain_reliability("mofcom.gov.cn") == 0.9
    assert domain_reliability("lemonde.fr") == 0.5 and domain_reliability("random-blog.example") == 0.3
    assert domain_reliability("prnewswire.com") == 0.3
    assert domain_reliability("reuters.com", "/press-release/x") == 0.3  # paid release wherever it is published
    assert article_reliability("news.google.com", "reuters.com") == 0.7  # publisher from structured data
    assert article_reliability("news.google.com", "") == 0.3  # no structured data: unknown
    assert domain_reliability("newsite.example", extra={"newsite.example": 0.7}) == 0.7  # user's local layer


def ref(i: int, domain: str, title: str, cited: str = "unknown", reliability: float | None = None) -> ArticleRef:
    url = f"https://{domain}/a{i}"
    return ArticleRef(i, url, domain, "", title, "", cited,
                      reliability if reliability is not None else domain_reliability(domain))


def test_lineages_group_copies() -> None:
    text = "Fire stops the acrylonitrile plant of Gulfchem on the Gulf coast for several weeks"
    result_, keys = corroborate([ref(1, "reuters.com", text), ref(2, "copycat.example", text),
                                 ref(3, "lemonde.fr", "Incendie dans une usine texane, arrêt de plusieurs semaines")])
    assert keys[1] == keys[2] != keys[3]
    assert result_.lineages == 2 and result_.c_fait == pytest.approx(1 - 0.3 * 0.5)
    assert result_.confirmed
    same_origin, _ = corroborate([ref(1, "a.example", "x one", "Reuters"), ref(2, "b.example", "y two", "reuters")])
    assert same_origin.lineages == 1


# --- triage -----------------------------------------------------------------------------------------------
def test_triage_batch_validates_and_quarantines(ctx: AppContext) -> None:
    validated_chain(ctx)
    collect_cycle(ctx)
    backend = ReplayBatchBackend()
    batch_id = triage.submit_pending(ctx.db, ctx.budget, backend)
    assert batch_id is not None
    (requests,) = backend.batches.values()
    params = requests[0][1]
    assert params["model"] == "claude-haiku-5-5" and params["thinking"] == {"type": "disabled"}
    assert "tools" not in params and "<chain_context>" in params["system"][1]["text"]

    def make(row: Any) -> dict[str, Any]:
        if "Ignore previous instructions" in row["title"]:
            return result(injection_suspected=True, severity_hint=4)
        if "Rotterdam" in row["title"]:
            return result(relevant=False)
        return result(matched_node_ids=["cn_acrylonitrile", "invented_node"], title_fr=" ".join(["mot"] * 20))

    backend.responses = answers_for(ctx, make)
    assert triage.collect_pending(ctx.db, ctx.budget, backend) == len(requests)
    with ctx.db.connect() as conn:
        states = {r["title"]: r["status"] for r in conn.execute("SELECT title, status FROM articles WHERE kind = 'news'")}
        done = [json.loads(r[0]) for r in conn.execute("SELECT result FROM triage WHERE state = 'done'")]
        costs = conn.execute("SELECT mode, COUNT(*), SUM(cost_usd) FROM llm_calls GROUP BY mode").fetchall()
    assert any(s == "quarantined" for t, s in states.items() if "Ignore previous" in t)
    assert any(s == "irrelevant" for t, s in states.items() if "Rotterdam" in t)
    assert all("invented_node" not in d["matched_node_ids"] for d in done)
    assert all(len(d["title_fr"].split()) <= 13 for d in done)
    assert costs[0][0] == "batch" and costs[0][2] < 0.01  # Batch rate on Haiku 5.5
    Budget(ctx.db, ctx.clock).check_breaker()  # many batch results do not trip the breaker


def test_failed_triage_is_retried_then_set_aside(ctx: AppContext) -> None:
    validated_chain(ctx)
    collect_cycle(ctx)
    backend = ReplayBatchBackend()  # answers nothing: every request errors
    for attempt in range(1, triage.MAX_ATTEMPTS + 1):
        assert triage.submit_pending(ctx.db, ctx.budget, backend) is not None, attempt
        triage.collect_pending(ctx.db, ctx.budget, backend)
        with ctx.db.connect() as conn:
            statuses = {r[0] for r in conn.execute("SELECT status FROM articles WHERE kind = 'news'")}
        assert statuses == ({"new"} if attempt < triage.MAX_ATTEMPTS else {"irrelevant"})
    assert triage.submit_pending(ctx.db, ctx.budget, backend) is None  # nothing left to send


def test_quarantined_articles_never_reach_an_event(ctx: AppContext) -> None:
    validated_chain(ctx)
    collect_cycle(ctx)
    backend = ReplayBatchBackend()
    triage.submit_pending(ctx.db, ctx.budget, backend)
    backend.responses = answers_for(ctx, lambda row: result(injection_suspected=True))
    triage.collect_pending(ctx.db, ctx.budget, backend)
    with ctx.db.transaction() as conn:
        assert grouping.group_triaged(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM events WHERE origin = 'news'").fetchone()[0] == 0


# --- events -----------------------------------------------------------------------------------------------
def test_articles_grouped_and_corroborated(ctx: AppContext) -> None:
    validated_chain(ctx)
    collect_cycle(ctx)
    backend = ReplayBatchBackend()
    triage.submit_pending(ctx.db, ctx.budget, backend)

    def make(row: Any) -> dict[str, Any]:
        if row["source"] == "federal_register":
            return result(event_type="sanctions_trade_policy", matched_node_ids=["us_acrylonitrile"],
                          title_fr="Droits antidumping sur l'acrylonitrile coréen", country_iso2="US")
        return result(matched_node_ids=["plant_gulf_cracker"], place="Côte du Golfe, Texas", cited_origin="unknown")

    backend.responses = answers_for(ctx, make)
    triage.collect_pending(ctx.db, ctx.budget, backend)
    with ctx.db.transaction() as conn:
        touched = grouping.group_triaged(conn)
        events = {e["id"]: dict(e) for e in conn.execute("SELECT * FROM events WHERE origin IN ('news', 'policy')")}
        links = conn.execute("SELECT event_id, node_key FROM event_links").fetchall()
    assert len(touched) == len(events) == 2  # the outage (several articles) and the trade measure
    outage = next(e for e in events.values() if e["event_type"] == "industrial_outage")
    assert outage["lineages"] >= 2 and outage["c_fait"] > 0.75
    assert outage["confirmation"] == "confirmed"  # one lineage from reuters.com (0.7)
    # The same articles all citing one origin form a single lineage: copies never confirm each other.
    with ctx.db.transaction() as conn:
        for (tid, res) in conn.execute("SELECT id, result FROM triage WHERE state = 'done'").fetchall():
            r = json.loads(res)
            r["cited_origin"] = "Gulfchem (communiqué)"
            conn.execute("UPDATE triage SET result = ? WHERE id = ?", (json.dumps(r), tid))
        grouping.refresh_corroboration(conn, outage["id"])
        assert conn.execute("SELECT lineages FROM events WHERE id = ?", (outage["id"],)).fetchone()[0] == 1
    policy = next(e for e in events.values() if e["event_type"] == "sanctions_trade_policy")
    assert policy["official_source"] == 1 and policy["confirmation"] == "confirmed"  # federalregister.gov
    assert (outage["id"], "plant_gulf_cracker") in [tuple(r) for r in links]


def test_one_incident_typed_two_ways_is_one_event(ctx: AppContext) -> None:
    """Seen in the recorded scenario: the triage called the same fire an infrastructure failure in one article and
    an industrial outage in the other, with another place name; the shared company groups them."""
    validated_chain(ctx)
    collect_cycle(ctx)
    backend = ReplayBatchBackend()
    triage.submit_pending(ctx.db, ctx.budget, backend)
    with ctx.db.connect() as conn:
        purchased = conn.execute("SELECT node_key FROM chain_nodes WHERE tier = 0").fetchone()[0]
    gulfchem = [{"name": "Gulfchem", "kind": "company"}]
    answers = iter([
        result(event_type="infrastructure_failure", place="Texas", entities=gulfchem, matched_node_ids=[purchased],
               title_fr="Incendie à l'usine Gulfchem au Texas", cited_origin="unknown"),
        result(event_type="industrial_outage", place="Calderon", entities=[{"name": "GULFCHEM", "kind": "company"}],
               matched_node_ids=[purchased], title_fr="Force majeure de Gulfchem après l'incendie",
               cited_origin="unknown"),
        result(event_type="industrial_outage", place="Ulsan", country_iso2="KR", matched_node_ids=[purchased],
               entities=[{"name": "Hanchem", "kind": "company"}], title_fr="Arrêt technique chez Hanchem en Corée",
               cited_origin="unknown"),
        result(event_type="sanctions_trade_policy", place="Calderon", entities=gulfchem,
               matched_node_ids=[purchased], title_fr="Enquête commerciale visant Gulfchem", cited_origin="unknown"),
    ])
    backend.responses = answers_for(ctx, lambda row: next(answers, result(relevant=False)))
    triage.collect_pending(ctx.db, ctx.budget, backend)
    with ctx.db.transaction() as conn:
        grouping.group_triaged(conn)
        events = conn.execute("SELECT e.event_type, COUNT(*) AS n FROM events e JOIN event_articles ea "
                              "ON ea.event_id = e.id WHERE e.origin IN ('news', 'policy') GROUP BY e.id "
                              "ORDER BY n DESC, e.event_type").fetchall()
    # The fire (2 articles); the Korean outage stays apart although it matches the same purchased material;
    # a trade measure about the same company is another kind of event.
    assert [(r["event_type"], r["n"]) for r in events] == [
        ("infrastructure_failure", 2), ("industrial_outage", 1), ("sanctions_trade_policy", 1)]


def test_measurements_become_events_without_ai(ctx: AppContext) -> None:
    validated_chain(ctx)
    collect_cycle(ctx)
    with ctx.db.transaction() as conn:
        created = grouping.events_from_measurements(conn, NOW)
        events = {e["id"]: dict(e) for e in conn.execute("SELECT * FROM events")}
        links = {r[0]: r[1] for r in conn.execute("SELECT event_id, node_key FROM event_links")}
    by_title = {e["title"]: e for e in events.values()}
    flood = by_title["Flood alert in Netherlands"]
    assert flood["confirmation"] == "confirmed" and flood["severity_hint"] == 2 and flood["official_source"]
    assert links[flood["id"]] == "port_rotterdam"
    assert not any(e["title"].startswith("Incendie détecté") for e in events.values())  # never an event alone
    suez = next(e for e in events.values() if "Suez" in e["title"])
    assert suez["event_type"] == "maritime_disruption" and links[suez["id"]] == "chokepoint_suez"
    assert len(created) >= 3
    with ctx.db.connect() as conn:
        waiting = conn.execute("SELECT source FROM articles WHERE kind IN ('hazard', 'logistics') "
                               "AND status = 'new'").fetchall()
    assert [r[0] for r in waiting] == ["nasa_firms"]  # the hot spot waits 3 days for the press


# --- only what is really linked (decision of 8 November 2026) ----------------------------------------------
def _measure(conn: Any, source: str, title: str, seen: datetime, lat: float | None = None, lon: float | None = None,
             magnitude: float | None = None, **details: Any) -> int:
    rule = conn.execute("SELECT id FROM watch_rules WHERE active = 1 AND kind = 'zone' AND node_key = 'port_rotterdam'"
                        ).fetchone()[0]
    strait = conn.execute("SELECT id FROM watch_rules WHERE active = 1 AND kind = 'strait' "
                          "AND node_key = 'waterway_rhine'").fetchone()[0]
    rules = [strait] if source == "pegelonline" else [rule]
    kind = "logistics" if source == "pegelonline" else "hazard"
    cur = conn.execute(
        "INSERT INTO articles (source, kind, url, url_hash, title, first_seen_at, collected_at, lat, lon, magnitude, "
        "details, rules_hit, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')",
        (source, kind, f"https://x.example/{title}{seen}", f"h-{title}-{seen.isoformat()}-{lat}", title,
         seen.isoformat(), seen.isoformat(), lat, lon, magnitude, json.dumps(details), json.dumps(rules)))
    return int(cur.lastrowid or 0)


def _status(conn: Any, article_id: int) -> str:
    return str(conn.execute("SELECT status FROM articles WHERE id = ?", (article_id,)).fetchone()[0])


def test_satellite_fire_only_backs_a_press_report_at_the_site(ctx: AppContext) -> None:
    material_id = validated_chain(ctx)
    with ctx.db.transaction() as conn:
        far = _measure(conn, "nasa_firms", "Incendie détecté par satellite (puissance 40 MW)", NOW, 52.02, 4.14,
                       40.0, event_type="WF", near_node="port_rotterdam")  # about 8 km away: a field
        near = _measure(conn, "nasa_firms", "Incendie détecté par satellite (puissance 60 MW)", NOW, 51.96, 4.15,
                        60.0, event_type="WF", near_node="port_rotterdam")
        assert grouping.events_from_measurements(conn, NOW) == []
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert _status(conn, near) == "new"  # waits for the press
        # The press reports a fire at the port the next day: the nearby hot spot backs it, the far one does not.
        conn.execute("INSERT INTO events (id, material_id, event_type, event_status, origin, title, first_seen_at, "
                     "last_seen_at, confirmation, updated_at) VALUES ('EVT-1', ?, 'industrial_outage', 'ongoing', "
                     "'news', 'Incendie au port', ?, ?, 'early_signal', ?)",
                     (material_id, (NOW + timedelta(days=1)).isoformat(), (NOW + timedelta(days=1)).isoformat(),
                      NOW.isoformat()))
        conn.execute("INSERT INTO event_links (event_id, material_id, node_key) VALUES ('EVT-1', ?, 'port_rotterdam')",
                     (material_id,))
        grouping.events_from_measurements(conn, NOW + timedelta(days=1))
        assert _status(conn, near) == "grouped" and _status(conn, far) == "new"
        attached = [r[0] for r in conn.execute("SELECT article_id FROM event_articles WHERE event_id = 'EVT-1'")]
        assert attached == [near]
        grouping.events_from_measurements(conn, NOW + timedelta(days=4))
        assert _status(conn, far) == "irrelevant"  # no press after 3 days: dropped


def test_recurring_heat_is_a_flare(ctx: AppContext) -> None:
    validated_chain(ctx)
    with ctx.db.transaction() as conn:
        for days in (10, 5):
            old = _measure(conn, "nasa_firms", f"Incendie détecté par satellite ({days})", NOW - timedelta(days=days),
                           51.955, 4.145, 30.0, event_type="WF", near_node="port_rotterdam")
            conn.execute("UPDATE articles SET status = 'irrelevant' WHERE id = ?", (old,))
        today = _measure(conn, "nasa_firms", "Incendie détecté par satellite (puissance 30 MW)", NOW, 51.956, 4.146,
                         30.0, event_type="WF", near_node="port_rotterdam")
        grouping.events_from_measurements(conn, NOW)
        assert _status(conn, today) == "irrelevant"


def test_drought_alert_opens_no_event(ctx: AppContext) -> None:
    validated_chain(ctx)
    with ctx.db.transaction() as conn:
        dry = _measure(conn, "gdacs", "Drought in Netherlands", NOW, 51.9, 4.2, None, event_type="DR",
                       alert_level="Orange")
        assert grouping.events_from_measurements(conn, NOW) == []
        assert _status(conn, dry) == "irrelevant"


def test_kaub_counts_downstream_only_at_the_critical_level(ctx: AppContext) -> None:
    """The test chain's Rhine sits next to Rotterdam, downstream of Kaub."""
    validated_chain(ctx)
    with ctx.db.transaction() as conn:
        _measure(conn, "pegelonline", "Basses eaux : KAUB à 92 cm", NOW, magnitude=92.0, station="KAUB",
                 waterway="Rhin (Rhine, Rhein)")
        assert grouping.events_from_measurements(conn, NOW) == []
        _measure(conn, "pegelonline", "Basses eaux : KAUB à 70 cm", NOW + timedelta(days=8), magnitude=70.0,
                 station="KAUB", waterway="Rhin (Rhine, Rhein)")
        assert len(grouping.events_from_measurements(conn, NOW + timedelta(days=8))) == 1


def test_irrelevant_articles_purged_after_30_days(ctx: AppContext) -> None:
    validated_chain(ctx)
    collect_cycle(ctx)
    backend = ReplayBatchBackend()
    triage.submit_pending(ctx.db, ctx.budget, backend)
    backend.responses = answers_for(ctx, lambda row: result(relevant=False))
    triage.collect_pending(ctx.db, ctx.budget, backend)
    with ctx.db.transaction() as conn:
        assert triage.purge_irrelevant(conn, (NOW + timedelta(days=29)).isoformat()) == 0
        assert triage.purge_irrelevant(conn, (NOW + timedelta(days=31)).isoformat()) > 0
        assert conn.execute("SELECT COUNT(*) FROM articles WHERE status = 'irrelevant'").fetchone()[0] == 0
    assert isinstance(NOW, datetime)


def test_watch_cycle_runs_triage_and_grouping(ctx: AppContext) -> None:
    from goulot.pipeline.cycle import process

    validated_chain(ctx)
    collect_cycle(ctx)
    backend = ReplayBatchBackend(responses={})
    ctx.llm_batch = lambda: backend
    process(ctx, NOW)  # measurements become events, a triage batch is submitted
    backend.responses = answers_for(ctx, lambda row: result(matched_node_ids=["plant_gulf_cracker"],
                                                            cited_origin="unknown"))
    process(ctx, NOW + timedelta(hours=3))  # the next cycle collects it and groups the articles
    with ctx.db.connect() as conn:
        origins = {r[0] for r in conn.execute("SELECT origin FROM events")}
        pending = conn.execute("SELECT COUNT(*) FROM triage WHERE state = 'pending'").fetchone()[0]
    assert {"news", "hazard", "logistics"} <= origins and pending == 0
    # Without an API key, collection and measurement events go on; triage waits.
    def no_key() -> ReplayBatchBackend:
        raise MissingAPIKey("absente")

    ctx.llm_batch = no_key
    process(ctx, NOW + timedelta(hours=6))  # does not raise


def test_same_reading_again_prolongs_its_event(ctx: AppContext) -> None:
    """Decision of 6 November 2026: the next reading of the same source at the same place within 7 days prolongs
    the ongoing event instead of opening a new one (the Kaub gauge analysed every day)."""
    validated_chain(ctx)
    collect_cycle(ctx)
    with ctx.db.transaction() as conn:
        grouping.events_from_measurements(conn)
        suez = conn.execute("SELECT * FROM events WHERE title LIKE '%Suez%'").fetchone()
        item = conn.execute("SELECT a.* FROM articles a JOIN event_articles ea ON ea.article_id = a.id "
                            "WHERE ea.event_id = ?", (suez["id"],)).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

        def reading(days: int, title: str) -> None:
            row = dict(item)
            row.pop("id")
            row.update(url=f"{row['url']}?d={days}", url_hash=f"h{days}", status="new", title=title,
                       first_seen_at=(NOW + timedelta(days=days)).isoformat(), magnitude=0.1)
            conn.execute(f"INSERT INTO articles ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",  # noqa: S608
                         tuple(row.values()))

        assert item["lat"] is None  # a strait's traffic: the same place is told by its title without figures
        reading(1, "Suez Canal : 6 passages le 19/10, contre 60 en moyenne")
        created = grouping.events_from_measurements(conn)
        again = conn.execute("SELECT * FROM events WHERE id = ?", (suez["id"],)).fetchone()
        assert created == [] and conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == count
        assert again["last_seen_at"] > suez["last_seen_at"] and again["severity_hint"] == 3  # worse: hint raised
        reading(2, "Bab el-Mandeb : 6 passages le 20/10, contre 60 en moyenne")  # another strait: another event
        assert len(grouping.events_from_measurements(conn)) == 1


def test_old_noise_events_are_set_aside_and_explained(ctx: AppContext) -> None:
    """Events stored before the decision of 8 November 2026 (a hot spot alone) leave the screens and the analyst;
    every shown event says why it concerns the material."""
    from goulot.api.watch_routes import link_reason
    from goulot.pipeline.visibility import set_aside, unrelated

    material_id = validated_chain(ctx)
    with ctx.db.transaction() as conn:
        spot = _measure(conn, "nasa_firms", "Incendie détecté par satellite (puissance 26 MW)", NOW, 51.96, 4.15, 26.0,
                        event_type="WF", near_node="port_rotterdam")
        conn.execute("INSERT INTO events (id, material_id, event_type, event_status, origin, title, first_seen_at, "
                     "last_seen_at, confirmation, updated_at) VALUES ('EVT-old', ?, 'industrial_outage', 'ongoing', "
                     "'hazard', 'Incendie détecté par satellite', ?, ?, 'early_signal', ?)",
                     (material_id, NOW.isoformat(), NOW.isoformat(), NOW.isoformat()))
        conn.execute("INSERT INTO event_articles VALUES ('EVT-old', ?, '', 0)", (spot,))
        assert unrelated(conn, "EVT-old") and set_aside(conn, "EVT-old")
    nodes = {"port_rotterdam": {"name": "Port de Rotterdam", "tier": 2, "node_type": "port", "lat": 51.95, "lon": 4.14},
             "waterway_rhine": {"name": "Rhin", "tier": 2, "node_type": "waterway", "lat": None, "lon": None},
             "acrylonitrile": {"name": "Acrylonitrile", "tier": 0, "node_type": "material", "lat": None, "lon": None}}
    flood = {"origin": "hazard", "lat": 51.99, "lon": 4.14}
    assert link_reason(flood, ["port_rotterdam"], nodes, {}) == "à 4 km de Port de Rotterdam"
    low = {"origin": "logistics", "lat": None, "lon": None}
    assert "niveau critique" in link_reason(low, ["waterway_rhine"], nodes,
                                           {"station": "KAUB", "waterway": "Rhin (Rhine, Rhein)", "level_cm": 21})
    news = {"origin": "news", "lat": None, "lon": None}
    assert link_reason(news, ["acrylonitrile"], nodes, {}) == "l'article cite la matière"
    assert link_reason(news, ["acrylonitrile", "port_rotterdam"], nodes, {}) == "l'article cite Port de Rotterdam"


# --- decisions of 10 November 2026 ------------------------------------------------------------------------
def test_two_known_newspapers_confirm_but_unknown_sites_never_do() -> None:
    le_monde = ref(1, "lemonde.fr", "Grève au port de Calderon, les expéditions arrêtées")
    dw = ref(2, "dw.com", "Strike halts shipments at the port of Calderon")
    two, _ = corroborate([le_monde, dw])
    assert two.lineages == 2 and two.best == 0.5 and two.c_fait == 0.75 and two.confirmed
    alone, _ = corroborate([le_monde])
    assert not alone.confirmed
    # The same newspaper group, or the same cited origin, is one lineage: no confirmation.
    group, _ = corroborate([le_monde, ref(3, "courrierinternational.com", "Un port à l'arrêt")])
    assert group.lineages == 1 and not group.confirmed
    same_origin, _ = corroborate([ref(1, "lemonde.fr", "Grève au port", "Reuters"),
                                  ref(2, "dw.com", "Port strike", "reuters")])
    assert not same_origin.confirmed
    # Four unknown sites (0.3 each) reach c_fait 0.76 and still confirm nothing; one known newspaper with them neither.
    fakes = [ref(i, f"site{i}.example", f"Story number {i} told in its own words") for i in range(10, 14)]
    assert corroborate(fakes)[0].c_fait > 0.75 and not corroborate(fakes)[0].confirmed
    assert not corroborate([le_monde, *fakes])[0].confirmed
    # A specialised source and any other lineage: confirmed, as before.
    assert corroborate([ref(1, "reuters.com", "Port strike"), fakes[0]])[0].confirmed


def _triaged(ctx: AppContext, answers: list[dict[str, Any]]) -> None:
    validated_chain(ctx)
    collect_cycle(ctx)
    backend = ReplayBatchBackend()
    triage.submit_pending(ctx.db, ctx.budget, backend)
    it = iter(answers)
    backend.responses = answers_for(ctx, lambda row: next(it, result(relevant=False, link="none",
                                                                      matched_node_ids=[])))
    triage.collect_pending(ctx.db, ctx.budget, backend)


def test_a_country_alone_never_groups_two_stories(ctx: AppContext) -> None:
    """A fire and a quake in the same producing country are two events: grouped by the country node, they would
    count as two independent sources of one fact and confirm each other."""
    validated_chain(ctx)
    with ctx.db.connect() as conn:
        country = conn.execute("SELECT node_key FROM chain_nodes WHERE node_type = 'country'").fetchone()[0]
        site = conn.execute("SELECT node_key FROM chain_nodes WHERE node_type = 'plant'").fetchone()[0]
    _triaged(ctx, [
        result(link="country", event_type="industrial_outage", place="Tianjin", country_iso2="CN",
               matched_node_ids=[country], entities=[], title_fr="Incendie dans un port du nord",
               cited_origin="unknown"),
        result(link="country", event_type="natural_disaster", place="Yunnan", country_iso2="CN",
               matched_node_ids=[country], entities=[], title_fr="Séisme dans une province du sud-ouest",
               cited_origin="unknown"),
        result(link="site", event_type="industrial_outage", place="Somewhere", country_iso2="unknown",
               matched_node_ids=[site], entities=[], title_fr="Arrêt d'une unité après une panne",
               cited_origin="unknown"),
        result(link="site", event_type="infrastructure_failure", place="Elsewhere", country_iso2="unknown",
               matched_node_ids=[site], entities=[], title_fr="Coupure de courant sur le même site",
               cited_origin="unknown"),
    ])
    with ctx.db.transaction() as conn:
        grouping.group_triaged(conn)
        sizes = sorted(r[0] for r in conn.execute(
            "SELECT COUNT(*) FROM event_articles ea JOIN events e ON e.id = ea.event_id WHERE e.origin = 'news' "
            "GROUP BY e.id"))
    assert sizes == [1, 1, 2]  # the two country stories stay apart; a shared site still groups


def test_listed_company_elsewhere_is_never_an_event(ctx: AppContext) -> None:
    """Triage v2: the code decides. `other_site` or `none` makes the article irrelevant whatever `relevant` says."""
    _triaged(ctx, [result(link="other_site", relevant=True, severity_hint=3),
                   result(link="none", relevant=True)])
    with ctx.db.transaction() as conn:
        done = [json.loads(r[0]) for r in conn.execute("SELECT result FROM triage WHERE state = 'done' ORDER BY id")]
        assert grouping.group_triaged(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM articles WHERE status = 'triaged'").fetchone()[0] == 0
    assert all(not d["relevant"] and d["matched_node_ids"] == [] for d in done[:2])
    assert triage.check_triage(result(link="other_site"), {"plant_gulf_cracker", "propylene"}) == [
        "relevant without a link"]
    assert triage.check_triage(result(link="site"), {"plant_gulf_cracker", "propylene"}) == []


def test_triage_sees_the_share_of_each_node(ctx: AppContext) -> None:
    context = triage.chain_context([
        {"node_key": "plant_a", "node_type": "plant", "name": "A", "place": "X", "country_iso2": "US",
         "global_share_pct": 6.5},
        {"node_key": "plant_b", "node_type": "plant", "name": "B", "place": "Y", "country_iso2": "DE",
         "global_share_pct": None}])
    assert "plant_a | plant | A | X | US | 6.5 %" in context and "plant_b | plant | B | Y | DE | ?" in context
    assert triage.PROMPT == triage.SCHEMA == "triage_v2"


def test_event_reason_follows_the_triage_link() -> None:
    from goulot.api.watch_routes import link_reason

    nodes = {"p": {"name": "Usine Gulfchem", "tier": 2, "node_type": "plant", "lat": None, "lon": None},
             "c": {"name": "Chine", "tier": 2, "node_type": "country", "lat": None, "lon": None}}
    news = {"origin": "news", "lat": None, "lon": None}
    assert link_reason(news, ["p"], nodes, {"link": "site"}) == "fait qui touche Usine Gulfchem"
    assert link_reason(news, ["c"], nodes, {"link": "country"}) == "fait à l'échelle du pays producteur : Chine"
    assert link_reason(news, [], nodes, {"link": "material"}) == "fait qui vise la matière elle-même"
    assert link_reason(news, ["p"], nodes, {"link": None}) == "l'article cite Usine Gulfchem"  # triage v1 events
