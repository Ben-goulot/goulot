"""Market context for the analysis (docs/SPEC.md, section 6), computed by code on the stored series.

For each horizon (1, 5, 20 sessions): the materiality threshold is one standard deviation of the horizon's returns
over a rolling year (250 sessions); the base rates are the historical shares of windows whose move exceeds it,
up and down. A daily return beyond 3 standard deviations is a price anomaly, which can trigger an analysis.
"""

from __future__ import annotations

import statistics
from datetime import date
from typing import Any

from goulot.ledger.rules import Series, pct

HORIZONS = {"1d": 1, "5d": 5, "20d": 20}
YEAR = 250
ANOMALY_Z = 3.0


def _returns(values: list[float], h: int) -> list[float]:
    return [pct(values[i - h], values[i]) for i in range(h, len(values)) if values[i - h]]


def horizon_stats(s: Series, h: int) -> dict[str, float] | None:
    values = [v for _, v in s.points[-(YEAR + h):]]
    rets = _returns(values, h)
    if len(rets) < 30:
        return None
    sd = statistics.pstdev(rets)
    if sd == 0:
        return None
    return {"materiality_pct": round(sd, 3),
            "base_rate_up": round(sum(r > sd for r in rets) / len(rets), 3),
            "base_rate_down": round(sum(r < -sd for r in rets) / len(rets), 3)}


def anomaly_z(s: Series) -> float | None:
    values = [v for _, v in s.points[-(YEAR + 1):]]
    rets = _returns(values, 1)
    if len(rets) < 60:
        return None
    sd = statistics.pstdev(rets[:-1])
    return round(rets[-1] / sd, 2) if sd else None


def market_context(instrument_id: str, s: Series, unit: str, since: date | None) -> dict[str, Any]:
    """One instrument of `<market_context>`: closed list, variations, thresholds and base rates by horizon."""
    last_day, last = s.points[-1]
    ctx: dict[str, Any] = {
        "instrument_id": instrument_id, "name": s.name, "rank": s.rank, "frequency": s.frequency,
        "move_unit": "percent", "price_unit": unit, "last": last, "last_date": last_day.isoformat(),
    }
    if since is not None and (first := s.last_before(since)) is not None:
        ctx["change_since_first_source_pct"] = round(pct(first[1], last), 3)
    if len(s.points) > 5:
        ctx["change_5_sessions_pct"] = round(pct(s.points[-6][1], last), 3)
    if s.frequency == "daily":
        ctx["horizons"] = {k: st for k, h in HORIZONS.items() if (st := horizon_stats(s, h))}
    return ctx
