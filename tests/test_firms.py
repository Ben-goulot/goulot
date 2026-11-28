"""NASA FIRMS (28 November 2026): neighbouring sites share a request, and a slow answer is asked once more."""

from __future__ import annotations

import httpx2
import pytest

from goulot.collect import sources
from goulot.net.allowlist import Allowlist
from goulot.net.client import build_client

CSV_HEAD = b"latitude,longitude,bright_ti4,acq_date,acq_time,confidence,frp\n"
# Two plants 30 km apart in one industrial region, the same plant in a second chain, and one far away.
SITES = [("plant_a", 29.70, -95.10), ("plant_b", 29.90, -94.90), ("plant_a_other_chain", 29.70, -95.10),
         ("plant_far", 51.95, 4.14)]


def client(answer, asked: list[httpx2.Request]) -> httpx2.Client:  # type: ignore[no-untyped-def]
    def handler(request: httpx2.Request) -> httpx2.Response:
        asked.append(request)
        return answer(request, len(asked))

    return build_client(Allowlist(set()), transport=httpx2.MockTransport(handler))


def test_neighbouring_sites_share_one_request() -> None:
    areas = sources.firms_areas(SITES)
    assert len(areas) == 2  # the region of the two plants, and the far one
    region = next(sites for _, sites in areas if len(sites) == 2)
    assert {k for k, _, _ in region} == {"plant_a", "plant_b"}  # the same position is asked once
    area = next(a for a, sites in areas if len(sites) == 2)
    assert area == "-95.200,29.600,-94.800,30.000"
    far = [(f"site{i}", 10.0 + i * 0.5, 20.0) for i in range(6)]  # 2.5° from end to end: two requests
    assert [len(s) for _, s in sources.firms_areas(far)] == [4, 2]


def test_a_fire_is_kept_near_a_site_only() -> None:
    rows = (b"29.71,-95.11,330,2026-11-27,0130,h,40\n"  # near plant_a
            b"29.89,-94.91,330,2026-11-27,0130,h,25\n"  # near plant_b
            b"29.85,-95.10,330,2026-11-27,0130,h,90\n")  # in the area of the request, near neither plant
    asked: list[httpx2.Request] = []
    fires = sources.firms(client(lambda r, n: httpx2.Response(200, content=CSV_HEAD + rows), asked), "k" * 32,
                          SITES[:3])
    assert sorted((f.details["near_node"], f.magnitude) for f in fires) == [("plant_a", 40.0), ("plant_b", 25.0)]
    assert len(asked) == 2  # one area, two satellites (four requests before 28 November 2026)


def test_a_slow_answer_is_asked_once_more(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources, "FIRMS_RETRY_PAUSE", 0.0)

    def first_slow(request: httpx2.Request, n: int) -> httpx2.Response:
        if n == 1:
            raise httpx2.ReadTimeout("slow", request=request)
        if n == 2:
            return httpx2.Response(503)
        return httpx2.Response(200, content=CSV_HEAD + b"51.96,4.15,330,2026-11-27,0130,h,55\n")

    asked: list[httpx2.Request] = []
    with pytest.raises(sources.SourceError):  # a slow answer, then a server error: given up, a failure
        sources.firms(client(first_slow, asked), "k" * 32, SITES[3:])
    asked.clear()
    fires = sources.firms(client(lambda r, n: first_slow(r, n + 2) if n > 1 else first_slow(r, 1), asked),
                          "k" * 32, SITES[3:])
    assert len(fires) == 1 and len(asked) == 3  # slow once, then both satellites answer

    def refused(request: httpx2.Request, n: int) -> httpx2.Response:
        return httpx2.Response(403)

    asked.clear()
    with pytest.raises(sources.SourceError):
        sources.firms(client(refused, asked), "k" * 32, SITES[3:])
    assert len(asked) == 1  # a refusal is not asked again
