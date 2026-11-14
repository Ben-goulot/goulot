"""Official records (pilot of 13 November 2026): recognizing a site of the chains in a regulator's, an emergency
service's or a stock exchange's own words, and the three readers. Fictitious answers, no network, no key."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx2
import pytest

from goulot.collect import sources
from goulot.collect.rules import Rule, match, title_only
from goulot.collect.sitematch import build_site, match_sites, names_site
from goulot.net.allowlist import Allowlist
from goulot.net.client import build_client
from goulot.pipeline.corroborate import domain_reliability

SINCE = datetime(2026, 11, 1, tzinfo=UTC)
NOW = datetime(2026, 11, 13, tzinfo=UTC)


def site(name: str, place: str = "unknown", iso: str = "US", key: str = "") -> Any:
    built = build_site("m1", key or name.split()[0].lower(), name, place, iso)
    assert built is not None, name
    return built


INEOS = site("INEOS Nitriles, Green Lake (Point Comfort/Port Lavaca)", "Port Lavaca, Texas")
ASCEND = site("Ascend Performance Materials, Chocolate Bayou", "Alvin, Texas")
NALK = site("Nalk Freeport", "Freeport, Texas")
LOTTE = site("Lotte Chemical, Yeosu", "Yeosu", "KR")
KZ = site("Korea Zinc", "unknown", "KR", "korea_zinc")
VITAL = site("Vital Materials", "unknown", "CN")
KOELN = site("INEOS Nitriles Köln", "Köln", "DE", "ineos_koln")


def test_a_regulator_names_a_plant_its_own_way() -> None:
    assert names_site("INEOS NITRILES USA LLC GREEN LAKE PLANT, CALHOUN COUNTY", INEOS)
    assert names_site("ASCEND PERFORMANCE MATERIALS CHOCOLATE BAYOU PLANT", ASCEND)
    assert names_site("NALK HARBOR OPERATIONS FREEPORT", NALK)
    # Same company, other site: the place must be named too.
    assert not names_site("INEOS USA LLC CHOCOLATE BAYOU PLANT", INEOS)
    assert not names_site("NALK CORPORATION BAYTOWN", NALK)
    assert not names_site("FREEPORT LNG LIQUEFACTION PLANT", NALK)
    assert match_sites("ASCEND PERFORMANCE MATERIALS CHOCOLATE BAYOU PLANT", [INEOS, ASCEND, NALK]) == [ASCEND]


def test_a_company_name_alone_needs_its_full_phrase() -> None:
    assert names_site("LOTTE CHEMICAL CORPORATION 롯데케미칼", LOTTE, place_implied=True)
    assert not names_site("LOTTE SHOPPING CO., LTD.", LOTTE, place_implied=True)  # a retailer, not the chemical maker
    assert names_site("KOREA ZINC COMPANY, LTD.", KZ, place_implied=True)
    assert names_site("Vital Materials Co. reports a furnace incident", VITAL)
    assert not names_site("A vital shipment of materials was delayed", VITAL)  # words apart: not the company
    assert not names_site("Nalk announces quarterly results", NALK, place_implied=True)  # four letters say too little
    assert build_site("m1", "x", "Usine", "unknown", "FR") is None  # nothing distinctive: never matched
    # Replay of February 2021 (14 November 2026): a three-letter company name was missed although its site had declared. A
    # record's own owner field names the company for sure, however short the name.
    zed = site("Zed")  # fictitious, like « Nalk »
    assert names_site("ZED TEXAS OPERATIONS FREEPORT texas", zed, owner="THE ZED CHEMICAL COMPANY")
    assert not names_site("WEST ZEDLAND CTB texas", zed, owner="PERMIAN OPERATOR LTD")
    assert not names_site("ZED TEXAS OPERATIONS FREEPORT texas", zed)  # in free text, three letters stay too little
    assert not names_site("BAYTOWN SITE texas", NALK, owner="NALK CORPORATION")  # the place is still required


def test_a_city_is_recognized_under_its_other_names() -> None:
    # The chain writes « Cologne », « Antwerp », « Yeosu »; a local source writes the city its own way. The embedded
    # city list gives the other names, offline.
    cologne = site("INEOS Nitriles", "Cologne, Rhénanie-du-Nord-Westphalie", "DE", "ineos_cologne")
    assert names_site("Brand bei INEOS Nitriles in Köln-Worringen", cologne)
    assert names_site("Incendie chez INEOS Nitriles à Cologne", cologne)
    assert not names_site("Brand bei INEOS Nitriles in Lima", cologne)
    assert not names_site("INEOS in Köln (Cologne)", cologne)  # two names of one city count once: « Nitriles » is missing
    antwerp = site("BASF Antwerp", "Antwerp", "BE")
    assert names_site("Brand bij BASF Antwerpen", antwerp) and names_site("Incendie chez BASF à Anvers", antwerp)
    assert not names_site("Brand bei BASF Ludwigshafen", antwerp)
    assert names_site("LOTTE CHEMICAL 여수공장 화재", LOTTE) and not names_site("LOTTE CHEMICAL 울산공장 화재", LOTTE)
    # Romanized spellings of other scripts, written in lower case by the list, are not names anybody writes.
    assert ("gu", "long") not in cologne.city_names and ("koln",) in cologne.city_names
    assert site("Vital Materials", "unknown", "CN").city_names == frozenset()


def http(routes: dict[str, Any], seen: list[httpx2.Request] | None = None) -> httpx2.Client:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        body = routes.get(request.url.path)
        if body is None:
            return httpx2.Response(404, text="")
        if isinstance(body, str):
            return httpx2.Response(200, text=body, headers={"content-type": "application/rss+xml"})
        return httpx2.Response(200, json=body)

    return build_client(Allowlist(set()), transport=httpx2.MockTransport(handler))


def rss(*entries: tuple[str, str, str]) -> str:
    items = "".join(
        f"<item><title>{t}</title><description>{d}</description><link>{link}</link>"
        "<pubDate>Thu, 12 Nov 2026 08:00:00 GMT</pubDate></item>" for t, d, link in entries)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{items}</channel></rss>'


DETAIL = "https://www2.tceq.texas.gov/oce/eer/index.cfm?fuseaction=main.getDetails&amp;target={n}"


def declaration(owner: str, entity: str, kind: str, county: str = "CALHOUN") -> str:
    """A fictitious TCEQ declaration page, with the labels of the real one (checked from the Mac, 14 November 2026)."""
    return (f"<html><body><table><tr><td>Incident Status:</td><td>OPEN</td><td>Report Type:</td><td>INITIAL</td>"
            f"<td>Report Date:</td><td>11/12/2026</td></tr><tr><td>Name of Owner or Operator:</td><td>{owner}</td>"
            f"<td>CN:</td><td>CN600000001</td></tr><tr><td>Regulated Entity Name:</td><td>{entity}</td><td>RN:</td>"
            f"<td>RN100000001</td></tr><tr><td>Physical Location:</td><td>1 MI S OF FM 1</td><td>County:</td>"
            f"<td>{county}</td></tr><tr><td>Event/Activity Type:</td><td>{kind}</td></tr><tr><td>Date and Time Event "
            "Discovered or Scheduled Activity Start:</td><td>11/11/2026 05:00 PM</td><td>Date and Time Event or "
            "Scheduled Activity Ended:</td><td>11/12/2026 07:00 AM</td><td>Event Duration:</td><td>14 hours, 0 minutes"
            "</td></tr><tr><th>Process Unit or Area Common Names</th></tr><tr><td>Acrylonitrile Unit 2</td></tr><tr>"
            "<th>Facility Common Name</th></tr><tr><td>Cause of Emission Event or Excess Opacity Event, or Reason for "
            "Scheduled Activity:</td><td>A reactor tripped after a power dip. <script>alert(1)</script>The unit was "
            "shut down.</td></tr><tr><td>Actions Taken, or Being Taken, to Minimize Emissions And/or Correct the "
            "Situation:</td><td>The unit is being restarted.</td></tr><tr><td>Basis Used to Determine Quantities"
            "</td></tr></table></body></html>")


def texas(pages: dict[str, str], seen: list[httpx2.Request] | None = None) -> httpx2.Client:
    sources._tceq_seen.clear()
    feed = rss(*[(f"{n} - RN100000001 X", "RN100000001 X", DETAIL.format(n=n)) for n in pages])

    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        if request.url.path.endswith("RSSDataFeed.xml"):
            return httpx2.Response(200, text=feed)
        page = pages.get(request.url.params.get("target", ""))
        return httpx2.Response(200, content=page.encode("latin-1")) if page else httpx2.Response(500, text="")

    return build_client(Allowlist(set()), transport=httpx2.MockTransport(handler))


def test_texas_declarations_are_read_on_their_own_page() -> None:
    """The feed names only the installation; its page gives the owner, the unit, the cause and the duration."""
    seen: list[httpx2.Request] = []
    client = texas({"900001": declaration("INEOS NITRILES USA LLC", "GREEN LAKE PLANT", "EMISSIONS EVENT"),
                    "900002": declaration("INEOS USA LLC", "CHOCOLATE BAYOU PLANT", "EMISSIONS EVENT", "BRAZORIA"),
                    "900003": declaration("NALK CORPORATION", "FREEPORT PLANT B", "MAINTENANCE", "BRAZORIA")}, seen)
    url = "https://www2.tceq.texas.gov/oce/eer/RSSDataFeed.xml"
    items = sources.tceq(client, "tceq_emissions", url, NOW, [INEOS, ASCEND, NALK])
    assert [i.details["sites"] for i in items] == [[INEOS.name], [NALK.name]]  # the olefins plant is another site
    first = items[0]
    assert first.title == "GREEN LAKE PLANT (INEOS NITRILES USA LLC, Texas) : événement d'émissions, incident 900001"
    assert "Unité : Acrylonitrile Unit 2." in first.excerpt and "14 hours, 0 minutes" in first.excerpt
    assert "A reactor tripped after a power dip." in first.excerpt and "alert(1)" not in first.excerpt
    assert "maintenance" in items[1].title and not title_only(first) and domain_reliability(first.domain) == 0.9
    assert len(seen) == 4  # the feed and three pages
    # A page is asked once: the next cycle reads the feed only.
    assert sources.tceq(client, "tceq_emissions", url, NOW, [INEOS, ASCEND, NALK]) == [] and len(seen) == 5


def test_registry_feed_keeps_only_the_sites_of_the_chains() -> None:
    feed = rss(
        ("Incident 400001", "INEOS NITRILES USA LLC GREEN LAKE PLANT - EMISSIONS EVENT - unit trip, flaring",
         "https://www2.tceq.texas.gov/oce/eer/index.cfm?fuseaction=main.getDetails&amp;target=400001"),
        ("Incident 400002", "PERMIAN GAS GATHERING STATION 12 - EMISSIONS EVENT",
         "https://www2.tceq.texas.gov/oce/eer/index.cfm?fuseaction=main.getDetails&amp;target=400002"),
        ("Incident 400003", "NALK CORPORATION BAYTOWN - MAINTENANCE", "https://www2.tceq.texas.gov/x?target=400003"),
    )
    items = sources.registry_feed(http({"/oce/eer/RSSDataFeed.xml": feed}), "tceq_emissions",
                                  "https://www2.tceq.texas.gov/oce/eer/RSSDataFeed.xml", "déclaration TCEQ", "en",
                                  SINCE, [INEOS, ASCEND, NALK])
    assert [i.title for i in items] == ["Incident 400001"]
    item = items[0]
    assert item.details["sites"] == [INEOS.name] and "unit trip" in item.excerpt and "site suivi" in item.excerpt
    assert not title_only(item) and item.queried and item.domain == "www2.tceq.texas.gov"
    # The chain's own name, carried in the details, is what the rule filter recognizes.
    rule = Rule(1, "m1", "keyword", INEOS.name, "ineos", None, None, None)
    assert match(item, [rule]) == [rule]
    assert domain_reliability(item.domain) == 0.9


def test_german_city_feeds_follow_the_chains() -> None:
    seen: list[httpx2.Request] = []
    feed = rss(("FW-K: Brand in einer Produktionsanlage bei INEOS Nitriles Köln", "Die Feuerwehr war mit 40 Kräften "
                "im Einsatz, die Anlage wurde abgefahren.", "https://www.presseportal.de/blaulicht/pm/1/2"),
               ("POL-K: Verkehrsunfall in Köln-Deutz", "Zwei Verletzte.", "https://www.presseportal.de/blaulicht/pm/1/3"))
    cities = sources.german_cities([KOELN, INEOS, LOTTE], {"ineos_koln": "Köln", "ineos": "Port Lavaca, Texas"})
    assert list(cities) == ["Köln"]
    # The chain may name the city in French: the embedded city list gives the name the feed is filed under.
    assert list(sources.german_cities([KOELN], {"ineos_koln": "Cologne"})) == ["Köln"]
    client = http({"/rss/polizei/r/Köln.rss2": feed}, seen)
    items = sources.presseportal(client, "presseportal_blaulicht", SINCE, [KOELN], cities)
    assert len(items) == 1 and "INEOS" in items[0].title and items[0].lang == "de"
    assert str(seen[0].url) == "https://www.presseportal.de/rss/polizei/r/K%C3%B6ln.rss2"
    assert domain_reliability("presseportal.de", "/blaulicht/pm/1/2") == 0.9  # the authorities' section
    assert domain_reliability("presseportal.de", "/pm/183222/1") == 0.3  # a company's press release
    # A city Presseportal does not know is skipped, not an error.
    assert sources.presseportal(http({}), "presseportal_blaulicht", SINCE, [KOELN], cities) == []


def test_korean_disclosures_of_a_stop() -> None:
    seen: list[httpx2.Request] = []
    listing = {"status": "000", "total_page": 1, "list": [
        {"corp_code": "00111111", "corp_name": "롯데케미칼", "report_nm": "생산중단", "rcept_no": "20261112800001",
         "rcept_dt": "20261112"},
        {"corp_code": "00222222", "corp_name": "롯데쇼핑", "report_nm": "생산중단", "rcept_no": "20261112800002",
         "rcept_dt": "20261112"},
        {"corp_code": "00111111", "corp_name": "롯데케미칼", "report_nm": "주주총회소집결의", "rcept_no": "20261112800003",
         "rcept_dt": "20261112"},
    ]}
    companies = {"00111111": "LOTTE CHEMICAL CORPORATION", "00222222": "LOTTE SHOPPING CO., LTD."}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.path == "/api/list.json":
            return httpx2.Response(200, json=listing)
        return httpx2.Response(200, json={"status": "000",
                                          "corp_name_eng": companies[request.url.params["corp_code"]]})

    client = build_client(Allowlist(set()), transport=httpx2.MockTransport(handler))
    items = sources.opendart(client, "k" * 40, SINCE, NOW, [LOTTE, INEOS])
    assert len(items) == 1
    item = items[0]
    assert item.title == "LOTTE CHEMICAL CORPORATION : arrêt de production (annonce à la Bourse de Corée)"
    assert item.url == "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261112800001"
    assert item.details["sites"] == [LOTTE.name] and domain_reliability(item.domain) == 0.9
    assert item.published_at is not None and not title_only(item)
    assert seen[0].url.params["pblntf_ty"] == "I" and seen[0].url.params["bgn_de"] == "20261101"
    assert len([r for r in seen if r.url.path == "/api/company.json"]) == 2  # only for the two disclosures of a stop
    # No Korean site in the chains: nothing is asked at all.
    assert sources.opendart(client, "k" * 40, SINCE, NOW, [INEOS]) == []
    # A refused key is an error of the source, shown in the diagnostic.
    bad = build_client(Allowlist(set()), transport=httpx2.MockTransport(
        lambda r: httpx2.Response(200, json={"status": "010", "message": "등록되지 않은 키입니다."})))
    with pytest.raises(sources.SourceError):
        sources.opendart(bad, "k" * 40, SINCE, NOW, [LOTTE])


def test_pilot_sources_are_wired_and_korea_waits_for_its_key() -> None:
    from goulot.collect.base import load_sources
    from goulot.security.secrets import SECRET_NAMES

    cfg = load_sources()
    # Texas and Germany were checked from the Mac and switched on (14 November 2026); Korea was never tried.
    for sid, enabled in (("tceq_emissions", True), ("presseportal_blaulicht", True), ("opendart", False)):
        assert cfg[sid]["family"] == "registry" and cfg[sid]["enabled"] is enabled, sid
    assert "opendart" in SECRET_NAMES


def test_probe_counts_what_the_records_would_bring(ctx: Any) -> None:
    """`goulot dev check-sources`: entries read and entries naming a site, without showing the chains themselves."""
    from goulot.chain import store
    from goulot.cli import check_sources
    from goulot.llm.backend import ReplayBackend
    from tests import chain_fixtures as fx
    from tests.test_chain import run

    material_id, _, _ = run(ctx, ReplayBackend(responses=fx.replay_responses()))
    with ctx.db.transaction() as conn:
        store.validate_material(conn, material_id, acknowledge_injection=True)
        conn.execute("UPDATE chain_nodes SET name = 'Usine fictive Exemple, Bayport', place = 'Bayport, Texas' "
                     "WHERE node_key = 'plant_gulf_cracker' AND valid_to IS NULL")
    lines = check_sources.registry_probe(ctx.settings, texas(
        {"900001": declaration("USINE FICTIVE EXEMPLE LLC", "BAYPORT PLANT", "AIR SHUTDOWN"),
         "900002": declaration("ANOTHER OPERATOR LP", "WEST TANK BATTERY", "EMISSIONS EVENT")}))
    text = "\n".join(lines)
    assert "1 sites producteurs" in text and "dont 1 aux États-Unis" in text
    assert "Texas (TCEQ) : 2 entrées lues, 1 nomme(nt) un site" in text and "BAYPORT PLANT" in text
    assert "WEST TANK BATTERY" not in text and "pas de clé" in text
