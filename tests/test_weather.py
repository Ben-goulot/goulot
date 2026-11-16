"""Official weather alerts set against the past of the plants they cover (14 November 2026). Fictitious alerts with
the fields of the real ones (checked on api.weather.gov), fictitious companies, no network."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx2
import pytest

from goulot.collect import records, tceq, weather
from goulot.collect.rules import Rule, match, title_only
from goulot.collect.sources import SourceError
from goulot.db.connection import Database
from goulot.net.allowlist import Allowlist
from goulot.net.client import build_client
from goulot.pipeline import grouping
from tests.test_records import (
    BAYOU,
    FEED,
    NALK,
    NOW,
    PLANTS,
    SOURCE,
    VERDA,
    ZED,
    Filed,
    Registry,
    db,
    habit,
    plant,
    site,
)

__all__ = ["db"]  # the scratch base of tests/test_records.py

NIAGARA = site("Nalk Niagara", "Niagara Falls, New York", "nalk_ny")


def alert(event: str, vtec: str, counties: list[str], message: str = "Alert", onset_days: float = 2,
          area: str = "Coastal county") -> dict[str, Any]:
    number = vtec.split(".")[5] if vtec else "1"
    return {"type": "Feature", "geometry": None, "properties": {
        "@id": f"https://api.weather.gov/alerts/urn:oid:2.49.0.1.840.0.{abs(hash((event, vtec))) % 10**9}.{number}",
        "id": f"urn:oid:2.49.0.1.840.0.{number}", "areaDesc": area, "status": "Actual",
        "messageType": message, "event": event, "severity": "Severe",
        "geocode": {"SAME": ["0" + c for c in counties], "UGC": ["TXZ337"]},
        "sent": NOW.isoformat(), "onset": (NOW + timedelta(days=onset_days)).isoformat(),
        "ends": (NOW + timedelta(days=onset_days + 2)).isoformat(),
        "headline": f"{event} issued November 13 by NWS <script>alert(1)</script>",
        "parameters": {"VTEC": [vtec]} if vtec else {}}}


def freeze_of_the_past() -> Filed:
    """What the fictitious registry holds of the plant's worst stop: 19 days, a freeze."""
    start = NOW - timedelta(days=640)
    return Filed("3001", "RN100000001", "BLUE HARBOR OPERATIONS", "BLUE HARBOR OPERATIONS LLC", "BRAZORIA",
                 "EMISSIONS EVENT", start, start + timedelta(hours=463),
                 cause="During a severe winter storm the power supply was interrupted: facility-wide outage.")


def storm_of_the_past() -> Filed:
    """A company without any place: the registry knows one of its installations, in Orange County."""
    start = NOW - timedelta(days=400)
    return Filed("3002", "RN100000003", "ZED TEXAS OPERATIONS", "THE ZED CHEMICAL COMPANY", "ORANGE",
                 "EMISSIONS EVENT", start, start + timedelta(hours=40),
                 cause="Units were shut down ahead of the hurricane; flaring during the shutdown.")


def test_an_alert_is_set_against_what_such_weather_did_to_the_plant(db: Database) -> None:
    registry = Registry([*habit(), freeze_of_the_past(), storm_of_the_past()], PLANTS)
    registry.plants.append(plant("NALK CORP - NIAGARA PLANT", "NIAGARA FALLS", "NIAGARA", "NY", "NALK CORP"))
    client = registry.client()
    sites = [NALK, VERDA, BAYOU, NIAGARA, ZED]
    # The registry is read first: the plant is found, its past read, the cause of its long stop with it.
    tceq.collect(db, client, SOURCE, FEED, NOW, sites, budget=tceq.Budget(identities=9, sites=9, backfills=9))
    assert weather.collect(db, client, NOW, sites) == []  # a calm sky, checked state by state
    registry.alerts = {
        "TX": [alert("Hard Freeze Watch", "/O.NEW.KHGX.HZ.A.0001.261115T0600Z-261116T1800Z/", ["48039", "48167"]),
               alert("Hard Freeze Warning", "/O.NEW.KHGX.HZ.W.0001.261115T0600Z-261116T1800Z/", ["48039"]),
               alert("Hurricane Watch", "/O.NEW.KHGX.HU.A.1009.000000T0000Z-000000T0000Z/", ["48039"]),
               alert("Wind Advisory", "/O.NEW.KHGX.WI.Y.0008.261115T1500Z-261116T2100Z/", ["48039", "48057"]),
               alert("Winter Storm Warning", "/O.CAN.KHGX.WS.W.0002.000000T0000Z-261116T1800Z/", ["48057"]),
               alert("Ice Storm Warning", "/O.NEW.KCRP.IS.W.0001.261115T0600Z-261116T1800Z/", ["48057"], "Cancel"),
               alert("Tropical Storm Warning", "/O.NEW.KLCH.TR.W.1009.000000T0000Z-000000T0000Z/", ["48361"],
                     area="Northern Orange; Southern Orange"),
               alert("Tropical Storm Warning", "/O.NEW.KLCH.TR.W.1009.000000T0000Z-000000T0000Z/", ["48351"],
                     area="Orangefield Heights; Newton")],
        "LA": [alert("Winter Storm Watch", "", ["22019"])],  # without its code: read from its name
        "NY": [alert("Winter Storm Warning", "/O.NEW.KBUF.WS.W.0004.261115T0600Z-261116T1800Z/", ["36063"])],
    }
    items = {(i.details["county"], i.details["hazard"]): i for i in weather.collect(db, client, NOW, sites)}
    assert sorted(items) == [("Brazoria", "freeze"), ("Brazoria", "tropical"), ("Calcasieu", "freeze"),
                             ("Niagara", "freeze"), ("Orange", "tropical")]  # Calhoun: a wind advisory, two withdrawn
    # A company without any place: the county of the installation the registry bound to it, found by its name among
    # the alert's zones (whole words: « Orangefield » is not « Orange »), with what a storm did to it before.
    orange = items[("Orange", "tropical")]
    assert orange.details["sites"] == ["Zed"] and orange.details["severity"] == 3
    assert "Zed (arrêté ou perturbé 40 h par un ouragan ou une tempête tropicale en 10/2025" in orange.excerpt
    freeze = items[("Brazoria", "freeze")]
    assert freeze.title == "Alerte météo officielle, comté de Brazoria (Texas) : gel dur, avertissement"
    assert freeze.kind == "hazard" and freeze.source == "nws_alerts" and not title_only(freeze)
    assert "gel dur, avertissement (attendu ou en cours)" in freeze.excerpt
    assert ("Nalk Freeport (arrêté ou perturbé 19 jours par un épisode de gel en 02/2025 (déclaration au registre))"
            in freeze.excerpt)
    assert "à partir du 15/11/2026 18:00 UTC" in freeze.excerpt and "alert(1)" not in freeze.excerpt
    # The warning stands for the county, not the watch; a plant already stopped that way raises it to the top.
    assert freeze.details["level"] == "warning" and freeze.details["severity"] == 3
    assert freeze.details["sites"] == ["Nalk Freeport"] and (freeze.lat, freeze.lon) == (28.95, -95.35)
    # A hurricane never stopped it, as far as the registry says: the watch keeps its own level.
    tropical = items[("Brazoria", "tropical")]
    assert tropical.details["severity"] == 2 and "aucun arrêt de ce genre déclaré au registre" in tropical.excerpt
    assert tropical.identity() != freeze.identity()
    # Louisiana: no registry follows the plant. The alert is still an alert; its past is said unknown.
    unknown = items[("Calcasieu", "freeze")]
    # A freeze watch on the Gulf Coast is analysed even without any past: a freeze there is the anomaly.
    assert unknown.details["severity"] == 2 and "passé inconnu : aucun registre ne suit ce site" in unknown.excerpt
    assert unknown.title.startswith("Alerte météo officielle, comté de Calcasieu (Louisiane) : tempête hivernale, veille")
    # A winter storm in a cold state: its plants are built for it.
    assert items[("Niagara", "freeze")].details["severity"] == 1
    # Asked by state: nothing of the chains is sent to the weather service, which is told only who asks.
    asked = [r for r in registry.seen if r.url.host == "api.weather.gov"]
    assert {r.url.params["area"] for r in asked} == {"TX", "LA", "NY"}
    assert all(set(r.url.params) == {"area"} and "Goulot" in r.headers["user-agent"] for r in asked)
    # The same alert at the next cycle is the same item; the watch pipeline keeps it once.
    again = {(i.details["county"], i.details["hazard"]): i for i in weather.collect(db, client, NOW, sites)}
    assert again[("Brazoria", "freeze")].identity() == freeze.identity()
    # Through the rule filter and into an event: the site's own name links it, the code gives the severity.
    rules = [Rule(1, "m1", "keyword", "Nalk Freeport", "nalk", None, None, None),
             Rule(2, "m1", "zone", "verda", "verda", 28.95, -95.35, 50.0)]
    assert [r.id for r in match(freeze, rules)] == [1]  # by its sites, never by a point of the county
    assert grouping._measurement_severity("nws_alerts", {"magnitude": None}, freeze.details) == 3  # type: ignore[arg-type]
    assert grouping.HAZARD_TYPES["WX"] == "extreme_weather"
    with db.connect() as conn:
        subjects = {s.node_key: s.subject for s in sites}
        shown = weather.covered(conn, "m1", subjects)
        assert shown["nalk"] == {"covered": True, "text": (
            "Alertes météo officielles suivies sur son comté (Brazoria, TX).")}
        assert set(shown) == {"nalk", "verda", "verda_la", "nalk_ny"}
        assert weather.covered(conn, "m1", {"nalk": "Autre usine|Ailleurs"}) == {}  # a node rebuilt as another site
        assert records.antecedent(conn, SOURCE, ["RN100000001"], "tropical", NOW) is None


def test_an_unreadable_answer_is_an_error_not_a_calm_sky(db: Database) -> None:
    registry = Registry(habit(), PLANTS)
    weather.identity.ensure(db, registry.client(), [NALK], NOW)
    for body in ({"title": "Service Unavailable"}, ["not", "alerts"]):
        answers = iter([httpx2.Response(200, json=body)])
        broken = build_client(Allowlist(set()), transport=httpx2.MockTransport(lambda r, a=answers: next(a)))
        with pytest.raises(SourceError):
            weather.collect(db, broken, NOW, [NALK])
    down = build_client(Allowlist(set()), transport=httpx2.MockTransport(lambda r: httpx2.Response(503)))
    with pytest.raises(SourceError):
        weather.collect(db, down, NOW, [NALK])
    # A plant the EPA does not know: the chain screen says its county is unknown and the alerts not followed.
    orbe = site("Orbe Chemicals, Baytown", "Baytown, Texas")
    weather.identity.ensure(db, registry.client(), [orbe], NOW)
    with db.connect() as conn:
        shown = weather.covered(conn, "m1", {"orbe": orbe.subject})
    assert shown["orbe"]["covered"] is False and "ne sont pas suivies" in str(shown["orbe"]["text"])
    # No American plant with a known county: nothing is asked at all.
    assert weather.collect(db, down, NOW, [site("Usine fictive, Anvers", "Anvers", iso="BE")]) == []
