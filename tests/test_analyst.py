"""Lot 10: the analyst of each material. Whitelisted dossier, triggers, its own cap inside the watch budget, checks
of a real output, the Fiche matière and the Monday digest. Recorded answers only: no network, no keychain."""

from __future__ import annotations

import copy
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from goulot.api.context import AppContext
from goulot.llm.batch import ReplayBatchBackend
from goulot.llm.budget import CRISIS, ECONOMY, NORMAL, RULES_ONLY, CallRecord
from goulot.llm.pricing import Usage
from goulot.pipeline import analyst
from goulot.settings import REPO_ROOT
from tests.conftest import login
from tests.test_alerts import MONDAY_MORNING, SUNDAY_EVENING
from tests.test_collect import NOW
from tests.test_screens_api import prepared

EXAMPLE: dict[str, Any] = json.loads(
    (REPO_ROOT / "schemas" / "examples" / "analyst_v1.example.json").read_text(encoding="utf-8"))


def message(result: dict[str, Any]) -> dict[str, Any]:
    return {"id": "msg_a", "type": "message", "role": "assistant", "model": analyst.MODEL, "stop_reason": "end_turn",
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "usage": {"input_tokens": 9_000, "output_tokens": 3_500}}


def reading(refs: dict[str, str], **changes: Any) -> dict[str, Any]:
    """The schema example, cut to the references actually sent."""
    note = copy.deepcopy(EXAMPLE)
    first = next((r for r in refs if r.startswith("E")), None)
    for part in ("changes", "counter_evidence", "watch"):
        for item in note[part]:
            item["event_refs"] = [first] if first and item["event_refs"] else []
    note["main_signal"]["event_refs"] = [first] if first else []
    note["set_aside"] = [{"event_ref": first, "why": "Exemple."}] if first else []
    note["watch"][0]["date"] = "2026-11"
    note.update(changes)
    return note


def send(ctx: AppContext, now: Any = NOW, mode: str = NORMAL) -> tuple[ReplayBatchBackend, list[tuple[str, Any]]]:
    backend = ReplayBatchBackend()
    assert analyst.submit(ctx.db, ctx.budget, backend, now, mode) >= 1
    (requests,) = backend.batches.values()
    return backend, requests


def answer(ctx: AppContext, backend: ReplayBatchBackend, requests: list[tuple[str, Any]],
           make: Any = None) -> list[str]:
    with ctx.db.connect() as conn:
        rows = {r["custom_id"]: json.loads(r["dossier"])["refs"] for r in conn.execute("SELECT * FROM analyst_notes")}
    backend.responses = {cid: message((make or reading)(rows[cid])) for cid, _ in requests}
    analyst.collect(ctx.db, ctx.budget, backend)
    with ctx.db.connect() as conn:
        return [r[0] for r in conn.execute("SELECT state FROM analyst_notes ORDER BY id")]


def test_dossier_is_public_and_whitelisted(ctx: AppContext) -> None:
    material_id, event_id = prepared(ctx)
    with ctx.db.transaction() as conn:
        conn.execute("INSERT INTO settings VALUES ('delivery_zone', 'asia', 'now')")
    _, requests = send(ctx)
    ((cid, params),) = requests
    text = params["messages"][0]["content"]
    assert cid.startswith("anl_") and params["model"] == "claude-sonnet-5-5"
    assert params["thinking"] == {"type": "adaptive"} and params["output_config"]["effort"] == "medium"
    assert params["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"} and "tools" not in params
    for section in ("<request>", "<verdict>", "<events>", "<set_aside>", "<prices>", "<chain>", "<hotspots>",
                    "<upcoming>", "<previous>"):
        assert section in text
    assert '"ref": "E1"' in text and event_id not in text  # events are cited by reference only
    assert "http" not in text and "asia" not in text and "37.2917" not in text
    with ctx.db.connect() as conn:
        row = conn.execute("SELECT * FROM analyst_notes").fetchone()
    assert row["trigger"] == "first" and row["state"] == "pending" and row["material_id"] == material_id
    assert json.loads(row["dossier"])["refs"]["E1"] == event_id


def test_reading_shown_on_the_sheet_with_its_doubt(client: TestClient, ctx: AppContext) -> None:
    material_id, event_id = prepared(ctx)
    backend, requests = send(ctx)
    doubt = {"doubts": True, "reason": "Le canal reste ouvert d'après la source officielle."}
    assert answer(ctx, backend, requests, lambda refs: reading(refs, verdict_doubt=doubt)) == ["done"]
    login(client, ctx)
    sheet = client.get(f"/api/materials/{material_id}/sheet").json()
    r = sheet["analyst"]
    assert r["note"]["verdict_doubt"] == doubt and r["trigger"] == "first" and not r["pending"]
    assert r["note"]["main_signal"]["event_ids"] == [event_id] and event_id in r["titles"]
    assert sheet["verdict"]["verdict"] == "pot"  # the doubt never changes the verdict
    with ctx.db.connect() as conn:
        cost = conn.execute("SELECT cost_usd FROM llm_calls WHERE purpose = 'analyst'").fetchone()[0]
    assert 0 < cost < 0.04  # 9,000 tokens in, 3,500 out, at the Batch rate


@pytest.mark.parametrize(("change", "problem"), [
    ({"main_signal": {"text": "x", "event_refs": ["E9"], "novelty": "new", "origin": "unknown"}}, "unknown events"),
    ({"situation": "Tout est expliqué sur https://evil.example/page."}, "url"),
    ({"headline": "Voir evil-news.example pour le détail"}, "url"),
    ({"injection_suspected": True}, "injection"),
    ({"verdict_doubt": {"doubts": True, "reason": ""}}, "verdict_doubt"),
    ({"situation": " ".join(["mot"] * 120)}, "length"),
])
def test_rejected_readings_are_never_shown(client: TestClient, ctx: AppContext, change: dict[str, Any],
                                           problem: str) -> None:
    material_id, _ = prepared(ctx)
    backend, requests = send(ctx)
    assert answer(ctx, backend, requests, lambda refs: reading(refs, **change)) == ["rejected"]
    with ctx.db.connect() as conn:
        row = conn.execute("SELECT problems, result FROM analyst_notes").fetchone()
    assert problem in row["problems"] and row["result"] is None
    login(client, ctx)
    assert client.get(f"/api/materials/{material_id}/sheet").json()["analyst"] is None


@pytest.mark.parametrize(("date", "ok"), [("2026-10", True), ("2026-10-18", True), ("2026-09", False),
                                          ("2026-10-17", False), ("2028-04", True), ("2028-06", False),
                                          ("dans deux semaines", False), ("unknown", True)])
def test_watch_dates_are_bounded(date: str, ok: bool) -> None:
    note = reading({"E1": "evt"})
    note["watch"][0]["date"] = date
    problems = analyst.check_note(note, {"E1"}, set(), "2026-10-18")
    assert (not problems) is ok, problems


def test_publishers_sent_may_be_named() -> None:
    note = reading({"E1": "evt"}, situation="L'arrêt est confirmé par example-wire.com.")
    assert analyst.check_note(note, {"E1"}, {"example-wire.com"}, "2026-10-18") == []
    assert analyst.check_note(note, {"E1"}, set(), "2026-10-18") == ["url"]


def test_enum_casing_is_normalized(ctx: AppContext) -> None:
    prepared(ctx)
    backend, requests = send(ctx)

    def shouting(refs: dict[str, str]) -> dict[str, Any]:
        note = reading(refs)
        note["main_signal"]["novelty"], note["confidence"]["level"] = "New", "MEDIUM"
        return note

    assert answer(ctx, backend, requests, shouting) == ["done"]


def test_triggers(ctx: AppContext) -> None:
    material_id, _ = prepared(ctx)
    tuesday = NOW + timedelta(days=2)  # away from the weekly window, Sunday evening and Monday
    with ctx.db.connect() as conn:
        material = {"id": material_id}
        assert analyst.due(conn, material, tuesday, NORMAL) == "first"
        assert analyst.due(conn, material, tuesday, ECONOMY) is None  # no first reading in economy mode
        assert analyst.due(conn, material, tuesday, CRISIS) is None
        assert analyst.due(conn, material, tuesday, RULES_ONLY) is None
    backend, requests = send(ctx, tuesday)
    with ctx.db.connect() as conn:
        assert analyst.due(conn, material, tuesday + timedelta(hours=1), NORMAL) is None  # one pending at a time
    answer(ctx, backend, requests)
    with ctx.db.connect() as conn:
        assert analyst.due(conn, material, tuesday + timedelta(hours=1), NORMAL) is None  # nothing changed
        assert analyst.due(conn, material, SUNDAY_EVENING + timedelta(days=7), NORMAL) == "weekly"
        # A new market verdict: a new reading, 24 h after the last one at least.
        conn.execute("UPDATE verdicts SET id = 'moved' WHERE current = 1 AND scope = 'market'")
        assert analyst.due(conn, material, tuesday + timedelta(hours=2), NORMAL) is None
        assert analyst.due(conn, material, tuesday + timedelta(hours=25), NORMAL) == "verdict"
        assert analyst.due(conn, material, tuesday + timedelta(days=3), ECONOMY) is None  # economy: once a week
        assert analyst.due(conn, material, tuesday + timedelta(days=8), ECONOMY) == "verdict"
        # A serious event the last reading had not seen: 48 h apart.
        conn.execute("UPDATE verdicts SET id = (SELECT verdict_id FROM analyst_notes) WHERE id = 'moved'")
        conn.execute("UPDATE analyst_notes SET serious = '[]'")
        assert analyst.due(conn, material, tuesday + timedelta(hours=30), NORMAL) is None
        assert analyst.due(conn, material, tuesday + timedelta(hours=49), NORMAL) == "event"


def test_weekly_reading_feeds_the_monday_digest(ctx: AppContext) -> None:
    from goulot.alerts import digest

    prepared(ctx)
    backend, requests = send(ctx, SUNDAY_EVENING)
    with ctx.db.connect() as conn:
        assert conn.execute("SELECT trigger, week FROM analyst_notes").fetchone()[1] == "2026-W42"
    assert digest.submit(ctx.db, SUNDAY_EVENING) == 1
    assert digest.compose(ctx.db, MONDAY_MORNING) is None  # waits for the analyst's Batch
    doubt = {"doubts": True, "reason": "Voir evil.example : le canal reste ouvert."}
    answer(ctx, backend, requests, lambda refs: reading(refs, verdict_doubt=doubt))
    with ctx.db.connect() as conn:  # a domain in a reading is rejected; the digest falls back to the code
        assert conn.execute("SELECT state FROM analyst_notes").fetchone()[0] == "rejected"
    _, text = digest.compose(ctx.db, MONDAY_MORNING)  # type: ignore[misc]
    assert "code seul" in text and "evil" not in text


def test_own_cap_inside_the_watch_budget(ctx: AppContext) -> None:
    prepared(ctx)
    assert analyst.monthly_cap() == 1.5 and ctx.budget.cap("watch") == 6.5
    ctx.budget.record(CallRecord("watch", "analyst", analyst.MODEL, "batch", Usage(), 1.48))
    assert analyst.submit(ctx.db, ctx.budget, ReplayBatchBackend(), NOW, NORMAL) == 0  # left for next month
    assert analyst.submit(ctx.db, ctx.budget, ReplayBatchBackend(), NOW + timedelta(days=20), NORMAL) == 1


def test_failed_batch_is_retried_later(ctx: AppContext) -> None:
    material_id, _ = prepared(ctx)
    backend, _ = send(ctx)
    analyst.collect(ctx.db, ctx.budget, backend)  # no recorded answer: an errored request
    with ctx.db.connect() as conn:
        assert conn.execute("SELECT state FROM analyst_notes").fetchone()[0] == "error"
        assert analyst.due(conn, {"id": material_id}, NOW + timedelta(hours=1), NORMAL) == "first"


def test_recorded_readings_replay(tmp_path: Path) -> None:
    """`goulot dev record-analyst`: the real readings of the 3 test CAS pass every check again, and the dossier
    rebuilt today from the same recordings cites the same events."""
    from datetime import datetime

    from goulot.api.context import build_context
    from goulot.cli import watch_scenario
    from goulot.security.clock import ManualClock
    from goulot.settings import Settings

    out = watch_scenario.ANALYST_RECORDINGS
    if not (out / "summary.json").is_file():
        assert not any(out.glob("*.json"))  # nothing half-recorded
        return
    for cas in watch_scenario.TEST_CAS:
        data = json.loads((out / f"{cas}.json").read_text(encoding="utf-8"))
        sent = data["dossier"]
        note = analyst.parse(data["response"])
        assert note is not None and data["state"] == "done", cas
        assert analyst.check_note(note, set(sent["refs"]), set(sent["publishers"]), sent["today"]) == [], cas
        assert data["cost_usd"] < 0.06, cas  # the worst case reserved for one reading
        at = datetime.fromisoformat(data["now"])
        ctx = build_context(Settings(home=tmp_path / cas, log_dir_override=tmp_path / f"{cas}-l"),
                            ManualClock(at.timestamp()))
        now = watch_scenario.analyst_material(ctx.db, ctx.budget, ctx.vault, cas,
                                              ReplayBatchBackend(watch_scenario.RECORDINGS))
        with ctx.db.connect() as conn:
            material = conn.execute("SELECT * FROM materials").fetchone()
            dossier = analyst.build_dossier(conn, dict(material), now)
        assert dossier.refs == sent["refs"], cas
