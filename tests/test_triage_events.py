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
    (fx.REPO_ROOT / "schemas" / "examples" / "triage_v1.example.json").read_text(encoding="utf-8"))


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
        created = grouping.events_from_measurements(conn)
        events = {e["id"]: dict(e) for e in conn.execute("SELECT * FROM events")}
        links = {r[0]: r[1] for r in conn.execute("SELECT event_id, node_key FROM event_links")}
    by_title = {e["title"]: e for e in events.values()}
    flood = by_title["Flood alert in Netherlands"]
    assert flood["confirmation"] == "confirmed" and flood["severity_hint"] == 2 and flood["official_source"]
    assert links[flood["id"]] == "port_rotterdam"
    fire = next(e for e in events.values() if e["title"].startswith("Incendie détecté"))
    assert fire["confirmation"] == "early_signal"  # a satellite hot spot needs the press to be confirmed
    suez = next(e for e in events.values() if "Suez" in e["title"])
    assert suez["event_type"] == "maritime_disruption" and links[suez["id"]] == "chokepoint_suez"
    assert len(created) >= 4
    with ctx.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM articles WHERE kind IN ('hazard', 'logistics') "
                            "AND status = 'new'").fetchone()[0] == 0


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
