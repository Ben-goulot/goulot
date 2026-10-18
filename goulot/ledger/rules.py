"""Outcome rules (docs/SPEC.md, section 7). Pure functions on price series; nothing reads a modified state.

Silence never counts as a success: without an observed price in the window, the result is « non résolu ». A result
obtained on the proxy index (rank 5; rank 4 or 5 for a price pressure) is marked « résolu sur indice ». Each rule
takes the thresholds in force on the day of the advice (the caller reads them from `settings_change` entries).
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta

PENDING, UNRESOLVED = "pending", "unresolved"
SESSIONS = {"1d": 1, "5d": 5, "20d": 20}
CALM_WINDOW = timedelta(weeks=8)
PRESSURE_WINDOWS = {"daily": None, "monthly": timedelta(days=62), "customs": timedelta(days=92)}
PRESSURE_SESSIONS = 20


@dataclass
class Series:
    points: list[tuple[date, float]]  # sorted by date
    rank: int  # 1 quote, 2 official price, 3 customs unit price, 4 family index, 5 proxy index
    frequency: str = "daily"  # daily | monthly | customs
    name: str = ""
    dates: list[date] = field(init=False)

    def __post_init__(self) -> None:
        self.points = sorted(self.points)
        self.dates = [d for d, _ in self.points]

    def last_before(self, day: date) -> tuple[date, float] | None:
        i = bisect_right(self.dates, day)
        return self.points[i - 1] if i else None

    def between(self, start: date, end: date) -> list[tuple[date, float]]:
        """Points strictly after `start`, up to `end` included."""
        return self.points[bisect_right(self.dates, start):bisect_right(self.dates, end)]

    def nth_after(self, day: date, n: int) -> tuple[date, float] | None:
        i = bisect_right(self.dates, day) + n - 1
        return self.points[i] if i < len(self.points) else None

    def covers(self, day: date) -> bool:
        return bool(self.dates) and self.dates[-1] >= day


def pct(a: float, b: float) -> float:
    return 100.0 * (b / a - 1.0) if a else 0.0


@dataclass
class Outcome:
    result: str
    observed_pct: float | None = None
    on_index: bool = False
    lead_weeks: float | None = None
    detail: str = ""


def hypothesis(issued: date, horizon: str, p10: float, p90: float, direction: str, series: Series | None,
               materiality_pct: float, unit: str = "percent", today: date | None = None) -> Outcome:
    """« fourchette juste » if the move at the horizon falls in [p10, p90]; the direction is right if the move
    exceeds the materiality threshold in the announced direction."""
    if series is None or not series.points:
        return Outcome(UNRESOLVED, detail="aucune série observée")
    if series.frequency != "daily":
        return Outcome(UNRESOLVED, detail="série non quotidienne")
    start = series.last_before(issued)
    end = series.nth_after(issued, SESSIONS[horizon])
    if start is None:
        return Outcome(UNRESOLVED, detail="aucun prix avant le conseil")
    if end is None:
        # The horizon is not reached in the series: wait, unless the series has stayed silent far too long.
        stale = today is not None and today > issued + timedelta(days=2 * SESSIONS[horizon] + 30)
        return Outcome(UNRESOLVED, detail="série muette à l'échéance") if stale else Outcome(PENDING)
    move = pct(start[1], end[1]) if unit == "percent" else end[1] - start[1]
    hit = p10 <= move <= p90
    sign = 1 if direction == "up" else -1
    moved = (pct(start[1], end[1]) * sign) >= materiality_pct
    return Outcome("range_hit" if hit else "range_missed", round(move, 3), series.rank >= 5,
                   detail="sens juste" if moved else "sens non confirmé")


def warning(issued: date, x_weeks: float | None, series: Series | None, threshold_pct: float,
            today: date) -> Outcome:
    """« Anticiper stock » or « Problème potentiel »: confirmed if the price rises by the threshold within
    [issue; issue + max(X, 4 weeks) + 2 weeks], otherwise a false alarm."""
    window_end = issued + timedelta(weeks=max(x_weeks or 4.0, 4.0) + 2)
    if series is None or not series.points:
        return Outcome(UNRESOLVED if today > window_end else PENDING, detail="aucune série observée")
    start = series.last_before(issued)
    if start is None:
        return Outcome(UNRESOLVED if today > window_end else PENDING, detail="aucun prix avant le conseil")
    for day, value in series.between(issued, window_end):
        rise = pct(start[1], value)
        if rise >= threshold_pct:
            return Outcome("confirmed", round(rise, 3), series.rank >= 5,
                           lead_weeks=round((day - issued).days / 7, 1))
    if today <= window_end:
        return Outcome(PENDING)
    if not series.between(issued, window_end):
        return Outcome(UNRESOLVED, detail="aucun prix dans la fenêtre")
    return Outcome("false_alarm", round(max((pct(start[1], v) for _, v in series.between(issued, window_end)),
                                            default=0.0), 3), series.rank >= 5)


def calm(issued: date, series: Series | None, threshold_pct: float, today: date) -> Outcome:
    """« Rien à signaler »: right if no rise of the threshold within 8 weeks, otherwise a missed rise."""
    window_end = issued + CALM_WINDOW
    if series is None or not series.points:
        return Outcome(UNRESOLVED if today > window_end else PENDING, detail="aucune série observée")
    start = series.last_before(issued)
    if start is None:
        return Outcome(UNRESOLVED if today > window_end else PENDING, detail="aucun prix avant le conseil")
    inside = series.between(issued, window_end)
    for _, value in inside:
        if pct(start[1], value) >= threshold_pct:
            return Outcome("missed_rise", round(pct(start[1], value), 3), series.rank >= 5)
    if today <= window_end:
        return Outcome(PENDING)
    if not inside:
        return Outcome(UNRESOLVED, detail="aucun prix dans la fenêtre")  # silence is never a success
    return Outcome("correct_calm", round(max(pct(start[1], v) for _, v in inside), 3), series.rank >= 5)


def pressure(issued: date, sign: int, series: Series | None, materiality_pct: float, today: date) -> Outcome:
    """Price pressure ≠ 0: right direction if the best series exceeds its materiality threshold in the announced
    direction within 20 sessions (2 months for a monthly series, 3 for a customs price)."""
    if sign == 0:
        raise ValueError("a neutral pressure has no outcome")
    if series is None or not series.points:
        return Outcome(UNRESOLVED, detail="aucune série observée")
    span = PRESSURE_WINDOWS.get(series.frequency)
    if span is None:
        end_point = series.nth_after(issued, PRESSURE_SESSIONS)
        window_end = end_point[0] if end_point else issued + timedelta(days=28)
    else:
        window_end = issued + span
    start = series.last_before(issued)
    if start is None:
        return Outcome(UNRESOLVED, detail="aucun prix avant le conseil")
    on_index = series.rank >= 4
    for _, value in series.between(issued, window_end):
        move = pct(start[1], value)
        if move * sign >= materiality_pct:
            return Outcome("right_direction", round(move, 3), on_index)
        if -move * sign >= materiality_pct:
            return Outcome("wrong_direction", round(move, 3), on_index)
    if today <= window_end or (span is None and series.nth_after(issued, PRESSURE_SESSIONS) is None):
        return Outcome(PENDING)
    if not series.between(issued, window_end):
        return Outcome(UNRESOLVED, detail="aucun prix dans la fenêtre")
    return Outcome("no_effect", None, on_index)


def missed_moves(series: Series, alerts: list[date], threshold_pct: float, sessions: int = 20) -> list[date]:
    """Rises of the threshold over 20 sessions with no « Problème » or « Anticiper » alert in the previous
    8 weeks. One date per episode (the day the threshold is crossed)."""
    found: list[date] = []
    alerts = sorted(alerts)
    for i in range(sessions, len(series.points)):
        day, value = series.points[i]
        base = series.points[i - sessions][1]
        if pct(base, value) < threshold_pct:
            continue
        if found and (day - found[-1]).days < 28:
            continue  # same episode
        j = bisect_left(alerts, day - CALM_WINDOW)
        if j < len(alerts) and alerts[j] <= day:
            continue  # an alert came before the rise
        found.append(day)
    return found
