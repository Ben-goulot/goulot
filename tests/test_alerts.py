"""Phase 3, lot 7: alert wording, grouping, discreet mode, health and security alerts, weekly digest."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from goulot.alerts import digest, service
from goulot.api.context import AppContext
from goulot.db.connection import set_setting
from goulot.ledger import chain
from goulot.llm.batch import ReplayBatchBackend
from goulot.pipeline import assess
from goulot.security.audit import audit
from goulot.settings import REPO_ROOT
from tests.test_collect import NOW
from tests.test_screens_api import prepared

SUNDAY_EVENING = datetime(2026, 10, 18, 20, tzinfo=UTC)  # after 18:00 local time in Europe and in UTC
MONDAY_MORNING = datetime(2026, 10, 19, 10, tzinfo=UTC)  # after 08:00 local time
EXAMPLE: dict[str, Any] = json.loads(
    (REPO_ROOT / "schemas" / "examples" / "digest_v1.example.json").read_text(encoding="utf-8"))


def message(result: dict[str, Any]) -> dict[str, Any]:
    return {"id": "msg_d", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "usage": {"input_tokens": 3_000, "output_tokens": 500}}


# --- verdict alerts and dispatch --------------------------------------------------------------------------
def test_verdict_alert_wording_and_break_in(ctx: AppContext) -> None:
    prepared(ctx)
    with ctx.db.transaction() as conn:
        mid = conn.execute("SELECT material_id FROM verdicts WHERE current = 1").fetchone()[0]
        rows = conn.execute("SELECT * FROM verdicts WHERE current = 1").fetchone()
    from goulot.pipeline.verdict import Verdict

    v = Verdict(verdict="pot", pressure=1, x_weeks=6.1, x_low=5.0, x_high=7.0, window_weeks=2.1, window_assumed=True)
    with ctx.db.connect() as conn:
        (alert,) = service.verdict_alerts(conn, [(mid, v)], NOW)
    assert alert.text.startswith("Goulot · acrylonitrile : Problème potentiel à 6 semaines.")
    assert "Fenêtre d'action : 2 semaines (délai fournisseur supposé)." in alert.text
    assert alert.text.endswith("(rodage)") and rows["break_in"] == 1
    assert "http" not in alert.text and not alert.urgent
    first_calm = Verdict(verdict="ras", pressure=0)
    with ctx.db.transaction() as conn:
        conn.execute("DELETE FROM verdicts WHERE current = 0")
        assert service.verdict_alerts(conn, [(mid, first_calm)], NOW) == []  # a first « Rien à signaler »


def test_dispatch_dedupes_groups_and_respects_discreet_mode(ctx: AppContext) -> None:
    sent: list[str] = []
    a = service.Alert("verdict", "k1", "Goulot · acrylonitrile : Problème potentiel.", "m1")
    b = service.Alert("verdict", "k2", "Goulot · acrylonitrile : autre changement.", "m1")
    urgent = service.Alert("verdict", "k3", "Goulot · acrylonitrile : Anticiper stock.", "m1", urgent=True)
    assert service.dispatch(ctx.db, [a, a], sent.append, NOW) == 1  # once per key
    assert service.dispatch(ctx.db, [b], sent.append, NOW + timedelta(minutes=20)) == 0  # one per hour
    assert service.dispatch(ctx.db, [urgent], sent.append, NOW + timedelta(minutes=30)) == 1  # except « Anticiper »
    with ctx.db.transaction() as conn:
        set_setting(conn, "alerts_discreet", "1")
    service.dispatch(ctx.db, [service.Alert("verdict", "k4", "Goulot · acrylonitrile : X", "m2")], sent.append,
                     NOW + timedelta(hours=2))
    assert sent[-1] == "Goulot : un verdict a changé, ouvre l'app." and "acrylonitrile" not in sent[-1]

    def broken(text: str) -> None:
        raise OSError("network down")

    assert service.dispatch(ctx.db, [service.Alert("test", "k5", "x")], broken, NOW) == 0
    with ctx.db.connect() as conn:
        row = conn.execute("SELECT sent_at, error FROM alerts WHERE dedupe_key = 'k5'").fetchone()
    assert row["sent_at"] is None and row["error"] == "OSError"  # kept in the app, the watch goes on


def test_health_and_security_alerts(ctx: AppContext) -> None:
    with ctx.db.transaction() as conn:
        old = (NOW - timedelta(hours=30)).isoformat()
        recent = (NOW - timedelta(hours=1)).isoformat()
        conn.execute("INSERT INTO collect_runs (cycle_id, source, family, started_at, ok) VALUES ('c', 'gdacs', "
                     "'hazard', ?, 1)", (old,))
        conn.execute("INSERT INTO collect_runs (cycle_id, source, family, started_at, ok) VALUES ('c', 'gdacs', "
                     "'hazard', ?, 0)", (recent,))
        conn.execute("INSERT INTO cycles (id, started_at, ok) VALUES ('c1', ?, 1)", (old,))
        conn.execute("INSERT INTO cycles (id, started_at, ok) VALUES ('c2', ?, 0)", (recent,))
        alerts = service.health_alerts(conn, NOW)
        audit(conn, "unlock_blocked")
        audit(conn, "session_opened")
        security = service.security_alerts(conn, 0)
    assert {a.kind for a in alerts} == {"silent_source", "unverified"}
    assert any("gdacs" in a.text for a in alerts)
    assert len(security) == 1 and security[0].urgent and "bloquée" in security[0].text


# --- weekly digest ----------------------------------------------------------------------------------------
def test_weekly_digest_from_sunday_to_monday(ctx: AppContext) -> None:
    prepared(ctx)
    backend = ReplayBatchBackend()
    assert digest.submit(ctx.db, ctx.budget, backend, NOW - timedelta(hours=8), ai=True) == 0  # Sunday morning
    assert digest.submit(ctx.db, ctx.budget, backend, SUNDAY_EVENING, ai=True) == 1
    (requests,) = backend.batches.values()
    params = requests[0][1]
    assert params["thinking"] == {"type": "between_tools"} and params["output_config"]["effort"] == "medium"
    assert "<verdict>" in params["messages"][0]["content"] and "37.2917" not in json.dumps(params)
    disagreeing = copy.deepcopy(EXAMPLE)
    disagreeing.update(disagrees_with_verdict=True, disagreement_reason="Les stocks semblent suffire.",
                       price_comment="Voir https://evil.example pour le détail.")
    backend.responses = {requests[0][0]: message(disagreeing)}
    assert digest.collect(ctx.db, ctx.budget, backend) == 1
    assert digest.compose(ctx.db, SUNDAY_EVENING) is None  # sent on Monday only
    week, text = digest.compose(ctx.db, MONDAY_MORNING)  # type: ignore[misc]
    assert week == "2026-W42"
    assert "acrylonitrile : Problème potentiel à 6 semaines." in text
    assert "L'IA conteste ce verdict, qui reste inchangé" in text
    assert "https://" not in text and "evil[.]example" in text
    assert "Empreinte du registre : " in text
    assert digest.compose(ctx.db, MONDAY_MORNING) is None  # once a week
    with ctx.db.connect() as conn:
        entries = chain.entries(conn, kinds=("verdict",))
        assert chain.verify(conn).ok
    assert entries[-1]["versions"] == {"digest": "2026-W42"}  # the verdict at digest time is in the ledger


def test_digest_written_by_code_in_economy_mode(ctx: AppContext) -> None:
    prepared(ctx)
    backend = ReplayBatchBackend()
    assert digest.submit(ctx.db, ctx.budget, backend, SUNDAY_EVENING, ai=False) == 0
    assert backend.batches == {}
    _, text = digest.compose(ctx.db, MONDAY_MORNING)  # type: ignore[misc]
    assert "Synthèse rédigée par le code seul" in text and "Problème potentiel à 6 semaines" in text


def test_rejected_digest_falls_back_to_code(ctx: AppContext) -> None:
    prepared(ctx)
    backend = ReplayBatchBackend()
    digest.submit(ctx.db, ctx.budget, backend, SUNDAY_EVENING, ai=True)
    (requests,) = backend.batches.values()
    bad = copy.deepcopy(EXAMPLE)
    bad["headline"] = " ".join(["mot"] * 20)  # too long
    bad["injection_suspected"] = False
    backend.responses = {requests[0][0]: message(bad)}
    digest.collect(ctx.db, ctx.budget, backend)
    _, text = digest.compose(ctx.db, MONDAY_MORNING)  # type: ignore[misc]
    assert "mot mot mot" not in text and "code seul" in text
    assert assess  # the verdict itself never comes from the digest


def test_source_without_key_is_said_once_not_every_day(ctx: AppContext) -> None:
    with ctx.db.transaction() as conn:
        for hours in (50, 26, 1):
            conn.execute("INSERT INTO collect_runs (cycle_id, source, family, started_at, ok, error) VALUES ('c', "
                         "'nasa_firms', 'hazard', ?, 0, 'SourceError: missing key')",
                         ((NOW - timedelta(hours=hours)).isoformat(),))
        alerts = [a for a in service.health_alerts(conn, NOW) if "nasa_firms" in a.text]
        later = [a for a in service.health_alerts(conn, NOW + timedelta(days=1)) if "nasa_firms" in a.text]
    assert [a.kind for a in alerts] == ["missing_key"] and "setup" in alerts[0].text
    assert later[0].dedupe_key == alerts[0].dedupe_key  # the same key: dispatched once, never again
