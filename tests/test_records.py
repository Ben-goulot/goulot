"""Official records followed over time (14 November 2026): who operates a site, which installation it is, what each
site usually declares, and what departs from that habit. A fictitious registry with the pages of the real one
(checked from the Mac), fictitious companies, no network."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest

from goulot.collect import identity, records, tceq
from goulot.collect.records import Declaration
from goulot.collect.sitematch import build_site, names_site
from goulot.db.connection import Database
from goulot.net.allowlist import Allowlist
from goulot.net.client import build_client

SOURCE = "tceq_emissions"
FEED = "https://www2.tceq.texas.gov/oce/eer/RSSDataFeed.xml"
NOW = datetime(2026, 11, 13, 18, 0, tzinfo=UTC)


def site(name: str, place: str = "unknown", key: str = "", iso: str = "US") -> Any:
    built = build_site("m1", key or name.split()[0].lower(), name, place, iso)
    assert built is not None, name
    return built


NALK = site("Nalk Freeport", "Freeport, Texas")  # its plant is run by a subsidiary with another name
VERDA = site("Verda Nitriles, Green Lake", "Port Lavaca, Texas")
BAYOU = site("Verda Nitriles, Lake Charles", "Lake Charles", "verda_la")  # Louisiana: the Texas registry cannot see it
ZED = site("Zed")  # a company without any place


def texas(day: datetime) -> str:
    return f"{day.astimezone(tceq.TEXAS_TIME):%m/%d/%Y %I:%M %p}"


@dataclass
class Filed:
    """One declaration as the fictitious registry holds it."""

    ext_id: str
    ref: str
    entity: str
    owner: str
    county: str
    kind: str
    start: datetime
    end: datetime | None
    final: bool = True
    cause: str = "A compressor tripped. Process gas was routed to the flare."
    unit: str = "Unit 2"
    compounds: str = "Acrylonitrile 12.0 POUNDS 0.0 N/A"
    entity_label: str = "Regulated Entity Name:"

    def page(self) -> str:
        status, report = ("CLOSED", "FINAL") if self.final else ("OPEN", "INITIAL")
        end = texas(self.end) if self.end else ""
        return (
            f"<html><body><table><tr><td>Incident Status:</td><td>{status}</td><td>Report Type:</td><td>{report}</td>"
            f"<td>Report Date:</td><td>11/12/2026</td></tr><tr><td>Name of Owner or Operator:</td><td>{self.owner}"
            f"</td><td>CN:</td><td>CN600000001</td></tr><tr><td>{self.entity_label}</td><td>{self.entity}</td>"
            f"<td>RN:</td><td>{self.ref}</td></tr><tr><td>Physical Location:</td><td>1 MI S OF FM 1</td>"
            f"<td>County:</td><td>{self.county}</td></tr><tr><td>Event/Activity Type:</td><td>{self.kind}</td></tr>"
            f"<tr><td>Date and Time Event Discovered or Scheduled Activity Start:</td><td>{texas(self.start)}</td>"
            f"<td>Date and Time Event or Scheduled Activity Ended:</td><td>{end}</td><td>Event Duration:</td>"
            f"<td>x hours</td></tr><tr><th>Process Unit or Area Common Names</th></tr><tr><td>{self.unit}</td></tr>"
            "<tr><th>Facility Common Name</th></tr><tr><td>List of Air Contaminant Compounds - 1 total Description "
            f"Est. Quantity/Opacity Units Emission Limit Units Authorization {self.compounds}</td></tr><tr><td>Cause "
            "of Emission Event or Excess Opacity Event, or Reason for Scheduled Activity:</td>"
            f"<td>{self.cause} <script>alert(1)</script></td></tr><tr><td>Actions Taken, or Being Taken, to Minimize "
            "Emissions And/or Correct the Situation:</td><td>Restart.</td></tr><tr><td>Initial Notification "
            f"Date/Time:</td><td>{texas(self.start + timedelta(hours=18))}</td><td>Method:</td><td>STEERS</td></tr>"
            "</table></body></html>")

    def row(self) -> str:
        return (
            f'<tr class="tablesaltrow"> <td><a href="index.cfm?fuseaction=main.getDetails&amp;target={self.ext_id}">'
            f"{self.ext_id}</a></td> <td>{self.ref}</td> <td>{self.entity}</td> <td> {texas(self.start)} </td> "
            f"<td> {texas(self.end) if self.end else ''} </td> <td> {self.kind} </td> "
            f"<td>{'FINAL' if self.final else 'INITIAL'}</td> <td>11/02/2026</td> <td> CN600000001 {self.owner} </td> "
            "</tr>")


class Registry:
    """The fictitious Texas registry and the fictitious EPA list of plants."""

    def __init__(self, filed: list[Filed], plants: list[dict[str, Any]] | None = None, quiet: bool = False
                 ) -> None:
        """Unless `quiet`, the feed always carries one declaration of an oil field, as the real one always does."""
        self.filed = {f.ext_id: f for f in filed}
        self.quiet = quiet
        self.alerts: dict[str, list[dict[str, Any]]] = {}  # state → weather alerts standing there
        self.archive: list[dict[str, Any]] = []  # past weather alerts, as the university archive lists them
        self.plants = list(plants or [])  # a copy: a test that adds a plant changes no other test
        self.seen: list[httpx2.Request] = []
        self.now = NOW

    def asked(self, what: str) -> int:
        return len([r for r in self.seen if what in str(r.url)])

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        if not self.quiet:
            self.filed["8000"] = Filed("8000", "RN199999999", "WEST TANK BATTERY", "ANOTHER OPERATOR LP", "ANDREWS",
                                       "EMISSIONS EVENT", self.now - timedelta(hours=2), self.now - timedelta(hours=1))
        q = request.url.params
        if request.url.host == "mesonet.agron.iastate.edu":
            if request.url.path.endswith("vtec_events_bypoint.py"):
                return httpx2.Response(200, json={"events": self.archive})
            found = [e for e in self.archive if (e["phenomena"], e["significance"], str(e["eventid"])) == (
                q["phenomena"], q["significance"], q["etn"])]
            return httpx2.Response(200, json={"ugcs": [{"utc_product_issue": e["published"]} for e in found]})
        if request.url.host == "api.weather.gov":
            return httpx2.Response(200, json={"type": "FeatureCollection", "features": self.alerts.get(q["area"], [])})
        if request.url.host == "data.epa.gov":
            field, word = request.url.path.split("/tri_facility/")[1].split("/CONTAINING/")
            return httpx2.Response(200, json=[p for p in self.plants if word.split("/")[0] in p[field]])
        if request.url.path.endswith("RSSDataFeed.xml"):
            items = "".join(
                f"<item><title>{f.ext_id} - {f.ref} {f.entity}</title><link>https://www2.tceq.texas.gov/oce/eer/"
                f"index.cfm?fuseaction=main.getDetails&amp;target={f.ext_id}</link>"
                f"<pubDate>{f.start:%a, %d %b %Y %H:%M}</pubDate></item>"
                for f in self.filed.values() if self.now - f.start <= timedelta(days=7))
            return httpx2.Response(200, text=f'<?xml version="1.0"?><rss version="2.0"><channel>{items}</channel></rss>')
        if q.get("fuseaction") == "main.getDetails":
            f = self.filed.get(q.get("target", ""))
            return httpx2.Response(200, content=f.page().encode("latin-1")) if f else httpx2.Response(500)
        begin = datetime.strptime(q["event_start_beg_dt"], "%m/%d/%Y").replace(tzinfo=tceq.TEXAS_TIME)
        end = datetime.strptime(q["event_start_end_dt"], "%m/%d/%Y").replace(tzinfo=tceq.TEXAS_TIME)
        rows = [f for f in self.filed.values() if begin <= f.start < end + timedelta(days=1)
                and q.get("rn_txt", f.ref) == f.ref and q.get("cust_name", "") in f.owner
                and q.get("ls_cnty_name", f.county) == f.county]
        return httpx2.Response(200, text="<table>" + "".join(f.row() for f in rows[:50]) + "</table>")

    def client(self) -> httpx2.Client:
        return build_client(Allowlist(set()), transport=httpx2.MockTransport(self.handler))


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "goulot.db")
    database.migrate()
    return database


FIPS = {"BRAZORIA": "48039", "CALHOUN": "48057", "CALCASIEU": "22019", "HARRIS": "48201", "NIAGARA": "36063"}


def plant(facility: str, city: str, county: str, state: str, parent: str) -> dict[str, Any]:
    return {"facility_name": facility, "city_name": city, "county_name": county, "state_abbr": state,
            "fac_closed_ind": "0", "parent_co_name": parent, "standardized_parent_company": parent,
            "state_county_fips_code": FIPS[county], "fac_latitude": 285700, "fac_longitude": 952100}


PLANTS = [
    # As the EPA really files some plants: a label of its own as the name, the operator in the mailing name.
    {**plant("FREEPORT_NALK BH", "FREEPORT", "BRAZORIA", "TX", "NALK CORP"), "mail_name": "BLUE HARBOR OPERATIONS LLC"},
    plant("NALK CORP - NIAGARA PLANT", "NIAGARA FALLS", "NIAGARA", "NY", "NALK CORP"),
    plant("VERDA NITRILES USA LLC           GREEN LAKE PLANT", "PORT LAVACA", "CALHOUN", "TX", "VERDA US I INC"),
    plant("VERDA NITRILES USA LLC", "LAKE CHARLES", "CALCASIEU", "LA", "VERDA US I INC"),
]


def past(ref: str, entity: str, owner: str, county: str, hours: list[float], first: int = 1000) -> list[Filed]:
    """Short declarations spread over the past years: the habit of an installation."""
    return [Filed(str(first + i), ref, entity, owner, county, "EMISSIONS EVENT",
                  NOW - timedelta(days=100 + 150 * i), NOW - timedelta(days=100 + 150 * i) + timedelta(hours=h))
            for i, h in enumerate(hours)]


# --- who operates a site -------------------------------------------------------------------------------------------
def test_the_operator_of_a_plant_is_its_subsidiary(db: Database) -> None:
    assert identity.legal_name("VERDA NITRILES USA LLC           GREEN LAKE PLANT") == "VERDA NITRILES USA LLC"
    assert identity.legal_name("NALK CORP - NIAGARA PLANT") == "NALK CORP"
    assert identity.legal_name("ALMA PERFORMANCE MATERIALS-CHOCOLATE BAYOU PLANT") == "ALMA PERFORMANCE MATERIALS"
    registry = Registry([], PLANTS)
    client = registry.client()
    found = identity.ensure(db, client, [NALK, VERDA, BAYOU, ZED], NOW, limit=9)
    assert found[("m1", "nalk")] == identity.Identity(("FREEPORT_NALK BH", "BLUE HARBOR OPERATIONS LLC"), "BRAZORIA",
                                                      "TX", "48039", 28.95, -95.35)
    assert found[("m1", "verda")].county == "CALHOUN" and found[("m1", "verda_la")].state == "LA"
    assert ("m1", "zed") not in found and registry.asked("data.epa.gov") == 3  # a company without a place: not asked
    assert "/CONTAINING/NALK/JSON" in str(registry.seen[0].url)  # one word of a public producer's name, no more
    nalk = identity.apply([NALK], found)[0]
    assert nalk.aliases == (("freeport", "nalk", "bh"), ("blue", "harbor", "operations"))
    # The regulator writes the subsidiary; the county stands for the city when the plant's name has neither.
    assert names_site("BLUE HARBOR OPERATIONS BRAZORIA texas", nalk, owner="BLUE HARBOR OPERATIONS LLC")
    assert not names_site("BLUE HARBOR OPERATIONS BRAZORIA texas", NALK, owner="BLUE HARBOR OPERATIONS LLC")
    assert not names_site("BLUE HARBOR OPERATIONS HARRIS texas", nalk, owner="BLUE HARBOR OPERATIONS LLC")
    # Kept on the Mac: the next cycle asks nothing; a renamed site is asked again.
    assert identity.ensure(db, client, [NALK, VERDA, BAYOU], NOW + timedelta(days=1)) == found
    assert registry.asked("data.epa.gov") == 3
    renamed = site("Nalk Chlor Alkali, Freeport", "Freeport, Texas", "nalk")
    identity.ensure(db, client, [renamed], NOW + timedelta(days=1))
    assert registry.asked("data.epa.gov") == 4
    # A plant whose group the EPA left blank is found by its own name, with a second question.
    registry.plants.append(plant("ORBE CHEMICALS LLC", "BAYTOWN", "HARRIS", "TX", "NA"))
    orbe = site("Orbe Chemicals, Baytown", "Baytown, Texas")
    assert identity.ensure(db, client, [orbe], NOW)[("m1", "orbe")].county == "HARRIS"
    assert registry.asked("data.epa.gov") == 6 and "facility_name/CONTAINING/ORBE" in str(registry.seen[-1].url)
    # A plant the EPA does not list is remembered as unknown, and an unreadable answer is asked again later.
    unknown = site("Luma Chemicals, Baytown", "Baytown, Texas")
    assert identity.ensure(db, client, [unknown], NOW)[("m1", "luma")] == identity.Identity()
    bad = build_client(Allowlist(set()), transport=httpx2.MockTransport(lambda r: httpx2.Response(503)))
    other = site("Sola Chemicals, Baytown", "Baytown, Texas")
    assert identity.ensure(db, bad, [other], NOW) == {}


# --- which installation is which site ------------------------------------------------------------------------------
def test_a_site_is_followed_found_or_outside_never_calm_by_default(db: Database) -> None:
    filed = [
        *past("RN100000001", "BLUE HARBOR OPERATIONS", "BLUE HARBOR OPERATIONS LLC", "BRAZORIA", [2, 1, 3, 2, 1]),
        # Same group, same state, another county: not the Freeport plant.
        *past("RN100000002", "BLUE HARBOR OPERATIONS HOUSTON", "BLUE HARBOR OPERATIONS LLC", "HARRIS", [2], 2000),
        *past("RN100000003", "ZED TEXAS OPERATIONS", "THE ZED CHEMICAL COMPANY", "ORANGE", [1, 1], 3000),
        *past("RN100000004", "WEST ZEDLAND CTB", "PERMIAN OPERATOR LTD", "ANDREWS", [1], 4000),
    ]
    registry = Registry(filed, PLANTS)
    client = registry.client()
    assert tceq.collect(db, client, SOURCE, FEED, NOW, [NALK, VERDA, BAYOU, ZED], budget=tceq.Budget(
        identities=9, sites=9, backfills=9)) == []
    with db.connect() as conn:
        status = {k[1]: r["status"] for k, r in records.coverage(conn, SOURCE).items()}
        binding = records.bound(conn, SOURCE, [NALK, VERDA, BAYOU, ZED])
        history = conn.execute("SELECT COUNT(*) FROM official_records WHERE ref = 'RN100000001'").fetchone()[0]
        read_back = conn.execute("SELECT history_from FROM registry_installations WHERE ref = 'RN100000001'").fetchone()
    # Followed by its number; searched and not found (never « calm »); in Louisiana, outside what Texas covers.
    assert status == {"nalk": "bound", "verda": "none", "verda_la": "outside", "zed": "bound"}
    assert {ref: [s.node_key for s in sites] for ref, sites in binding.items()} == {
        "RN100000001": ["nalk"], "RN100000003": ["zed"]}
    assert history == 5 and read_back[0] is not None
    assert registry.asked("cust_name=FREEPORT") == 1  # asked for each name of the operator, even the EPA's label
    county = [r for r in registry.seen if r.url.params.get("cust_name") == "BLUE"]
    assert county and all(r.url.params["ls_cnty_name"] == "BRAZORIA" for r in county)  # searched in its county
    # What the chain screen says of each site: its past, or plainly that it is not followed.
    with db.connect() as conn:
        shown = records.site_summaries(conn, "m1", NOW, {s.node_key: s.subject for s in (NALK, VERDA, BAYOU, ZED)})
        # A chain rebuilt with another site under the same key: nothing of the former site is shown or followed.
        other = site("Orbe Chemicals, Baytown", "Baytown, Texas", "nalk")
        assert records.site_summaries(conn, "m1", NOW, {"nalk": other.subject}) == {}
        assert "RN100000001" not in records.bound(conn, SOURCE, [other])
    assert shown["nalk"] == {"status": "bound", "text": (
        "Suivi au registre du Texas (TCEQ) par 1 installation : 5 incidents déclarés en 5 ans, durée médiane 2 h ; "
        "le plus long, 3 h, en 10/2025 ; le dernier le 05/08/2026.")}
    assert "son silence ne dit rien" in shown["verda"]["text"] and shown["verda_la"]["status"] == "outside"
    # Nothing is asked again at the next cycle, except the feed; « not found » is searched again after a month.
    before = len(registry.seen)
    tceq.collect(db, client, SOURCE, FEED, NOW + timedelta(hours=3), [NALK, VERDA, BAYOU, ZED])
    assert len(registry.seen) == before + 1
    registry.now = NOW + timedelta(days=31)
    tceq.collect(db, client, SOURCE, FEED, registry.now, [NALK, VERDA, BAYOU, ZED])
    assert registry.asked("cust_name=VERDA") == 2


# --- what departs from a site's habit ------------------------------------------------------------------------------
def habit() -> list[Filed]:
    return past("RN100000001", "BLUE HARBOR OPERATIONS", "BLUE HARBOR OPERATIONS LLC", "BRAZORIA", [2, 1, 3, 2, 1, 2])


def fresh(ext_id: str, hours: float | None, start: datetime | None = None, kind: str = "EMISSIONS EVENT",
          final: bool = True, cause: str = "A compressor tripped. Process gas was routed to the flare.") -> Filed:
    start = start or NOW - timedelta(days=2)
    end = None if hours is None else start + timedelta(hours=hours)
    return Filed(ext_id, "RN100000001", "BLUE HARBOR OPERATIONS", "BLUE HARBOR OPERATIONS LLC", "BRAZORIA", kind,
                 start, end, final, cause)


def run(db: Database, registry: Registry, now: datetime = NOW) -> list[Any]:
    registry.now = now
    return tceq.collect(db, registry.client(), SOURCE, FEED, now, [NALK], {"m1": ["acrylonitrile", "ACN"]},
                        tceq.Budget(identities=9, sites=9, backfills=9))


def test_routine_is_kept_and_only_what_departs_from_habit_is_sent(db: Database) -> None:
    monthly = [Filed(str(5000 + i), "RN100000001", "BLUE HARBOR OPERATIONS", "BLUE HARBOR OPERATIONS LLC", "BRAZORIA",
                     "MAINTENANCE", NOW - timedelta(days=31 * (i + 1)), NOW - timedelta(days=31 * i)) for i in (1, 2, 3)]
    registry = Registry([*habit(), *monthly], PLANTS)
    assert run(db, registry) == []  # the site is found and its past read: nothing new yet
    registry.filed.update({f.ext_id: f for f in (
        fresh("9001", 2),  # two hours of flaring, as five times before
        fresh("9003", 0.5, cause="Loss of electrical power to the site. All units tripped."),
        fresh("9004", 31 * 24, kind="MAINTENANCE", start=NOW - timedelta(days=1)),  # the monthly blanket notice
        fresh("9005", 96, kind="AIR SHUTDOWN", start=NOW - timedelta(days=3)),  # a planned stop of four days
    )})
    items = run(db, registry, NOW + timedelta(hours=3))
    by_number = {i.title.rsplit(" ", 1)[1]: i for i in items}
    assert sorted(by_number) == ["9003", "9005"]
    registry.filed["9002"] = fresh("9002", 30)  # more than a day, and the third incident of the week
    items = run(db, registry, NOW + timedelta(hours=6))
    assert len(items) == 1
    long_one = by_number["9002"] = items[0]
    assert "3 incidents déclarés par ce site en une semaine" in long_one.excerpt
    assert long_one.title == ("BLUE HARBOR OPERATIONS (BLUE HARBOR OPERATIONS LLC, Texas) : incident avec émissions, "
                              "incident 9002")
    assert "Signalé parce que : incident de 30 h ; 3 incidents" in long_one.excerpt
    assert "Habitude de ce site au registre : 8 incidents déclarés en 5 ans, durée médiane 2 h." in long_one.excerpt
    assert "La déclaration cite : acrylonitrile." in long_one.excerpt and "Régulateur prévenu le" in long_one.excerpt
    assert "alert(1)" not in long_one.excerpt and long_one.details["sites"] == ["Nalk Freeport"]
    assert "de celles qui arrêtent une usine" in by_number["9003"].excerpt
    assert "arrêt ou travaux programmés de 4 jours" in by_number["9005"].excerpt
    with db.connect() as conn:
        kept = dict(conn.execute("SELECT ext_id, emitted FROM official_records WHERE ext_id LIKE '900%'").fetchall())
    assert kept == {"9001": "", "9002": "first", "9003": "first", "9004": "", "9005": "first"}  # all kept, three sent
    # Read once: the next cycle asks the feed and no page.
    before = registry.asked("getDetails")
    assert run(db, registry, NOW + timedelta(hours=9)) == [] and registry.asked("getDetails") == before


def test_a_declaration_is_followed_until_its_final_report(db: Database) -> None:
    registry = Registry(habit(), PLANTS)
    run(db, registry)
    start = NOW - timedelta(hours=10)
    registry.filed["9100"] = fresh("9100", None, final=False, start=start, cause="Winter storm: site-wide outage.")
    first = run(db, registry, NOW + timedelta(hours=1))
    assert len(first) == 1 and "toujours en cours" in first[0].excerpt and "Premier rapport" in first[0].excerpt
    # Not read again within the day; then read daily while it has no final report.
    pages = registry.asked("target=9100")
    assert run(db, registry, NOW + timedelta(hours=4)) == [] and registry.asked("target=9100") == pages
    assert run(db, registry, NOW + timedelta(hours=30)) == [] and registry.asked("target=9100") == pages + 1
    # It left the 7-day feed long ago when its final report comes: 19 days in all.
    registry.filed["9100"] = fresh("9100", 463, final=True, start=start, cause="Winter storm: site-wide outage.")
    last = run(db, registry, NOW + timedelta(days=21))
    assert len(last) == 1 and last[0].title.startswith("Rapport final : ")
    assert "Rapport final : l'incident a duré 19 jours." in last[0].excerpt
    assert last[0].identity() != first[0].identity() and last[0].url == first[0].url  # a second article, same page
    pages = registry.asked("target=9100")
    assert run(db, registry, NOW + timedelta(days=23)) == [] and registry.asked("target=9100") == pages
    # A short event that goes on: routine at first, sent the day it passes 24 hours.
    registry.filed["9200"] = fresh("9200", None, final=False, start=NOW + timedelta(days=24))
    assert run(db, registry, NOW + timedelta(days=24, hours=5)) == []
    late = run(db, registry, NOW + timedelta(days=25, hours=6))
    assert len(late) == 1 and "incident de 30 h, toujours en cours" in late[0].excerpt


def test_an_installation_that_is_no_site_is_read_once(db: Database) -> None:
    other = Filed("7001", "RN100000009", "WEST TANK BATTERY", "ANOTHER OPERATOR LP", "ANDREWS", "EMISSIONS EVENT",
                  NOW - timedelta(days=1), NOW - timedelta(hours=20))
    registry = Registry([other], PLANTS, quiet=True)
    assert run(db, registry) == [] and registry.asked("target=7001") == 1
    registry.filed["7002"] = Filed("7002", "RN100000009", "WEST TANK BATTERY", "ANOTHER OPERATOR LP", "ANDREWS",
                                   "EMISSIONS EVENT", NOW - timedelta(hours=5), NOW - timedelta(hours=4))
    assert run(db, registry, NOW + timedelta(hours=3)) == [] and registry.asked("getDetails") == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM official_records").fetchone()[0] == 2  # both kept: the source lives
    # A site recognized by its name in a new declaration is bound from then on.
    registry.filed["7003"] = Filed("7003", "RN100000010", "VERDA GREEN LAKE PLANT", "VERDA NITRILES USA LLC",
                                   "CALHOUN", "EMISSIONS EVENT", NOW - timedelta(hours=40), NOW - timedelta(hours=2))
    registry.now = NOW + timedelta(hours=6)
    items = tceq.collect(db, registry.client(), SOURCE, FEED, registry.now, [NALK, VERDA])
    assert len(items) == 1 and items[0].details["sites"] == [VERDA.name]
    with db.connect() as conn:
        assert [s.node_key for s in records.bound(conn, SOURCE, [VERDA])["RN100000010"]] == ["verda"]
        assert records.coverage(conn, SOURCE)[("m1", "verda")]["status"] == "bound"


def test_a_silent_or_changed_source_is_an_error_not_a_calm_week(db: Database) -> None:
    registry = Registry(habit(), PLANTS, quiet=True)  # nothing in the last 7 days: Texas never has an empty week
    with pytest.raises(tceq.TceqError, match="source muette"):
        run(db, registry)
    changed = build_client(Allowlist(set()), transport=httpx2.MockTransport(lambda r: httpx2.Response(
        200, text='<rss version="2.0"><channel><item><title>New layout</title><link>https://www2.tceq.texas.gov/x'
                  "</link></item></channel></rss>")))
    with pytest.raises(tceq.TceqError, match="format changé"):
        tceq.feed(changed, SOURCE, FEED, NOW)
    # Pages that no longer carry an installation: the cycle fails loudly instead of keeping nothing.
    registry = Registry([fresh(str(n), 2) for n in (1, 2, 3)], PLANTS, quiet=True)
    for f in registry.filed.values():
        f.entity_label = "Site Name:"
    with pytest.raises(tceq.TceqError, match="fiches illisibles"):
        run(db, registry)


def test_judging_rules() -> None:
    serious = ("the power supply from the provider was interrupted", "facility-wide outage", "a fire in the unit",
               "due to hurricane conditions", "hard freeze", "lightning strike", "loss of power")
    routine = ("a compressor tripped", "fired heater upset", "fire water pump test", "the column flooded",
               "power supply card of an analyzer failed")
    assert all(records.SERIOUS.search(t) for t in serious), [t for t in serious if not records.SERIOUS.search(t)]
    assert not any(records.SERIOUS.search(t) for t in routine), [t for t in routine if records.SERIOUS.search(t)]
    d = Declaration(SOURCE, "1", unit="AN-7", cause="Absorber off-gas vented.", compounds="Acetonitrile 9 Acrylonitrile 0.27")
    assert records.names_material(d, ["acrylonitrile", "ACN", "nitrile"]) == ["acrylonitrile"]  # whole words only
    assert json.dumps(records.Profile().sentence())  # too little past: said, not guessed
    assert "trop peu" in records.Profile(years=5, count=2, median_hours=1).sentence()


def test_diagnostics_say_what_the_registry_knows_of_each_site(ctx: Any) -> None:
    """`goulot dev check-sources` and `records-history`: coverage, operator, habit and what would have been sent,
    worked out in a scratch base. Nothing is written in the app's own base."""
    from goulot.chain import store
    from goulot.cli import check_sources
    from goulot.llm.backend import ReplayBackend
    from tests import chain_fixtures as fx
    from tests.test_chain import run as reconstruct

    material_id, _, _ = reconstruct(ctx, ReplayBackend(responses=fx.replay_responses()))
    with ctx.db.transaction() as conn:
        store.validate_material(conn, material_id, acknowledge_injection=True)
        conn.execute("UPDATE chain_nodes SET name = 'Nalk Freeport', place = 'Freeport, Texas' "
                     "WHERE node_key = 'plant_gulf_cracker' AND valid_to IS NULL")
    registry = Registry([*habit(), fresh("9002", 30), fresh("9001", 2)], PLANTS)
    text = "\n".join(check_sources.registry_probe(ctx.settings, registry.client(), NOW))
    assert "1 sites producteurs" in text and "dont 1 aux États-Unis" in text
    assert "Texas (TCEQ) : 3 déclarations dans le flux des 7 derniers jours" in text and "pas de clé" in text
    # The archive of past weather alerts: a watch published three days before the long incident, a fog advisory.
    registry.archive = [
        {"phenomena": "HZ", "significance": "A", "eventid": 1, "wfo": "HGX", "issue": "2026-11-11T06:00:00Z",
         "name": "Hard Freeze Watch", "published": "2026-11-08T18:00:00Z"},
        {"phenomena": "FG", "significance": "Y", "eventid": 8, "wfo": "HGX", "issue": "2026-11-09T06:00:00Z",
         "name": "Dense Fog Advisory", "published": "2026-11-09T05:00:00Z"}]
    text = "\n".join(check_sources.records_history(ctx.settings, 12, registry.client(), "2026-11-01", "2026-11-13",
                                                    NOW))
    assert ("🌦 Hard Freeze Watch publiée le 08/11/2026 18:00 UTC (aucun arrêt de ce genre déclaré au registre) → "
            "3.0 jours avant son premier long incident déclaré") in text and "Dense Fog" not in text
    assert ("✅ Nalk Freeport : suivi par 1 installation(s) ; exploitant : FREEPORT_NALK BH, BLUE HARBOR OPERATIONS "
            "LLC") in text
    assert "RN100000001 BLUE HARBOR OPERATIONS (BLUE HARBOR OPERATIONS LLC) : 2 déclaration(s)" in text
    assert "🔔" in text and "incident de 30 h ; régulateur prévenu le" in text
    assert "→ 1 à signaler, 1 de routine (gardées, non envoyées)" in text
    with ctx.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM official_records").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM site_identities").fetchone()[0] == 0
