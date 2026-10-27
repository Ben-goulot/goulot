"""Copernicus EMS and EUR-Lex (docs/DECISIONS.md, 27 October 2026). Fictitious frozen answers, no network."""

from __future__ import annotations

from datetime import timedelta

from goulot.api.context import AppContext
from goulot.collect import hotspots, sources
from goulot.collect.runner import run_cycle
from tests.test_collect import NOW, http, validated_chain


def test_copernicus_activations_of_the_last_30_days() -> None:
    items = sources.copernicus(http(), NOW)
    assert [it.key for it in items] == ["cems:EMSR999", "cems:EMSR998"]  # trap code and old activation dropped
    plant = items[0]
    assert (plant.lat, plant.lon) == (51.94, 4.15) and plant.kind == "hazard"
    assert plant.details["event_type"] == "IND" and plant.details["closed"] is False
    assert plant.url == "https://mapping.emergency.copernicus.eu/activations/EMSR999/"
    assert sources._wkt_point("POINT (200 10)") is None and sources._wkt_point("nonsense") is None


def test_copernicus_hot_spots_carry_the_credit() -> None:
    spots = hotspots.copernicus_hotspots(sources.copernicus(http(), NOW))
    assert [s["id"] for s in spots] == ["copernicus:EMSR999", "copernicus:EMSR998"]
    assert spots[0]["source_name"] == "© Union européenne, Copernicus Emergency Management Service"
    assert spots[0]["status"] == "activation Copernicus" and len(spots[0]["rings"][0]) == 25


def test_eurlex_acts_since_the_last_read() -> None:
    items = sources.eurlex(http(), NOW - timedelta(days=3))
    (act, other) = items  # the non-act and the old act are left out
    assert act.key == "eurlex:32026R9001" and act.kind == "policy"
    assert act.url == "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32026R9001"
    assert act.title.startswith("Commission Implementing Regulation (EU) 2026/9001")
    assert other.details["celex"] == "32026D9002"


def test_cycle_keeps_the_act_and_the_activation_near_a_site(ctx: AppContext) -> None:
    validated_chain(ctx)
    run_cycle(ctx.db, http(), lambda name: None, NOW)
    with ctx.db.connect() as conn:
        kept = {r["source"]: r["title"] for r in conn.execute(
            "SELECT source, title FROM articles WHERE source IN ('eurlex_oj_l', 'copernicus_ems')")}
        spots = {r[0] for r in conn.execute("SELECT id FROM hotspots WHERE kind = 'copernicus'")}
        runs = dict(conn.execute("SELECT source, ok FROM collect_runs WHERE source IN "
                                 "('eurlex_oj_l', 'copernicus_ems')").fetchall())
    assert runs == {"eurlex_oj_l": 1, "copernicus_ems": 1}
    assert "acrylonitrile" in kept["eurlex_oj_l"]  # the assistance measure names nothing watched: not kept
    assert "Rotterdam" in kept["copernicus_ems"]  # within 50 km of the port of Rotterdam
    assert spots == {"copernicus:EMSR999", "copernicus:EMSR998"}
