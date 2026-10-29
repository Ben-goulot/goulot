"""European media feeds (docs/DECISIONS.md, 29 October 2026): reader, stricter filter, copies judged without AI,
one lineage per headline, re-analyses capped, names in other languages, hot spots, report. No network."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx2
import pytest

from goulot.api.context import AppContext
from goulot.chain import store
from goulot.chain.identify import wikidata_labels
from goulot.collect import base, hotspots, report, runner, sources
from goulot.collect.base import Item
from goulot.collect.rules import active_rules, match
from goulot.collect.runner import _store, ensure_labels, queries, run_cycle
from goulot.llm.batch import ReplayBatchBackend
from goulot.net.allowlist import Allowlist
from goulot.net.client import build_client
from goulot.pipeline import analysis, grouping, triage
from goulot.pipeline.corroborate import ArticleRef, corroborate
from tests.test_collect import NOW, ROUTES, http, validated_chain
from tests.test_triage_events import result, triage_message

RSS2 = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Industrie</title>
<item><title>Explosion dans l'usine d'acrylonitrile de Rotterdam, production arr&#234;t&#233;e</title>
<link>https://www.lesechos.fr/industrie/acrylonitrile-rotterdam-123</link>
<pubDate>Sat, 18 Oct 2026 09:00:00 GMT</pubDate><description>&lt;p&gt;Le site &lt;b&gt;arr&#234;t&#233;&lt;/b&gt;&lt;/p&gt;</description></item>
<item><title>Ancien article</title><link>https://www.lesechos.fr/old</link><pubDate>Mon, 01 Sep 2026 09:00:00 GMT</pubDate></item>
<item><title>Lien pi&#233;g&#233;</title><link>javascript:alert(1)</link></item>
</channel></rss>"""
RDF = b"""<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>DW Business</title></channel>
<item><title>Strike at Rotterdam port halts chemical tankers</title><link>https://www.dw.com/en/strike/a-1</link>
<description>Dockers walked out.</description><dc:date>2026-10-18T08:00:00Z</dc:date></item></rdf:RDF>"""
ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Chemie</title>
<entry><title>Gro&#223;brand im Chemiepark Rotterdam</title><link rel="alternate" href="https://www.chemanager-online.com/a/1"/>
<updated>2026-10-18T07:00:00Z</updated><summary>Feuerwehr im Einsatz</summary></entry></feed>"""


def feed_client(body: bytes) -> httpx2.Client:
    return build_client(Allowlist({"feed.example"}),
                        transport=httpx2.MockTransport(lambda r: httpx2.Response(200, content=body)))


def media(title: str, excerpt: str = "", domain: str = "lesechos.fr") -> Item:
    return Item(source="media_lesechos", kind="news", url=f"https://{domain}/{abs(hash(title))}", title=title,
                excerpt=excerpt, domain=domain, queried=False)


# --- reader -----------------------------------------------------------------------------------------------
def test_rss_rdf_and_atom_feeds_read_as_text() -> None:
    since = NOW - timedelta(days=3)
    (item,) = sources.media_feed(feed_client(RSS2), "media_lesechos", "https://feed.example/rss", "fr", since)
    assert item.title.startswith("Explosion dans l'usine d'acrylonitrile") and item.excerpt == "Le site arrêté"
    assert item.domain == "lesechos.fr" and item.lang == "fr" and item.queried is False and item.kind == "news"
    (rdf,) = sources.media_feed(feed_client(RDF), "media_dw_business", "https://feed.example/rdf", "en", since)
    assert rdf.url == "https://www.dw.com/en/strike/a-1" and rdf.published_at is not None
    (atom,) = sources.media_feed(feed_client(ATOM), "media_chemanager", "https://feed.example/atom", "de", since)
    assert atom.url == "https://www.chemanager-online.com/a/1" and atom.title == "Großbrand im Chemiepark Rotterdam"


# --- stricter filter --------------------------------------------------------------------------------------
def test_feed_items_need_the_material_or_a_disruption_word(ctx: AppContext) -> None:
    validated_chain(ctx)
    with ctx.db.connect() as conn:
        rules = active_rules(conn)
    calm = "Le port de Rotterdam inaugure un nouveau terminal"
    assert match(media(calm), rules) == []  # a site named, nothing happens: left out
    assert match(Item(source="google_news_rss", kind="news", url="https://x.example/1", title=calm), rules)
    assert match(media("Grève au port de Rotterdam"), rules)  # site + disruption
    assert match(media("Streik im Hafen Rotterdam"), rules)
    assert match(media("Les prix de l'acrylonitrile se tendent"), rules)  # the material itself


# --- copies judged without AI -----------------------------------------------------------------------------
def store_items(ctx: AppContext, items: list[Item]) -> None:
    with ctx.db.connect() as conn:
        rules = active_rules(conn)
    with ctx.db.transaction() as conn:
        for it in items:
            assert _store(conn, it, match(it, rules), NOW)


@pytest.mark.parametrize("quarantine", [False, True])
def test_copy_takes_its_twins_judgment(ctx: AppContext, quarantine: bool) -> None:
    validated_chain(ctx)
    title = "Fire halts the acrylonitrile plant near Rotterdam for several weeks"
    store_items(ctx, [
        media(title, "The site is closed.", "lesechos.fr"),
        Item(source="google_news_rss", kind="news", url="https://news.google.com/rss/articles/zz", title=title,
             excerpt=f"{title} Les Echos", domain="news.google.com", provider_domain="lesechos.fr"),
    ])
    backend = ReplayBatchBackend()
    triage.submit_pending(ctx.db, ctx.budget, backend)
    (requests,) = backend.batches.values()
    assert len(requests) == 1  # the copy waits for its twin
    backend.responses = {requests[0][0]: triage_message(result(injection_suspected=quarantine))}
    triage.collect_pending(ctx.db, ctx.budget, backend)
    assert triage.submit_pending(ctx.db, ctx.budget, backend) is None  # no call for the copy
    with ctx.db.connect() as conn:
        rows = conn.execute("SELECT a.status, a.details, t.cost_usd, t.batch_id FROM articles a "
                            "JOIN triage t ON t.article_id = a.id ORDER BY a.id").fetchall()
    assert [r["status"] for r in rows] == (["quarantined"] * 2 if quarantine else ["triaged"] * 2)
    assert rows[1]["batch_id"] is None and rows[1]["cost_usd"] == 0
    assert "copy_of" in json.loads(rows[1]["details"])
    if not quarantine:
        with ctx.db.transaction() as conn:
            (event_id,) = grouping.group_triaged(conn)
            lineages = conn.execute("SELECT lineages FROM events WHERE id = ?", (event_id,)).fetchone()[0]
        assert lineages == 1  # one article read twice is one lineage


def test_same_headline_is_one_lineage() -> None:
    title = "Explosion dans l'usine d'acrylonitrile de Rotterdam, production arrêtée"
    one = ArticleRef(1, "https://lesechos.fr/a", "lesechos.fr", "", title, "Résumé du média", "unknown", 0.5)
    two = ArticleRef(2, "https://news.google.com/x", "news.google.com", "lesechos.fr", title, "Autre", "unknown", 0.5)
    short = ArticleRef(3, "https://lemonde.fr/b", "lemonde.fr", "", "Grève à Rotterdam",
                       "Les dockers cessent le travail pour trois jours", "unknown", 0.5)
    other = ArticleRef(4, "https://dw.com/b", "dw.com", "", "Grève à Rotterdam",
                       "Le terminal pétrolier tourne au ralenti depuis lundi", "unknown", 0.5)
    assert corroborate([one, two])[0].lineages == 1
    assert corroborate([short, other])[0].lineages == 2  # a short headline may be a coincidence


# --- re-analyses ------------------------------------------------------------------------------------------
def test_new_echoes_beyond_three_lineages_do_not_trigger_an_analysis() -> None:
    def event(confirmation: str, lineages: int, severity: int = 2, status: str = "ongoing") -> dict[str, Any]:
        return {"confirmation": confirmation, "lineages": lineages, "severity_hint": severity, "event_status": status}

    assert not analysis.signature_changed("confirmed|3|2|ongoing", event("confirmed", 6))  # type: ignore[arg-type]
    assert analysis.signature_changed("confirmed|2|2|ongoing", event("confirmed", 3))  # type: ignore[arg-type]
    assert analysis.signature_changed("confirmed|5|2|ongoing", event("confirmed", 5, 3))  # type: ignore[arg-type]
    assert analysis.signature_changed("early_signal|4|2|ongoing", event("confirmed", 4))  # type: ignore[arg-type]
    assert analysis.signature_changed("garbled", event("confirmed", 1))  # type: ignore[arg-type]


# --- names in other languages -----------------------------------------------------------------------------
LABELS = {"results": {"bindings": [
    {"label": {"xml:lang": "de", "type": "literal", "value": "Acrylnitril"}},
    {"label": {"xml:lang": "es", "type": "literal", "value": "acrilonitrilo"}},
    {"label": {"xml:lang": "it", "type": "literal", "value": "acrilonitrile\u0007"}},  # control character: left out
    {"label": {"xml:lang": "ru", "type": "literal", "value": "акрилонитрил"}},
]}}


def labels_client() -> httpx2.Client:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "query.wikidata.org":
            return httpx2.Response(200, json=LABELS)
        status, body = ROUTES.get((request.url.host, request.url.path), (404, b"{}"))
        return httpx2.Response(status, content=body)

    return build_client(Allowlist(set()), transport=httpx2.MockTransport(handler))


def test_labels_become_filter_rules_not_search_terms(ctx: AppContext) -> None:
    assert wikidata_labels(labels_client(), "107-13-1") == {"de": "Acrylnitril", "es": "acrilonitrilo"}
    assert wikidata_labels(labels_client(), "107-13-2") is None  # invalid CAS: never sent
    material_id = validated_chain(ctx)
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE materials SET labels = NULL WHERE id = ?", (material_id,))
    assert ensure_labels(ctx.db, labels_client()) == 1
    assert ensure_labels(ctx.db, labels_client()) == 0  # asked once
    with ctx.db.connect() as conn:
        rules = active_rules(conn)
        terms = " ".join(q for q, _ in queries(conn))
        labels = store.get_material(conn, material_id)["labels"]  # type: ignore[index]
    assert labels == {"de": "Acrylnitril", "es": "acrilonitrilo"}
    assert {"Acrylnitril", "acrilonitrilo"} <= {r.value for r in rules if r.kind == "keyword"}
    assert "Acrylnitril" not in terms  # Google News and GDELT keep the French and English names
    assert match(media("Acrylnitril wird knapp"), rules)


# --- cycle and hot spots ----------------------------------------------------------------------------------
def test_cycle_reads_an_enabled_feed_and_a_failing_feed_fails_alone(ctx: AppContext,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    config = dict(base.load_sources())
    config["media_lesechos"] = {**config["media_lesechos"], "enabled": True}
    monkeypatch.setattr(base, "load_sources", lambda: config)
    monkeypatch.setattr(runner, "load_sources", lambda: config)
    routes = {**ROUTES, ("syndication.lesechos.fr", "/rss/rss_industrie-services.xml"): (200, RSS2),
              ("www.lemonde.fr", "/economie/rss_full.xml"): (503, b"Backend fetch failed")}
    validated_chain(ctx)
    outcome = run_cycle(ctx.db, http(routes), lambda name: None, NOW)
    with ctx.db.connect() as conn:
        runs = {r["source"]: (r["ok"], r["family"]) for r in conn.execute(
            "SELECT source, ok, family FROM collect_runs WHERE source IN ('media_lesechos', 'media_lemonde_economie')")}
        kept = [r[0] for r in conn.execute("SELECT title FROM articles WHERE source = 'media_lesechos'")]
    assert runs == {"media_lesechos": (1, "media"), "media_lemonde_economie": (0, "media")}
    assert outcome.ok  # the news and hazard families answered: a media feed down does not stop the watch
    assert len(kept) == 1 and "acrylonitrile" in kept[0]


def test_media_headlines_count_for_the_straits_in_crisis() -> None:
    items = [
        media("Angriff auf Tanker in der Straße von Hormus", domain="handelsblatt.com"),
        media("Détroit d'Ormuz : nouvelle attaque contre un pétrolier", domain="usinenouvelle.com"),
        media("Ataque a un petrolero en el estrecho de Ormuz", domain="elpais.com"),
    ]
    spots = hotspots.news_hotspots(items, NOW)
    assert [s["id"] for s in spots] == ["strait_news:hormuz"] and spots[0]["source_name"] == "usinenouvelle.com"


# --- report -----------------------------------------------------------------------------------------------
def test_report_shows_triage_outcomes_copies_and_cost(ctx: AppContext) -> None:
    validated_chain(ctx)
    store_items(ctx, [media("Grève au port de Rotterdam, les chimistes inquiets")])
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE articles SET status = 'irrelevant'")
        conn.execute("INSERT INTO llm_calls (at, budget, purpose, model, mode, cost_usd) "
                     "VALUES (?, 'watch', 'triage', 'claude-haiku-5-5', 'batch', 0.014)", (NOW.isoformat(),))
        data = report.history(conn, NOW)
    text = report.render(data, None)
    assert data["outcomes"]["media_lesechos"] == {"irrelevant": 1}
    assert "media_lesechos" in text and "0 % pertinents" in text
    assert "Copies jugées sans appel IA" in text and "Coût IA de la veille : 0.01 $ sur 14 jours" in text


def test_check_sources_shows_where_a_refused_redirect_leads() -> None:
    from goulot.cli.check_sources import redirect_target

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(301, headers={"location": "https://feeds.other.example/x"})

    client = build_client(Allowlist({"feed.example"}), transport=httpx2.MockTransport(handler))
    assert redirect_target(client, "https://feed.example/rss") == "https://feeds.other.example/x"  # shown, not followed
