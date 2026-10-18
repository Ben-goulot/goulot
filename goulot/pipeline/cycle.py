"""The watch cycle, every 3 hours and at start-up (catch-up after sleep). One cycle at a time:

collection and rule filter → prices → events from measurements → triage (Batch) → grouping and corroboration
→ impact analyses (Batch, or standard when serious) → market verdicts → outcomes in the ledger → alerts → weekly
digest → external heartbeat → purge of old irrelevant articles.

The watch budget sets what the AI may do (CLAUDE.md, Budget): normal; economy at 80 % (corroborated severity ≥ 2
only, one Batch a day, no AI digest); crisis reserve at 90 % (severity ≥ 3 only); rules only at 100 %. Without an
API key, the AI steps wait and verdicts are computed from what exists.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import anthropic

from goulot.alerts import digest, service
from goulot.alerts.channels import send_email, send_telegram
from goulot.api.context import AppContext, MissingAPIKey
from goulot.collect.runner import CycleResult, freshness, run_cycle
from goulot.db.connection import get_setting, set_setting
from goulot.llm.budget import (
    CRISIS,
    ECONOMY,
    RULES_ONLY,
    BudgetExceeded,
    CircuitOpen,
    WatchMode,
    raise_headroom,
    watch_mode,
)
from goulot.net.allowlist import load_allowlist
from goulot.net.client import build_client, fetch
from goulot.pipeline import assess, grouping, triage
from goulot.pipeline.assess import Allowance
from goulot.prices import collect as price_collect
from goulot.security.secrets import get_secret

log = logging.getLogger("goulot.pipeline")
_lock = threading.Lock()


def allowance_for(mode: str) -> Allowance:
    if mode == RULES_ONLY:
        return Allowance(analyze=False, rules_only=True)
    if mode == CRISIS:
        return Allowance(min_severity=3, one_batch_a_day=True)
    if mode == ECONOMY:
        return Allowance(min_severity=2, one_batch_a_day=True)
    return Allowance()


def _batch_today(ctx: AppContext, purpose: str, now: datetime) -> bool:
    with ctx.db.connect() as conn:
        row = conn.execute("SELECT MAX(submitted_at) FROM llm_batches WHERE purpose = ?", (purpose,)).fetchone()
    if not row[0]:
        return False
    last = datetime.fromisoformat(row[0])
    return now - (last if last.tzinfo else last.replace(tzinfo=UTC)) < timedelta(hours=24)


def sender(ctx: AppContext) -> service.Sender | None:
    """The owner's channel, or None when none is configured (alerts are then only kept in the app)."""
    with ctx.db.connect() as conn:
        channel = get_setting(conn, "alert_channel")
        chat_id = get_setting(conn, "telegram_chat_id") or ""
        smtp = {k: get_setting(conn, f"smtp_{k}") or "" for k in ("host", "port", "user")}
        to = get_setting(conn, "alert_email_to") or ""
    if channel == "telegram":
        token = get_secret("telegram_bot")
        if not token or not chat_id:
            return None

        def telegram(text: str) -> None:
            client = ctx.http_client()
            try:
                send_telegram(client, token, chat_id, text)
            finally:
                client.close()

        return telegram
    if channel == "email":
        password = get_secret("smtp_password")
        if not (password and smtp["host"] and to):
            return None

        def email(text: str) -> None:
            send_email(smtp["host"], int(smtp["port"] or 465), smtp["user"], password, to, "Goulot", text)

        return email
    return None


def heartbeat(ctx: AppContext) -> None:
    """Optional external heartbeat (docs/SPEC.md, section 9): a call without any data to a secret address."""
    with ctx.db.connect() as conn:
        if get_setting(conn, "heartbeat_enabled") != "1":
            return
    url = get_secret("healthcheck")
    host = urlsplit(url or "").hostname
    if not url or not host or not url.startswith("https://"):
        return
    client = build_client(load_allowlist(ctx.settings.config_dir, [host]))  # its host only once enabled
    try:
        fetch(client, url, max_bytes=10_000)
    except Exception as exc:
        log.warning("heartbeat failed: %s", type(exc).__name__)
    finally:
        client.close()


def process(ctx: AppContext, now: datetime) -> None:
    """Everything after collection; a missing key or budget pauses the AI steps, never the rest."""
    mode = watch_mode(ctx.budget)
    allowance = allowance_for(mode.mode)
    with ctx.db.transaction() as conn:
        grouping.events_from_measurements(conn)
    try:
        std = ctx.llm_backend()
    except MissingAPIKey:
        std = None
    backend = None
    try:
        backend = ctx.llm_batch()
        triage.collect_pending(ctx.db, ctx.budget, backend)
        if not allowance.rules_only and not (allowance.one_batch_a_day and _batch_today(ctx, "triage", now)):
            triage.submit_pending(ctx.db, ctx.budget, backend)
    except (MissingAPIKey, BudgetExceeded, CircuitOpen) as exc:
        log.warning("triage skipped: %s", type(exc).__name__)
    except anthropic.APIError as exc:
        log.warning("triage batch failed: %s", type(exc).__name__)
    with ctx.db.transaction() as conn:
        grouping.group_triaged(conn)
    try:
        if allowance.one_batch_a_day and _batch_today(ctx, "impact", now):
            allowance = Allowance(analyze=False, rules_only=allowance.rules_only)
        assess.run_analyses(ctx.db, ctx.budget, std, backend, now, allowance)
    except (BudgetExceeded, CircuitOpen) as exc:
        log.warning("analyses skipped: %s", type(exc).__name__)
    except anthropic.APIError as exc:
        log.warning("analysis batch failed: %s", type(exc).__name__)
    fresh = freshness(ctx.db, now)
    changed = assess.refresh_verdicts(ctx.db, ctx.vault, now, verified=fresh["verified"],
                                      last_ok_cycle=fresh["last_ok_cycle"], rules_only=allowance.rules_only)
    assess.resolve_outcomes(ctx.db, now)
    try:
        if backend is not None:
            digest.collect(ctx.db, ctx.budget, backend)
        digest.submit(ctx.db, ctx.budget, backend, now, ai=mode.mode not in (ECONOMY, CRISIS, RULES_ONLY))
    except (BudgetExceeded, CircuitOpen, anthropic.APIError) as exc:
        log.warning("digest batch skipped: %s", type(exc).__name__)
    notify(ctx, now, changed, mode)
    if fresh["verified"]:
        heartbeat(ctx)
    with ctx.db.transaction() as conn:
        triage.purge_irrelevant(conn, now.isoformat(timespec="seconds"))


def notify(ctx: AppContext, now: datetime, changed: Sequence[tuple[str, object]], mode: WatchMode) -> int:
    with ctx.db.transaction() as conn:
        last_audit = int(get_setting(conn, "alerts_last_audit_id") or 0)
        serious = conn.execute("SELECT 1 FROM events WHERE confirmation = 'confirmed' AND severity_hint >= 3 "
                               "AND event_status IN ('ongoing', 'escalating') LIMIT 1").fetchone() is not None
        alerts = [
            *service.security_alerts(conn, last_audit),
            *service.verdict_alerts(conn, changed, now),
            *service.event_alerts(conn),
            *service.budget_alerts(conn, mode, now, serious, raise_headroom(ctx.budget)),
            *service.health_alerts(conn, now),
        ]
        top = conn.execute("SELECT COALESCE(MAX(id), 0) FROM audit_log").fetchone()[0]
        set_setting(conn, "alerts_last_audit_id", str(top))
    composed = digest.compose(ctx.db, now)
    if composed is not None:
        week, text = composed
        alerts.append(service.Alert("digest", f"digest-{week}", text))
    return service.dispatch(ctx.db, alerts, sender(ctx), now)


def run(ctx: AppContext) -> CycleResult | None:
    if not _lock.acquire(blocking=False):
        log.info("cycle skipped: another one is running")
        return None
    try:
        now = datetime.fromtimestamp(ctx.clock.time(), UTC)
        http = ctx.http_client()
        try:
            result = run_cycle(ctx.db, http, get_secret, now)
            price_collect.run(ctx.db, http, get_secret, now)
        finally:
            http.close()
        process(ctx, now)
        return result
    except Exception:
        log.exception("watch cycle crashed")
        return None
    finally:
        _lock.release()
