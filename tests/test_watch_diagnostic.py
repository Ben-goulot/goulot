"""27 October 2026: GDELT pacing after a 429, pooled queries, and the watch diagnostic. No network."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from goulot.api.context import AppContext
from goulot.collect import report, sources
from goulot.collect.base import Item
from goulot.collect.runner import gdelt_queries, queries, run_cycle
from goulot.net.allowlist import Allowlist
from goulot.net.client import build_client
from tests.test_collect import GDELT_JSON, NOW, ROUTES, http, validated_chain

GDELT_HOST = ("api.gdeltproject.org", "/api/v2/doc/doc")


def gdelt_client(answers: list[httpx2.Response], seen: list[str]) -> httpx2.Client:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.params["query"])
        return answers.pop(0) if answers else httpx2.Response(200, json=GDELT_JSON)

    return build_client(Allowlist({"api.gdeltproject.org"}), transport=httpx2.MockTransport(handler))


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    waits: list[float] = []
    monkeypatch.setattr(sources.time, "sleep", waits.append)
    monkeypatch.setattr(sources, "GDELT_PAUSE_S", 6.0)
    monkeypatch.setattr(sources, "_gdelt_last", 0.0)
    return waits


def test_retry_after_read_as_seconds_or_date_and_bounded() -> None:
    now = datetime(2026, 10, 27, 12, tzinfo=UTC)
    assert sources.retry_after_seconds("7", now, 30, 120) == 7
    assert sources.retry_after_seconds("3600", now, 30, 120) == 120
    assert sources.retry_after_seconds("", now, 30, 120) == 30
    assert sources.retry_after_seconds("n'importe quoi", now, 30, 120) == 30
    assert sources.retry_after_seconds("Tue, 27 Oct 2026 12:00:45 GMT", now, 30, 120) == 45


def test_gdelt_429_waits_retry_after_retries_once_and_slows_down(slept: list[float]) -> None:
    seen: list[str] = []
    client = gdelt_client([httpx2.Response(429, headers={"Retry-After": "7"})], seen)
    items = sources.gdelt_many(client, ["a", "b"], NOW - timedelta(hours=3), NOW)
    assert seen == ["a", "a", "b"] and len(items) == 4
    assert 7 in slept
    assert slept[-1] > sources.GDELT_PAUSE_S  # after a 429, the next query waits longer


def test_second_429_ends_gdelt_for_the_cycle_keeping_what_was_read(slept: list[float]) -> None:
    seen: list[str] = []
    refused = [httpx2.Response(200, json=GDELT_JSON), httpx2.Response(429), httpx2.Response(429)]
    with pytest.raises(sources.PartialFailure) as caught:
        sources.gdelt_many(gdelt_client(refused, seen), ["a", "b", "c"], NOW - timedelta(hours=3), NOW)
    assert seen == ["a", "b", "b"]  # "c" is never asked
    assert len(caught.value.items) == 2 and str(caught.value) == "HTTP 429"
    assert sources.GDELT_RETRY_AFTER_DEFAULT_S in slept  # no Retry-After: the default wait


def test_timeout_keeps_items_read(slept: list[float]) -> None:
    calls = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        if len(calls) > 1:
            raise httpx2.ConnectTimeout("slow", request=request)
        return httpx2.Response(200, json=GDELT_JSON)

    client = build_client(Allowlist({"api.gdeltproject.org"}), transport=httpx2.MockTransport(handler))
    with pytest.raises(sources.PartialFailure) as caught:
        sources.gdelt_many(client, ["a", "b"], NOW - timedelta(hours=3), NOW)
    assert len(caught.value.items) == 2 and str(caught.value) == "ConnectTimeout"


def test_partial_gdelt_failure_stores_what_was_read(ctx: AppContext, slept: list[float]) -> None:
    validated_chain(ctx)
    sent = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal sent
        if (request.url.host, request.url.path) == GDELT_HOST:
            sent += 1
            return httpx2.Response(200, json=GDELT_JSON) if sent == 1 else httpx2.Response(429)
        status, body = ROUTES.get((request.url.host, request.url.path), (404, b"{}"))
        return httpx2.Response(status, content=body)

    result = run_cycle(ctx.db, build_client(Allowlist(set()), transport=httpx2.MockTransport(handler)),
                       lambda name: None, NOW)
    assert "gdelt_doc" in result.failed
    with ctx.db.connect() as conn:
        run = conn.execute("SELECT ok, error, fetched, matched, kept FROM collect_runs WHERE source = 'gdelt_doc'"
                           ).fetchone()
        stored = conn.execute("SELECT COUNT(*) FROM articles WHERE source = 'gdelt_doc'").fetchone()[0]
    assert run["ok"] == 0 and run["error"] == "SourceError: HTTP 429"
    assert run["fetched"] == 2 and run["matched"] == 1 and run["kept"] == 1 and stored == 1


def test_gdelt_queries_pool_materials_and_never_wrap_one_term(ctx: AppContext) -> None:
    validated_chain(ctx)
    with ctx.db.connect() as conn:
        pooled, per_material = gdelt_queries(conn), queries(conn)
    assert 0 < len(pooled) <= len(per_material)
    terms = {t for q, _ in per_material for t in q.split(" (")[0].strip("()").split(" OR ")}
    assert terms <= {t for q in pooled for t in q.split(" (")[0].strip("()").split(" OR ")}
    assert all(" OR " in q.split(" (")[0] or not q.startswith("(") for q in pooled)
    assert "(fictif)" not in " ".join(pooled) and '"Vapocraqueur de la côte du Golfe fictif"' in " ".join(pooled)


def test_matched_tells_duplicates_from_rejected(ctx: AppContext) -> None:
    validated_chain(ctx)
    run_cycle(ctx.db, http(), lambda name: None, NOW)
    run_cycle(ctx.db, http(), lambda name: None, NOW + timedelta(hours=3))
    with ctx.db.connect() as conn:
        first, second = conn.execute(
            "SELECT fetched, matched, kept FROM collect_runs WHERE source = 'gdelt_doc' ORDER BY id").fetchall()
    # Each GDELT query answers the same two articles: one passes the filter, the other is rejected.
    assert first["kept"] == 1 and first["fetched"] == 2 * first["matched"]
    assert second["matched"] == first["matched"] and second["kept"] == 0  # seen again: duplicates, not rejections


def test_report_history_per_source_and_material(ctx: AppContext) -> None:
    validated_chain(ctx)
    run_cycle(ctx.db, http(), lambda name: None, NOW)
    with ctx.db.connect() as conn:
        found = report.history(conn, NOW + timedelta(hours=1))
    assert found["has_matched"] and found["cycles"]["cycles"] == 1
    gdelt = next(s for s in found["sources"] if s["source"] == "gdelt_doc")
    assert gdelt["fetched"] == 2 * gdelt["matched"] and gdelt["kept"] == 1
    (material,) = found["materials"]
    assert material["status"] == "validated" and material["rules"]["keyword"] > 0
    assert sum(material["articles"].values()) >= 1
    text = report.render(found, None)
    assert "Par source" in text and "gdelt_doc" in text and material["name"] in text


def test_report_live_writes_nothing_and_cleans_titles(ctx: AppContext) -> None:
    validated_chain(ctx)
    run_cycle(ctx.db, http(), lambda name: None, NOW)
    trap = Item(source="gdelt_doc", kind="news", url="https://example.org/x", title="\x1b[31mRouge\x1b]0;titre\x07")
    known = Item(source="gdelt_doc", kind="news", url=GDELT_JSON["articles"][0]["url"],
                 title=GDELT_JSON["articles"][0]["title"])

    def fake(c: httpx2.Client, since: datetime) -> list[Item]:
        return [trap, known]

    with ctx.db.connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        (row,) = report.live(conn, http(), lambda name: None, NOW, fetchers={"gdelt_doc": fake})
        after = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    assert (row["fetched"], row["rejected"], row["duplicates"], row["new"]) == (2, 1, 1, 0)
    assert before == after
    assert all(ch.isprintable() for t in row["examples"] for ch in t)
    assert json.dumps(row)  # plain data, printable as is


def test_report_reads_a_base_from_before_migration_0013(ctx: AppContext) -> None:
    """The installed app may be one version behind the working copy: no `matched` column yet."""
    with ctx.db.transaction() as conn:
        conn.execute("ALTER TABLE collect_runs DROP COLUMN matched")
        conn.execute("INSERT INTO collect_runs (cycle_id, source, family, started_at, ok, error) "
                     "VALUES ('c', 'gdelt_doc', 'news', ?, 0, 'HTTP 429')", (NOW.isoformat(),))
        found = report.history(conn, NOW + timedelta(hours=1))
    assert not found["has_matched"] and found["sources"][0]["matched"] is None
    assert "?" in report.render(found, None)


def test_validated_material_without_rules_is_repaired_at_the_next_cycle(ctx: AppContext) -> None:
    """27 October 2026: materials validated on 17 October, before rules existed (18 October), were never watched."""
    validated_chain(ctx)
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE watch_rules SET active = 0")
        found = report.history(conn, NOW)
    assert found["materials"][0]["rules"] == {} and found["base"]["rules"]["active"] == 0
    assert found["materials"][0]["rule_history"]["inactive"] > 0 and "inactives" in report.render(found, None)
    result = run_cycle(ctx.db, http(), lambda name: None, NOW)
    assert result.kept >= 1
    with ctx.db.connect() as conn:
        assert report.history(conn, NOW)["materials"][0]["rules"]["keyword"] > 0
