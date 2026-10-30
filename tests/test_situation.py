"""30 October 2026: the situation behind a verdict, old articles, hazards spread over several countries, world hot
spots only where they touch the chain. Frozen data, no network."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx2
from fastapi.testclient import TestClient

from goulot.api.context import AppContext
from goulot.chain import geo, mapdata
from goulot.chain.countries import iso2_from_name, name_fr
from goulot.collect import hotspots, sources
from goulot.collect.base import Item
from goulot.collect.rules import Rule, match, too_old
from goulot.collect.runner import run_cycle
from goulot.net.allowlist import Allowlist
from goulot.net.client import build_client
from goulot.pipeline import assess
from goulot.pipeline.verdict import ANTICIPATE, POTENTIAL, describe
from goulot.pipeline.visibility import from_old_news
from tests.conftest import login
from tests.test_collect import NOW, ROUTES, http, validated_chain
from tests.test_screens_api import prepared

DROUGHT = b"""<?xml version="1.0"?><rss xmlns:georss="http://www.georss.org/georss" xmlns:gdacs="http://www.gdacs.org">
<channel><item><title>Drought is on going in Andorra, Albania, Austria, Germany</title>
<link>https://www.gdacs.org/report.aspx?eventid=9</link><guid>DR9</guid><pubDate>Wed, 11 Feb 2026 03:00:00 GMT</pubDate>
<georss:point>48.0 12.0</georss:point><gdacs:eventtype>DR</gdacs:eventtype><gdacs:alertlevel>Orange</gdacs:alertlevel>
<gdacs:country>Andorra, Albania, Austria, Germany</gdacs:country></item>
<item><title>Tropical cyclone</title><link>https://www.gdacs.org/report.aspx?eventid=8</link><guid>TC8</guid>
<georss:point>20.0 120.0</georss:point><gdacs:eventtype>TC</gdacs:eventtype><gdacs:alertlevel>Red</gdacs:alertlevel>
<gdacs:country>Philippines, Taiwan, China, Japan</gdacs:country></item>
<item><title>Drought somewhere</title><link>https://www.gdacs.org/report.aspx?eventid=7</link><guid>DR7</guid>
<georss:point>10.0 10.0</georss:point><gdacs:eventtype>DR</gdacs:eventtype><gdacs:alertlevel>Red</gdacs:alertlevel>
<gdacs:country>Atlantis</gdacs:country></item></channel></rss>"""


def gdacs_items() -> list[Item]:
    client = build_client(Allowlist({"www.gdacs.org"}), transport=httpx2.MockTransport(
        lambda r: httpx2.Response(200, content=DROUGHT)))
    return sources.gdacs(client)


# --- the words of a verdict -------------------------------------------------------------------------------
def test_verdict_text_says_what_a_late_order_means() -> None:
    late = describe(ANTICIPATE, 1, 1, 1.0, 1.0, -3.0, 1.0, True, False)
    assert "1 événement retenu." in late and "environ 1 semaine." in late
    assert "(délai : 4 semaines supposées) arriverait environ 3 semaines après le début de l'impact" in late
    assert "-3" not in late and "1 à 1" not in late and "règle" not in late
    on_time = describe(POTENTIAL, 1, 2, 5.0, 7.0, 2.0, 6.0, False, False)
    assert "2 événements retenus." in on_time and "dans 5 à 7 semaines" in on_time
    assert "Il reste environ 2 semaines pour commander à temps (délai : 4 semaines)." in on_time
    assert "au moment de l'impact" in describe(ANTICIPATE, 1, 1, 4.0, 4.0, 0.0, 4.0, True, False)


def test_sheet_shows_the_situation_of_the_event_behind_the_verdict(client: TestClient, ctx: AppContext) -> None:
    material_id, event_id = prepared(ctx)
    login(client, ctx)
    sheet = client.get(f"/api/materials/{material_id}/sheet").json()
    assert sheet["verdict"]["event_ids"] == [event_id]
    (e,) = sheet["situation"]
    assert e["id"] == event_id and e["analysed"] is True and e["severity"] == 2
    assert e["summary"] and e["headline"] and isinstance(e["transmission"], list) and isinstance(e["triggers"], list)
    assert e["sources"] and {"title", "url", "publisher", "published_at", "reliability"} <= set(e["sources"][0])
    assert e["letter"] == next(x["letter"] for x in sheet["events"] if x["id"] == event_id)
    assert "délai : 4 semaines supposées" in sheet["verdict"]["explanation"]  # also on a verdict already stored


def test_a_year_of_prices_reaches_the_sheet() -> None:
    from datetime import date
    from types import SimpleNamespace

    from goulot.api.watch_routes import _series_payload

    points = [(date(2025, 1, 1) + timedelta(days=i), 100.0 + i) for i in range(600)]
    series = SimpleNamespace(name="x", rank=1, frequency="daily", source="czce", points=points)
    payload = _series_payload(series, 12)
    assert payload is not None and len(payload["points"]) == 361
    assert payload["points"][-1]["day"] == points[-1][0].isoformat()


# --- old articles -----------------------------------------------------------------------------------------
def test_an_old_article_never_reaches_the_triage(ctx: AppContext) -> None:
    old = Item(source="google_news_rss", kind="news", url="https://x.example/a", title="Explosion à l'usine",
               published_at=NOW - timedelta(days=31))
    assert too_old(old, NOW)
    assert not too_old(Item(source="x", kind="news", url="https://x.example/b", title="y",
                            published_at=NOW - timedelta(days=29)), NOW)
    assert not too_old(Item(source="x", kind="news", url="https://x.example/c", title="sans date"), NOW)
    assert not too_old(Item(source="x", kind="policy", url="https://x.example/d", title="acte",
                            published_at=NOW - timedelta(days=400)), NOW)
    validated_chain(ctx)
    feed = (b"<rss><channel><item><title>Explosion at acrylonitrile plant near Rotterdam stops production</title>"
            b"<link>https://news.google.com/rss/articles/old</link><pubDate>Mon, 09 Dec 2024 06:00:00 GMT</pubDate>"
            b"<source url='https://www.reuters.com'>Reuters</source></item></channel></rss>")
    run_cycle(ctx.db, http({**ROUTES, ("news.google.com", "/rss/search"): (200, feed)}), lambda name: None, NOW)
    with ctx.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM articles WHERE url LIKE '%articles/old'").fetchone()[0] == 0


def test_an_event_made_only_of_old_news_leaves_verdict_analysis_and_sheet(client: TestClient, ctx: AppContext) -> None:
    material_id, event_id = prepared(ctx)
    login(client, ctx)
    with ctx.db.transaction() as conn:
        assert not from_old_news(conn, event_id)  # a measurement, not a press article
        conn.execute("UPDATE articles SET kind = 'news', published_at = '2024-12-09T06:00:00+00:00' WHERE id IN "
                     "(SELECT article_id FROM event_articles WHERE event_id = ?)", (event_id,))
        assert from_old_news(conn, event_id)
        assert event_id not in [e.id for e in assess.event_inputs(conn, material_id, NOW)]
        assert event_id not in [e["id"] for e in assess.due_events(conn, assess.Allowance())]
    sheet = client.get(f"/api/materials/{material_id}/sheet").json()
    assert event_id not in [e["id"] for e in sheet["events"]] and sheet["situation"] == []


# --- hazards over several countries -----------------------------------------------------------------------
def test_country_names_of_gdacs_are_recognized() -> None:
    assert iso2_from_name("Germany") == "DE" and iso2_from_name("Türkiye") == "TR"
    assert iso2_from_name("Viet Nam") == "VN" and iso2_from_name("Democratic Republic of Congo") == "CD"
    assert iso2_from_name("Atlantis") is None and name_fr("AL") == "Albanie"


def test_a_drought_covers_its_countries_and_not_a_circle_around_its_centre() -> None:
    drought, cyclone, unknown = gdacs_items()
    assert drought.details["area_wide"] is True and drought.details["countries"] == ["AD", "AL", "AT", "DE"]
    assert cyclone.details["area_wide"] is False  # a cyclone's point is its real position
    assert unknown.details["countries"] == []
    spots = hotspots.gdacs_hotspots([drought, cyclone, unknown])
    assert len(spots) == 2  # no recognized country: left out rather than drawn at the wrong place
    area = spots[0]
    assert area["label"] == "Sécheresse : 4 pays (Albanie, Allemagne…)"
    tirana, munich, milan = (41.33, 19.82), (48.14, 11.58), (45.46, 9.19)
    inside = lambda p: any(geo._inside(p[1], p[0], ring) for ring in area["rings"])  # noqa: E731
    assert inside(tirana) and inside(munich) and not inside(milan)
    assert len(spots[1]["rings"]) == 1  # the cyclone keeps its circle


def test_the_centre_of_a_territory_creates_no_event_near_a_site() -> None:
    near = Rule(1, "m", "zone", "site", "site", 48.1, 12.1, 50.0)
    drought = gdacs_items()[0]
    assert match(drought, [near]) == []
    quake = Item(source="usgs", kind="hazard", url="https://x.example", title="M 6", lat=48.0, lon=12.0,
                 details={"event_type": "EQ"})
    assert match(quake, [near]) == [near]


# --- the map of a material --------------------------------------------------------------------------------
def map_data(*zones: dict[str, object]) -> dict[str, object]:
    return {
        "nodes": [
            {"node_key": "site", "lat": 51.9, "lon": 4.1, "coord_source": "city", "country_iso2": "NL"},
            {"node_key": "cn", "lat": 35.0, "lon": 103.0, "coord_source": "country", "country_iso2": "CN"},
        ],
        "flows": [{"coords": [[4.1, 51.9], [32.5, 30.0]]}],
        "zones": list(zones),
    }


def spot(zone_id: str, lat: float, lon: float, kind: str = "hotspot") -> dict[str, object]:
    from goulot.chain import places

    return {"id": zone_id, "kind": kind, "lat": lat, "lon": lon, "rings": [places.circle(lat, lon, 100)]}


def test_map_keeps_only_the_hot_spots_touching_the_chain() -> None:
    data = map_data(
        spot("on_site", 51.9, 4.1), spot("on_flow", 30.0, 32.5), spot("in_country_of_a_vague_node", 23.0, 113.0),
        spot("crete", 35.2, 24.9), spot("event_far", -30.0, 150.0, "event"),
    )
    out = mapdata.mark_exposure(data)
    assert {z["id"] for z in out["zones"]} == {"on_site", "on_flow", "in_country_of_a_vague_node", "event_far"}
    site, vague = out["nodes"]
    assert site["exposed_by"] == ["on_site"] and vague["exposed_by"] == ["in_country_of_a_vague_node"]


def test_world_list_tells_which_chains_are_touched(client: TestClient, ctx: AppContext) -> None:
    material_id = validated_chain(ctx)
    login(client, ctx)
    with ctx.db.transaction() as conn:
        hotspots._replace(conn, "gdacs", [
            {"id": "gdacs:near", "kind": "gdacs", "label": "Alerte à Rotterdam", "status": "alerte orange", "lat": 51.95,
             "lon": 4.14, "rings": [hotspots.places.circle(51.95, 4.14, 100)], "source_url": "https://www.gdacs.org/a",
             "source_name": "GDACS", "started_at": "2026-10-18"},
            {"id": "gdacs:far", "kind": "gdacs", "label": "Alerte en Crète", "status": "alerte orange", "lat": 35.2,
             "lon": 24.9, "rings": [hotspots.places.circle(35.2, 24.9, 100)], "source_url": "https://www.gdacs.org/b",
             "source_name": "GDACS", "started_at": "2026-10-18"},
        ], NOW)
        name = conn.execute("SELECT name FROM materials WHERE id = ?", (material_id,)).fetchone()[0]
    spots = {s["id"]: s for s in client.get("/api/hotspots").json()["hotspots"]}
    assert spots["gdacs:near"]["materials"] == [name] and spots["gdacs:far"]["materials"] == []
    shown = [z["id"] for z in client.get(f"/api/materials/{material_id}/map").json()["zones"]]
    assert "gdacs:near" in shown and "gdacs:far" not in shown
    assert json.dumps(spots).count("rings") == 0  # a list, no outlines sent


def test_hot_spots_need_a_session(client: TestClient) -> None:
    assert client.get("/api/hotspots").status_code in (401, 403)
