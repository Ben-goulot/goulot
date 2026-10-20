"""Threats 8, 13 and 18 (docs/SECURITY.md): links in alerts, spending caps, circuit breaker, Telegram send-only."""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from goulot.alerts import channels, service
from goulot.alerts.defang import defang
from goulot.api.context import AppContext
from goulot.llm.batch import ReplayBatchBackend
from goulot.llm.budget import (
    CRISIS,
    ECONOMY,
    NORMAL,
    RULES_ONLY,
    Budget,
    BudgetExceeded,
    CallRecord,
    CircuitOpen,
    watch_mode,
)
from goulot.llm.pricing import Usage
from goulot.net.allowlist import Allowlist
from goulot.net.client import build_client
from goulot.pipeline import cycle
from tests.test_collect import NOW
from tests.test_triage_events import collect_cycle

TRAP = "Fermeture du port, détails sur https://evil.example/login et www.phish.com, contact admin@evil.org"


def spend(budget: Budget, usd: float, name: str = "watch", mode: str = "batch") -> None:
    budget.record(CallRecord(name, "test", "claude-sonnet-5-5", mode, Usage(), usd))


def test_alert_links_defanged(ctx: AppContext) -> None:
    assert defang(TRAP) == ("Fermeture du port, détails sur hxxps://evil[.]example/login et www[.]phish[.]com, "
                            "contact admin[@]evil[.]org")
    with ctx.db.transaction() as conn:
        conn.execute("INSERT INTO materials (id, name, cas, status, created_at, updated_at) VALUES "
                     "('m1', 'acrylonitrile', '107-13-1', 'validated', 'x', 'x')")
        conn.execute("INSERT INTO events (id, material_id, event_type, event_status, origin, title, place, "
                     "first_seen_at, last_seen_at, confirmation, severity_hint, updated_at) VALUES ('E1', 'm1', "
                     "'labor_action', 'ongoing', 'news', ?, 'evil.example', 'x', 'x', 'confirmed', 3, 'x')", (TRAP,))
        conn.execute("INSERT INTO event_links (event_id, material_id, node_key) VALUES ('E1', 'm1', 'port_a')")
        alerts = service.event_alerts(conn)
    sent: list[str] = []
    service.dispatch(ctx.db, alerts, sent.append, NOW)
    (text,) = sent
    assert "https://" not in text and "evil.example" not in text and "@evil" not in text
    assert "hxxps://evil[.]example" in text
    with ctx.db.connect() as conn:
        stored = conn.execute("SELECT text FROM alerts").fetchone()[0]
    assert stored == text  # what is kept is what was sent
    payload = channels.telegram_payload("12345", TRAP)
    assert "parse_mode" not in payload and payload["link_preview_options"] == {"is_disabled": True}
    assert "https://" not in payload["text"]
    mail = channels.build_email("a@x.example", "b@x.example", "Sujet https://evil.example", TRAP)
    assert "https://" not in mail.get_content() and "https://" not in str(mail["Subject"])


def test_telegram_send_only() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"ok": True})

    client = build_client(Allowlist({"api.telegram.org"}), transport=httpx2.MockTransport(handler))
    channels.send_telegram(client, "123456:ABC", "-100123", "Goulot : test")
    (request,) = seen
    assert request.method == "POST" and request.url.path.endswith("/sendMessage")
    assert set(json.loads(request.content)) == {"chat_id", "text", "link_preview_options"}
    with pytest.raises(channels.ChannelError):
        channels.send_telegram(client, "123456:ABC", "@someone", "x")  # only the owner's numeric chat id
    source = inspect.getsource(channels) + inspect.getsource(service) + inspect.getsource(cycle)
    for forbidden in ("/getUpdates", "/setWebhook", "\"parse_mode\"", "parse_mode="):
        assert forbidden not in source, forbidden  # the bot never reads, never formats


def test_email_refuses_private_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(channels.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("192.168.1.10", 465))])
    with pytest.raises(channels.ChannelError, match="non-public"):
        channels.send_email("smtp.example.org", 465, "me", "pw", "me@example.org", "Goulot", "x")


def test_budget_cap_modes(ctx: AppContext) -> None:
    budget = ctx.budget
    assert watch_mode(budget).mode == NORMAL
    spend(budget, 4.05)
    assert watch_mode(budget).mode == ECONOMY  # 81 %
    spend(budget, 0.5)
    assert watch_mode(budget).mode == CRISIS  # 91 %
    spend(budget, 0.5)
    assert watch_mode(budget).mode == RULES_ONLY  # 101 %
    assert cycle.allowance_for(ECONOMY).min_severity == 2 and cycle.allowance_for(CRISIS).min_severity == 3
    assert cycle.allowance_for(RULES_ONLY).rules_only
    # At 100 %, a cycle sends nothing to the AI, but collection and verdicts go on.
    from tests.test_collect import validated_chain

    validated_chain(ctx)
    collect_cycle(ctx)
    backend = ReplayBatchBackend()
    ctx.llm_batch = lambda: backend
    cycle.process(ctx, NOW)
    assert backend.batches == {}
    with ctx.db.connect() as conn:
        kinds = [r[0] for r in conn.execute("SELECT dedupe_key FROM alerts")]
        verdicts = conn.execute("SELECT rules_only FROM verdicts WHERE current = 1").fetchall()
    assert f"budget-100-{NOW:%Y-%m}" in kinds and all(v[0] == 1 for v in verdicts)
    cycle.process(ctx, NOW + timedelta(hours=3))
    with ctx.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM alerts WHERE kind = 'budget'").fetchone()[0] == 1  # once a month
    # Nothing rises by itself; a raise needs a serious event and stays within the 15 $ workspace limit.
    with pytest.raises(BudgetExceeded):
        budget.raise_watch_cap(20)
    spend(budget, 2.0, "reconstruction", mode="standard")
    assert budget.raise_watch_cap(3.0) == 8.0  # 15 − 2 of reconstructions − 5 of cap = 8 possible, 3 taken
    assert watch_mode(budget).mode == NORMAL or watch_mode(budget).cap == 8.0


def test_raise_route_needs_a_serious_event(ctx: AppContext) -> None:
    from fastapi.testclient import TestClient

    from goulot.api.app import create_app
    from tests.conftest import ORIGIN, login, unsafe_headers

    with TestClient(create_app(ctx), base_url=ORIGIN) as client:
        h = unsafe_headers(login(client, ctx))
        r = client.post("/api/budget/raise", json={"extra_usd": 2}, headers=h)
        assert r.status_code == 409 and "grave" in r.json()["detail"]
        assert client.get("/api/budget").json()["raise_headroom_usd"] == 0.0


def test_circuit_breaker(ctx: AppContext) -> None:
    budget = Budget(ctx.db, ctx.clock)
    for _ in range(30):
        spend(budget, 0.0)  # batch results: not a runaway loop
    budget.check_breaker()
    for _ in range(21):
        spend(budget, 0.0, mode="standard")
    with pytest.raises(CircuitOpen):
        budget.check("watch", 0.01)
    ctx.clock.advance(3601)  # type: ignore[attr-defined]
    budget.check("watch", 0.01)
    for _ in range(3):
        budget.record(CallRecord("watch", "test", "claude-sonnet-5-5", "standard", Usage(), 0.0, ok=False))
    with pytest.raises(CircuitOpen):
        budget.check("watch", 0.01)
    with ctx.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE kind = 'llm_circuit_open'").fetchone()[0] == 2
    alerts = []
    with ctx.db.connect() as conn:
        alerts = service.security_alerts(conn, 0)
    assert any("disjoncteur" in a.text for a in alerts)
    assert datetime.now(UTC)
