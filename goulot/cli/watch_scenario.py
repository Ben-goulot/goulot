"""The fixed watch scenario of `tests/fixtures/articles/watch_107-13-1.json` (development only).

`goulot dev record-watch` runs it once with real Batch calls and records the answers; the tests replay them.
The chain is the recorded one of the test CAS 107-13-1; the articles are served by a local mock transport, so the
scenario never touches the network except for the AI calls of the recording.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx2

from goulot.chain import service, store
from goulot.collect.runner import run_cycle
from goulot.db.connection import Database
from goulot.llm.backend import ReplayBackend
from goulot.llm.batch import BatchBackend
from goulot.llm.budget import Budget
from goulot.net.allowlist import Allowlist
from goulot.net.client import build_client
from goulot.pipeline import assess, grouping, triage
from goulot.pipeline.assess import Allowance
from goulot.settings import REPO_ROOT
from goulot.vault.keys import Vault

CAS = "107-13-1"
SCENARIO = REPO_ROOT / "tests" / "fixtures" / "articles" / "watch_107-13-1.json"
CHAIN = REPO_ROOT / "tests" / "fixtures" / "llm" / CAS
RECORDINGS = REPO_ROOT / "tests" / "fixtures" / "llm" / "watch_107-13-1"


def load() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(SCENARIO.read_text(encoding="utf-8"))
    return data


def feed_rss(articles: list[dict[str, Any]]) -> bytes:
    """Articles {url, title, summary, seendate (YYYYMMDDTHHMMSSZ)} as an RSS 2.0 feed, escaped."""
    from email.utils import format_datetime
    from xml.sax.saxutils import escape  # nosec B406 - escaping only, nothing is parsed

    from goulot.collect.base import parse_datetime

    items = "".join(
        f"<item><title>{escape(a['title'])}</title><link>{escape(a['url'])}</link>"
        f"<description>{escape(a['summary'])}</description>"
        f"<pubDate>{format_datetime(parse_datetime(a['seendate']))}</pubDate></item>"  # type: ignore[arg-type]
        for a in articles)
    return f"<?xml version='1.0'?><rss version='2.0'><channel><title>x</title>{items}</channel></rss>".encode()


def feed_routes(articles: list[dict[str, Any]]) -> dict[tuple[str, str], bytes]:
    """(host, path) -> feed: the articles on the first enabled media feed of config/sources.yaml, the others empty.
    Since 4 November 2026 a headline alone never enters the triage: the scenario's articles come with their summary,
    as a media feed publishes them."""
    from urllib.parse import urlsplit

    from goulot.collect.base import load_sources

    feeds = [urlsplit(str(s["url"])) for s in load_sources().values() if s.get("family") == "media" and s["enabled"]]
    empty = feed_rss([])
    return {(str(u.hostname), u.path): feed_rss(articles) if i == 0 else empty for i, u in enumerate(feeds)}


def http(scenario: dict[str, Any]) -> httpx2.Client:
    feeds = feed_routes(scenario["articles"])
    documents = [{k: v for k, v in d.items() if k not in ("id", "expect")} for d in scenario["federal_register"]]

    def handler(request: httpx2.Request) -> httpx2.Response:
        if (request.url.host, request.url.path) in feeds:
            return httpx2.Response(200, content=feeds[(request.url.host, request.url.path)])
        if request.url.host == "www.federalregister.gov":
            return httpx2.Response(200, json={"results": documents})
        return httpx2.Response(404)

    return build_client(Allowlist(set()), transport=httpx2.MockTransport(handler))


def wait_until_ended(backend: BatchBackend, db: Database, purpose: str, poll: Callable[[], None]) -> None:
    with db.connect() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM llm_batches WHERE purpose = ? AND status = 'submitted'",
                                          (purpose,))]
    for batch_id in ids:
        while not backend.ended(batch_id):
            poll()


def run(db: Database, budget: Budget, vault: Vault, backend: BatchBackend, *, budget_name: str = "dev",
        poll: Callable[[], None] = lambda: time.sleep(20)) -> dict[str, Any]:
    """Chain → collection → triage (Batch) → events → analyses (Batch) → verdict. Returns a summary."""
    scenario = load()
    now = datetime.fromisoformat(scenario["now"])
    identity = json.loads((CHAIN / "identity.json").read_text(encoding="utf-8"))
    with db.transaction() as conn:
        material_id = store.create_material(conn, identity)
    service.reconstruct(service.Deps(db, budget, ReplayBackend(CHAIN), None), material_id,
                        service.start_job(db, material_id), budget_name="dev")
    with db.transaction() as conn:
        store.validate_material(conn, material_id, acknowledge_injection=False)
    run_cycle(db, http(scenario), lambda name: None, now)
    triage.submit_pending(db, budget, backend, budget_name)
    wait_until_ended(backend, db, "triage", poll)
    triage.collect_pending(db, budget, backend, budget_name)
    with db.transaction() as conn:
        grouping.group_triaged(conn)
    for _ in range(2):  # the impacts, then the comprehension and hypotheses of the events that have one
        assess.run_analyses(db, budget, None, backend, now, Allowance(), budget_name)
        wait_until_ended(backend, db, "impact", poll)
    assess.collect(db, budget, backend, budget_name)
    assess.refresh_verdicts(db, vault, now, verified=True, last_ok_cycle=now.isoformat())
    return summary(db, scenario)


ANALYST_RECORDINGS = REPO_ROOT / "tests" / "fixtures" / "llm" / "analyst"
TEST_CAS = ("107-13-1", "554-13-2", "7440-55-3")


def analyst_material(db: Database, budget: Budget, vault: Vault, cas: str, backend: BatchBackend) -> datetime:
    """A test CAS ready for its analyst: the fixed watch scenario for 107-13-1 (replayed, already paid), the
    recorded chain alone for the two others (no event: a calm reading). Returns the scenario's date."""
    now = datetime.fromisoformat(load()["now"])
    if cas == CAS:
        run(db, budget, vault, backend)
        return now
    chain_dir = REPO_ROOT / "tests" / "fixtures" / "llm" / cas
    identity = json.loads((chain_dir / "identity.json").read_text(encoding="utf-8"))
    with db.transaction() as conn:
        material_id = store.create_material(conn, identity)
    service.reconstruct(service.Deps(db, budget, ReplayBackend(chain_dir), None), material_id,
                        service.start_job(db, material_id), budget_name="dev")
    with db.transaction() as conn:
        store.validate_material(conn, material_id, acknowledge_injection=False)
    assess.refresh_verdicts(db, vault, now, verified=True, last_ok_cycle=now.isoformat())
    return now


def summary(db: Database, scenario: dict[str, Any]) -> dict[str, Any]:
    by_url = {a["url"]: a for a in scenario["articles"]}
    with db.connect() as conn:
        articles = {}
        for r in conn.execute("SELECT a.url, a.status, e.event_id FROM articles a LEFT JOIN event_articles e ON "
                              "e.article_id = a.id"):
            key = by_url.get(r["url"], {}).get("id", r["url"])
            articles[key] = {"status": r["status"], "event": r["event_id"]}
        events = [dict(id=r["id"], type=r["event_type"], confirmation=r["confirmation"], lineages=r["lineages"],
                       origin=r["origin"], analysis=r["analysis_state"])
                  for r in conn.execute("SELECT * FROM events ORDER BY id")]
        verdict = conn.execute(
            "SELECT verdict, pressure, x_weeks, event_ids FROM verdicts WHERE current = 1").fetchone()
        cost = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE purpose IN ('triage', 'impact')"
                            ).fetchone()[0]
    return {"articles": articles, "events": events,
            "verdict": dict(verdict) if verdict else None, "ai_cost_usd": round(float(cost), 4)}


def recorded() -> bool:
    return (RECORDINGS / "summary.json").is_file()


def save_summary(data: dict[str, Any], path: Path = RECORDINGS / "summary.json") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
