"""Sources of 22 November 2026: the Mississippi gauges (USGS) and the announcements of China's Ministry of Commerce."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx2

from goulot.collect import mofcom, sources
from goulot.collect.base import Item, load_sources
from goulot.collect.rules import Rule, _usable, match
from goulot.net.allowlist import Allowlist
from goulot.net.client import build_client
from goulot.settings import Settings

NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)


def client(routes: dict[str, tuple[int, bytes]]) -> httpx2.Client:
    def handler(request: httpx2.Request) -> httpx2.Response:
        status, body = routes.get(f"{request.url.host}{request.url.path}", (404, b""))
        return httpx2.Response(status, content=body)

    return build_client(Allowlist(set()), transport=httpx2.MockTransport(handler))


def gauge(site: str, readings: list[tuple[str, str]]) -> dict[str, object]:
    return {"sourceInfo": {"siteCode": [{"value": site}]},
            "values": [{"value": [{"dateTime": t, "value": v, "qualifiers": ["P"]} for t, v in readings]}]}


def test_mississippi_keeps_low_water_and_floods_only() -> None:
    data = {"value": {"timeSeries": [
        gauge("07010000", [("2026-11-21T12:00:00.000-06:00", "-1.2"), ("2026-11-22T12:00:00.000-06:00", "-3.4")]),
        gauge("07374000", [("2026-11-22T12:00:00.000-06:00", "8.00")]),  # normal: nothing
        gauge("99999999", [("2026-11-22T12:00:00.000-06:00", "-9")]),  # not one of ours
    ]}}
    items = sources.mississippi(client({"waterservices.usgs.gov/nwis/iv/": (200, json.dumps(data).encode())}))
    assert len(items) == 1
    low = items[0]
    assert low.magnitude == -3.4 and low.details["state"] == "critical" and low.details["waterway"] == "Mississippi"
    assert "Saint-Louis" in low.title and low.kind == "logistics"
    flood = {"value": {"timeSeries": [gauge("07374000", [("2026-11-22T12:00:00.000-06:00", "36.1")])]}}
    (high,) = sources.mississippi(client({"waterservices.usgs.gov/nwis/iv/": (200, json.dumps(flood).encode())}))
    assert high.details["state"] == "flood" and high.title.startswith("Crue")
    junk = {"value": {"timeSeries": [gauge("07010000", [("2026-11-22T12:00:00.000-06:00", "-999999")])]}}
    assert sources.mississippi(client({"waterservices.usgs.gov/nwis/iv/": (200, json.dumps(junk).encode())})) == []


HOME = """<html><body><aside><a href="/zwgk/zcfb/art/2026/art_{side}.html" title="侧栏">侧栏</a></aside>
<ul>
<li><a href="/zwgk/zcfb/art/2026/art_{a}.html" title="商务部 海关总署公告 关于对镓相关物项实施出口管制的公告">x</a></li>
<li><a title="关于对部分两用物项实施出口管制的公告" href="https://www.mofcom.gov.cn/zwgk/zcfb/art/2026/art_{b}.html">y</a></li>
<li><a href="/zwgk/zcfb/art/2026/art_{a}.html" title="again">again</a></li>
<li><a href="https://evil.example/zwgk/zcfb/art/2026/art_{c}.html" title="elsewhere">z</a></li>
<li><a href="/xwfb/rcxwfb/art/2026/art_{c}.html" title="news">news</a></li>
</ul><script>alert(1)</script></body></html>"""
A, B, C, SIDE = "a" * 32, "b" * 32, "c" * 32, "d" * 32
PAGE_A = ("<html><body><nav>首页 商务部</nav><div>发布时间：2026-11-20</div><div>为维护国家安全，对以下物项实施出口管制："
          "金属镓（海关商品编号8112923010）。自2026年12月1日起正式实施。</div><footer>2025-01-01</footer>"
          "</body></html>")
PAGE_B = "<html><body><div>2026年09月01日</div><div>两用物项出口管制，物项清单见附件。</div></body></html>"


def mofcom_routes() -> dict[str, tuple[int, bytes]]:
    home = HOME.format(a=A, b=B, c=C, side=SIDE).encode()
    return {"www.mofcom.gov.cn/": (200, home),
            f"www.mofcom.gov.cn/zwgk/zcfb/art/2026/art_{A}.html": (200, PAGE_A.encode()),
            f"www.mofcom.gov.cn/zwgk/zcfb/art/2026/art_{B}.html": (200, PAGE_B.encode())}


def test_mofcom_reads_announcements_with_their_text() -> None:
    items = mofcom.announcements(client(mofcom_routes()), ["gallium", "镓", "acrylonitrile"], NOW)
    # Only the ministry's own announcements, once each; the sidebar is a link too, its page answers 404: title only.
    urls = [it.url for it in items]
    assert f"https://www.mofcom.gov.cn/zwgk/zcfb/art/2026/art_{A}.html" in urls
    assert not any("evil.example" in u or "/xwfb/" in u for u in urls) and len(set(urls)) == len(urls)
    first = next(it for it in items if A in it.url)
    assert first.published_at == datetime(2026, 11, 20, tzinfo=UTC) and first.kind == "policy" and first.lang == "zh"
    assert first.details["names"] == ["镓"] and "8112923010" in first.details["hs_codes"]
    assert "首页" not in first.excerpt and "2025-01-01" not in first.excerpt and not first.queried
    # September's announcement still on the home page in November: long past, left out.
    assert not any(B in u for u in urls)


def rule(kind: str, value: str, material: str = "m-ga") -> Rule:
    return Rule(1, material, kind, value, None, None, None, None)


def test_a_chinese_announcement_meets_the_rules() -> None:
    (named, *_) = [it for it in mofcom.announcements(client(mofcom_routes()), ["镓"], NOW) if A in it.url]
    assert match(named, [rule("keyword", "镓")])  # the Chinese name of the material
    assert match(named, [rule("hs_code", "811292")])  # its customs code, in the text, with « 出口管制 »
    calm = Item(source="mofcom", kind="policy", url="https://www.mofcom.gov.cn/x", title="关于举办博览会的通知",
                excerpt="商品编号8112923010", queried=False)
    assert not match(calm, [rule("hs_code", "811292")])  # neither the material's name nor a policy action word
    assert _usable("镓") and _usable("甲醇") and not _usable("BP")


def test_new_sources_switched_on_with_the_users_go_ahead() -> None:
    found = load_sources()
    assert found["mofcom"]["enabled"] is True and found["usgs_water"]["enabled"] is True


def test_check_sources_probe_runs_both_readers(settings: Settings) -> None:
    from goulot.cli.check_sources import candidate_probe

    low = {"value": {"timeSeries": [gauge("07010000", [("2026-11-22T12:00:00.000-06:00", "-0.5")])]}}
    routes = {**mofcom_routes(), "waterservices.usgs.gov/nwis/iv/": (200, json.dumps(low).encode())}
    text = "\n".join(candidate_probe(settings, client(routes), NOW))
    assert "1 station(s)" in text and "Saint-Louis" in text
    assert "MOFCOM : 2 annonce(s)" in text and "nomme 镓" in text


def test_phone_numbers_are_not_customs_codes() -> None:
    text = "联系电话：86-10-65198919 传真 8610-65198172 税则号列29012990 及 2905.11.00"
    assert mofcom.customs_codes(text) == ["29012990", "29051100"]
