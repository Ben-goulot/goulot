"""AI spending: every call is recorded in `llm_calls`, caps are checked before each call (CLAUDE.md, Budget).

Budgets: `watch` (5 $ a month, modes in phase 3), `reconstruction` (10 $ a month, quote and confirmation before
each run) and `dev` (the phase 2 test budget, outside the app's caps, all-time total).
Circuit breaker: more than 20 calls in 10 minutes, or 3 errors in a row, pause every AI call for one hour.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from goulot.db.connection import Database, get_setting, set_setting
from goulot.llm.pricing import Usage, monthly_budget
from goulot.security.audit import audit
from goulot.security.clock import Clock

BUDGETS = ("watch", "reconstruction", "dev")
DEV_TEST_BUDGET_USD = 12.0  # validated by the user on 2026-10-16 (expected spend: about 8 $)
BREAKER_MAX_CALLS = 20
BREAKER_WINDOW = timedelta(minutes=10)
BREAKER_MAX_ERRORS = 3
BREAKER_PAUSE = timedelta(hours=1)
PAUSE_KEY = "llm_paused_until"


class BudgetExceeded(RuntimeError):
    """The call would go over a cap: nothing is sent."""


class CircuitOpen(RuntimeError):
    """AI calls are paused by the circuit breaker."""


@dataclass(frozen=True)
class CallRecord:
    budget: str
    purpose: str
    model: str
    mode: str
    usage: Usage
    cost_usd: float
    request_id: str = ""
    material_id: str | None = None
    stop_reason: str = ""
    ok: bool = True


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def month_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class Budget:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock.time(), UTC)

    def cap(self, budget: str) -> float:
        if budget == "dev":
            return DEV_TEST_BUDGET_USD
        with self.db.connect() as conn:
            override = get_setting(conn, f"budget_{budget}_usd")
            month = get_setting(conn, f"budget_{budget}_month")
        # A raise granted by the user applies to its month only; the cap comes back by itself the next month.
        if override and (month is None or month == self._now().strftime("%Y-%m")):
            return float(override)
        return monthly_budget(budget)

    def raise_watch_cap(self, extra_usd: float) -> float:
        """The user's decision, in the app, during a serious event: never automatic."""
        headroom = raise_headroom(self)
        if not 0 < extra_usd <= headroom:
            raise BudgetExceeded(f"Hausse refusée : au plus {headroom:.2f} $ ce mois-ci.")
        new_cap = round(self.cap("watch") + extra_usd, 2)
        with self.db.transaction() as conn:
            set_setting(conn, "budget_watch_usd", str(new_cap))
            set_setting(conn, "budget_watch_month", self._now().strftime("%Y-%m"))
            audit(conn, "watch_cap_raised", extra_usd=extra_usd)
        return new_cap

    def spent(self, budget: str, conn: sqlite3.Connection | None = None) -> float:
        """This month's spend (all time for `dev`)."""
        if budget not in BUDGETS:
            raise ValueError(budget)
        since = "" if budget == "dev" else _iso(month_start(self._now()))
        query = "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE budget = ? AND at >= ?"
        if conn is not None:
            return float(conn.execute(query, (budget, since)).fetchone()[0])
        with self.db.connect() as c:
            return float(c.execute(query, (budget, since)).fetchone()[0])

    def remaining(self, budget: str) -> float:
        return round(self.cap(budget) - self.spent(budget), 6)

    def check(self, budget: str, max_cost_usd: float) -> None:
        """Before a call: refuse if the paused breaker is on, or if the worst case would exceed the cap."""
        self.check_breaker()
        if self.spent(budget) + max_cost_usd > self.cap(budget) + 1e-9:
            raise BudgetExceeded(
                f"Plafond « {budget} » : {self.spent(budget):.2f} $ dépensés sur {self.cap(budget):.2f} $ ; "
                f"cet appel pourrait coûter jusqu'à {max_cost_usd:.2f} $."
            )

    def check_breaker(self) -> None:
        with self.db.connect() as conn:
            paused = get_setting(conn, PAUSE_KEY)
        if paused and datetime.fromisoformat(paused) > self._now():
            raise CircuitOpen(f"Appels IA en pause jusqu'à {paused} (disjoncteur).")

    def record(self, rec: CallRecord) -> None:
        now = self._now()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO llm_calls (at, budget, purpose, model, mode, request_id, material_id, input_tokens, "
                "output_tokens, cache_write_tokens, cache_read_tokens, web_searches, cost_usd, stop_reason, ok) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _iso(now), rec.budget, rec.purpose, rec.model, rec.mode, rec.request_id, rec.material_id,
                    rec.usage.input_tokens, rec.usage.output_tokens, rec.usage.cache_write_tokens,
                    rec.usage.cache_read_tokens, rec.usage.web_searches, rec.cost_usd, rec.stop_reason, int(rec.ok),
                ),
            )
            self._maybe_trip(conn, now)

    def _maybe_trip(self, conn: sqlite3.Connection, now: datetime) -> None:
        # Batch results arrive by the dozen and are not a runaway loop: the breaker counts standard calls only.
        recent = conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE mode = 'standard' AND at >= ?", (_iso(now - BREAKER_WINDOW),)
        ).fetchone()[0]
        last = [r[0] for r in conn.execute(
            "SELECT ok FROM llm_calls WHERE mode = 'standard' ORDER BY id DESC LIMIT ?", (BREAKER_MAX_ERRORS,))]
        errors = len(last) == BREAKER_MAX_ERRORS and not any(last)
        if recent > BREAKER_MAX_CALLS or errors:
            set_setting(conn, PAUSE_KEY, _iso(now + BREAKER_PAUSE))
            audit(conn, "llm_circuit_open", reason="errors" if errors else "rate")


# --- watch budget modes (CLAUDE.md, Budget) ----------------------------------------------------------------
NORMAL, ECONOMY, CRISIS, RULES_ONLY = "normal", "economy", "crisis_reserve", "rules_only"
WORKSPACE_LIMIT_USD = 15.0


@dataclass(frozen=True)
class WatchMode:
    mode: str
    spent: float
    cap: float

    @property
    def share(self) -> float:
        return self.spent / self.cap if self.cap else 1.0


def watch_mode(budget: Budget) -> WatchMode:
    """80 %: economy (corroborated severity ≥ 2 only, one Batch a day, no digest); 90 %: crisis reserve (severity
    ≥ 3 only, or ≥ 2 on a site that weighs for the user, phase 4); 100 %: rules only, and an alert."""
    spent, cap = budget.spent("watch"), budget.cap("watch")
    share = spent / cap if cap else 1.0
    mode = RULES_ONLY if share >= 1 else CRISIS if share >= 0.9 else ECONOMY if share >= 0.8 else NORMAL
    return WatchMode(mode, round(spent, 4), cap)


def raise_headroom(budget: Budget) -> float:
    """How far the watch cap may be raised this month: up to the workspace limit of 15 $, reconstructions of the
    month deducted. Only the user raises it, in the app; nothing rises by itself."""
    return round(max(0.0, WORKSPACE_LIMIT_USD - budget.spent("reconstruction") - budget.cap("watch")), 2)
