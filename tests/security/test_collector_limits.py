"""Threat 20 (docs/SECURITY.md): a huge or looping feed cannot saturate the app."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx2
import pytest

from goulot.api.context import AppContext
from goulot.collect import runner
from goulot.net.allowlist import Allowlist
from goulot.net.client import TIMEOUT, build_client
from goulot.net.transport import MAX_RESPONSE_BYTES
from goulot.net.xml import UnsafeXML, parse_xml
from goulot.pipeline import triage
from tests.test_collect import NOW, validated_chain


def client(handler: object) -> httpx2.Client:
    return build_client(Allowlist(set()), transport=httpx2.MockTransport(handler))  # type: ignore[arg-type]


def test_collector_limits(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    validated_chain(ctx)
    # Timeouts and sizes of the single outbound client.
    assert TIMEOUT.connect == 10 and TIMEOUT.read == 30 and MAX_RESPONSE_BYTES == 10 * 1024 * 1024
    with pytest.raises(UnsafeXML):
        parse_xml(b"<rss>" + b"x" * (10 * 1024 * 1024) + b"</rss>")

    # A response larger than the limit is refused, recorded as a failure, and the other sources go on.
    huge = b"[" + b"0," * (6 * 1024 * 1024) + b"0]"

    def flood(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "api.gdeltproject.org":
            return httpx2.Response(200, content=huge)
        return httpx2.Response(404)

    result = runner.run_cycle(ctx.db, client(flood), lambda name: None, NOW)
    assert "gdelt_doc" in result.failed
    with ctx.db.connect() as conn:
        error = conn.execute("SELECT error FROM collect_runs WHERE source = 'gdelt_doc'").fetchone()[0]
    assert error == "ResponseTooLarge"

    # A feed listing thousands of matching items is cut at the bounded queue.
    many = {"articles": [{"url": f"https://www.reuters.com/a{i}", "title": f"Acrylonitrile plant fire {i}",
                          "seendate": "20261018T080000Z"} for i in range(1500)]}

    def spam(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "api.gdeltproject.org":
            return httpx2.Response(200, content=json.dumps(many).encode())
        return httpx2.Response(404)

    monkeypatch.setattr(runner, "MAX_ITEMS_PER_SOURCE", 300)
    later = NOW + timedelta(days=2)
    runner.run_cycle(ctx.db, client(spam), lambda name: None, later)
    with ctx.db.connect() as conn:
        stored = conn.execute("SELECT COUNT(*) FROM articles WHERE source = 'gdelt_doc'").fetchone()[0]
        queries = len(runner.queries(conn))
    assert 0 < stored <= 300 * queries  # never more than the cap per fetch
    # The triage batch is bounded too.
    assert triage.MAX_PER_BATCH == 200

    # Exponential backoff: after repeated failures a source is left alone for longer and longer, 24 h at most.
    t0 = NOW + timedelta(days=10)

    def fail_at(hours: float) -> None:
        with ctx.db.transaction() as conn:
            conn.execute("INSERT INTO collect_runs (cycle_id, source, family, started_at, ok, error) VALUES "
                         "('x', 'probe_source', 'hazard', ?, 0, 'HTTPError')",
                         ((t0 + timedelta(hours=hours)).isoformat(),))

    def waiting(hours: float) -> bool:
        with ctx.db.connect() as conn:
            return runner._backoff(conn, "probe_source", t0 + timedelta(hours=hours))

    fail_at(0)
    assert not waiting(0.5)  # one failure: retried at the next cycle
    fail_at(3)
    assert waiting(5) and not waiting(6.1)  # two failures: 3 h
    fail_at(6.1)
    assert waiting(11) and not waiting(12.2)  # three: 6 h
    for h in (12.2, 24.3, 48.4, 96.5):
        fail_at(h)
    assert waiting(96.5 + 23) and not waiting(96.5 + 24.1)  # capped at 24 h
    with ctx.db.transaction() as conn:
        conn.execute("INSERT INTO collect_runs (cycle_id, source, family, started_at, ok) VALUES "
                     "('x', 'probe_source', 'hazard', ?, 1)", ((t0 + timedelta(hours=130)).isoformat(),))
    assert not waiting(130.5)  # one success resets it
    # And a source in backoff is really skipped by the cycle.
    calls: list[str] = []

    def counting(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.url.host or "")
        return httpx2.Response(503)

    with ctx.db.connect() as conn:
        backed_off = runner._backoff(conn, "gdacs", later + timedelta(minutes=5))
    runner.run_cycle(ctx.db, client(counting), lambda name: None, later + timedelta(minutes=5))
    assert backed_off and "www.gdacs.org" not in calls
