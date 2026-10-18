"""Verdict, delay X and price pressure, computed by code (docs/SPEC.md, section 5; CLAUDE.md, rule 4).

Market scope only here (public data); the personal verdict arrives with Ma chaîne (phase 4). The model never
writes a verdict: it gives severities, durations and price hypotheses, which these rules turn into one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_THRESHOLD_PCT = 5.0
DEFAULT_SUPPLIER_LEAD_WEEKS = 4.0
DEFAULT_DURATION_DAYS = 30
PRICE_ONLY_X_WEEKS = 4.0  # 20 trading days
UNKNOWN_X_WEEKS = 4.0

RAS, POTENTIAL, ANTICIPATE, UNVERIFIED = "ras", "pot", "ant", "unverified"
LABELS = {RAS: "Rien à signaler", POTENTIAL: "Problème potentiel", ANTICIPATE: "Anticiper stock",
          UNVERIFIED: "Non vérifié"}


@dataclass
class EventInput:
    id: str
    confirmed: bool
    best_lineage: float
    node_keys: list[str]
    analysis: dict[str, Any] | None = None  # validated impact assessment, never a quarantined one
    exposed_share_pct: float | None = None  # rules-only mode: share of world supply exposed


@dataclass
class Settings:
    threshold_pct: float = DEFAULT_THRESHOLD_PCT
    supplier_lead_weeks: float = DEFAULT_SUPPLIER_LEAD_WEEKS
    supplier_lead_assumed: bool = True


@dataclass
class EventEffect:
    id: str
    severity: int  # tightening severity on the material or a direct input
    loosening: int
    p_up: float | None
    rise_pct: float | None
    p_down: float | None
    fall_pct: float | None
    duration_high: float
    duration_assumed: bool
    x_weeks: float | None
    x_low: float | None
    x_high: float | None
    absorbed: bool
    retained_by: str = ""


@dataclass
class Verdict:
    verdict: str
    pressure: int
    x_weeks: float | None = None
    x_low: float | None = None
    x_high: float | None = None
    window_weeks: float | None = None
    window_assumed: bool = True
    rules_only: bool = False
    event_ids: list[str] = field(default_factory=list)
    explanation: str = ""
    effects: list[EventEffect] = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.verdict == POTENTIAL and self.x_weeks is not None:
            return f"Problème potentiel à {self.x_weeks:.0f} semaine{'s' if self.x_weeks >= 2 else ''}"
        return LABELS[self.verdict]


def severity_from_share(share_pct: float | None) -> int:
    """Scale of `prompts/impact_v1.md`, used in rules-only mode."""
    if share_pct is None or share_pct < 0.1:
        return 0
    return 1 if share_pct < 0.5 else 2 if share_pct < 2 else 3 if share_pct < 5 else 4


def _hypothesis_move(h: dict[str, Any], last_prices: dict[str, float]) -> float | None:
    mid = (float(h["move_p10"]) + float(h["move_p90"])) / 2
    if h.get("move_unit") == "percent":
        return mid
    price = last_prices.get(h["instrument_id"])
    return 100 * mid / price if price else None


def effect_of(event: EventInput, commodities: set[str], instruments: set[str], delays: dict[str, dict[str, float]],
              last_prices: dict[str, float], rules_only: bool) -> EventEffect:
    severity = loosening = 0
    duration_high: float | None = None
    p_up = rise = p_down = fall = None
    if rules_only or event.analysis is None:
        severity = severity_from_share(event.exposed_share_pct) if rules_only else 0
    else:
        for ci in event.analysis.get("commodity_impacts", []):
            if ci["commodity"] not in commodities:
                continue
            if ci["net_balance_effect"] == "tightening":
                severity = max(severity, int(ci["severity"]))
                d = ci["expected_duration_days"]
                candidate = d["high"] if d["high"] is not None else d["low"]
                if candidate is not None:
                    duration_high = max(duration_high or 0.0, float(candidate))
            elif ci["net_balance_effect"] == "loosening":
                loosening = max(loosening, int(ci["severity"]))
        for h in event.analysis.get("hypotheses", []):
            if h["metric"] != "outright_price" or h["horizon"] != "20d" or h["instrument_id"] not in instruments:
                continue
            move = _hypothesis_move(h, last_prices)
            if move is None:
                continue
            if h["direction"] == "up" and (p_up is None or h["probability"] > p_up):
                p_up, rise = float(h["probability"]), move
            elif h["direction"] == "down" and (p_down is None or h["probability"] > p_down):
                p_down, fall = float(h["probability"]), -move
    reach = [delays[k] for k in event.node_keys if k in delays]
    best = min(reach, key=lambda d: d["days"]) if reach else None
    duration = duration_high if duration_high is not None else DEFAULT_DURATION_DAYS
    return EventEffect(
        id=event.id, severity=severity, loosening=loosening, p_up=p_up, rise_pct=rise, p_down=p_down, fall_pct=fall,
        duration_high=duration, duration_assumed=duration_high is None,
        x_weeks=round(best["days"] / 7, 1) if best else None,
        x_low=round(best["low"] / 7, 1) if best else None, x_high=round(best["high"] / 7, 1) if best else None,
        absorbed=bool(best and duration < best.get("buffers", 0)),
    )


def compute(events: list[EventInput], *, commodities: set[str], instruments: set[str],
            delays: dict[str, dict[str, float]], last_prices: dict[str, float] | None = None,
            settings: Settings | None = None, verified: bool = True, rules_only: bool = False) -> Verdict:
    settings = settings or Settings()
    last_prices = last_prices or {}
    thr = settings.threshold_pct
    effects = [effect_of(e, commodities, instruments, delays, last_prices, rules_only) for e in events]
    by_id = {e.id: e for e in events}
    anticipate: list[EventEffect] = []
    potential: list[EventEffect] = []
    for eff in effects:
        ev = by_id[eff.id]
        backed = ev.confirmed or ev.best_lineage >= 0.7  # early signal backed by a reliable lineage
        x_event = eff.x_weeks if eff.x_weeks is not None else UNKNOWN_X_WEEKS
        window = x_event - settings.supplier_lead_weeks
        price_rule = not rules_only and eff.p_up is not None and eff.rise_pct is not None and eff.rise_pct >= thr
        physical = eff.severity >= 2 and not eff.absorbed  # an absorbed event keeps only its price effect
        if ev.confirmed and ((physical and window <= 2) or (price_rule and (eff.p_up or 0) >= 0.5)):
            eff.retained_by = "physique" if physical and window <= 2 else "prix"
            anticipate.append(eff)
        elif backed and (physical or (price_rule and (eff.p_up or 0) >= 0.25)):
            eff.retained_by = "physique" if physical else "prix"
            potential.append(eff)
    retained = anticipate or potential
    verdict = ANTICIPATE if anticipate else POTENTIAL if potential else RAS
    x: float | None = None
    xl: float | None = None
    xh: float | None = None
    if retained:
        physical_effects = [e for e in retained if e.retained_by == "physique"]
        if physical_effects:
            first = min(physical_effects, key=lambda e: e.x_weeks if e.x_weeks is not None else UNKNOWN_X_WEEKS)
            x = first.x_weeks if first.x_weeks is not None else UNKNOWN_X_WEEKS
            xl = first.x_low if first.x_low is not None else x
            xh = first.x_high if first.x_high is not None else x
        else:
            x = xl = xh = PRICE_ONLY_X_WEEKS
    if verdict == RAS and not verified:
        verdict = UNVERIFIED
    result = Verdict(
        verdict=verdict, pressure=_pressure(effects, by_id, thr, rules_only), x_weeks=x, x_low=xl, x_high=xh,
        window_weeks=round(x - settings.supplier_lead_weeks, 1) if x is not None else None,
        window_assumed=settings.supplier_lead_assumed, rules_only=rules_only,
        event_ids=[e.id for e in retained], effects=effects,
    )
    result.explanation = explain(result)
    return result


def _pressure(effects: list[EventEffect], by_id: dict[str, EventInput], thr: float, rules_only: bool) -> int:
    def confirmed(e: EventEffect) -> bool:
        return by_id[e.id].confirmed

    def backed(e: EventEffect) -> bool:
        return by_id[e.id].confirmed or by_id[e.id].best_lineage >= 0.7

    def up(e: EventEffect, p: float) -> bool:
        return not rules_only and (e.p_up or 0) >= p and (e.rise_pct or 0) >= thr

    def down(e: EventEffect, p: float) -> bool:
        return not rules_only and (e.p_down or 0) >= p and (e.fall_pct or 0) >= thr

    if any((confirmed(e) and e.severity >= 3) or up(e, 0.5) for e in effects):
        return 2
    if any((backed(e) and e.severity >= 2) or up(e, 0.25) for e in effects):
        return 1
    if any((confirmed(e) and e.loosening >= 3) or down(e, 0.5) for e in effects):
        return -2
    if any((confirmed(e) and e.loosening >= 2) or down(e, 0.25) for e in effects):
        return -1
    return 0


PRESSURE_LABELS = {2: "Forte hausse", 1: "Hausse", 0: "Neutre", -1: "Baisse", -2: "Forte baisse"}


def explain(v: Verdict) -> str:
    """Short French explanation written by the code (the analysis text is shown apart, as text)."""
    if v.verdict == UNVERIFIED:
        return "Aucun cycle de veille réussi depuis plus de 12 heures : rien ne garantit qu'il n'y a rien à signaler."
    if v.verdict == RAS:
        base = "Aucun événement retenu ne menace l'approvisionnement à l'horizon de la veille."
    else:
        rules = sorted({e.retained_by for e in v.effects if e.id in v.event_ids})
        base = (f"{len(v.event_ids)} événement{'s' if len(v.event_ids) > 1 else ''} retenu"
                f"{'s' if len(v.event_ids) > 1 else ''} (règle {' et '.join(rules)}) ; "
                f"délai avant impact {v.x_low:.0f} à {v.x_high:.0f} semaines.")
        if v.window_weeks is not None:
            base += (f" Fenêtre d'action : {v.window_weeks:.0f} semaine{'s' if abs(v.window_weeks) >= 2 else ''}"
                     + (" (délai fournisseur supposé de 4 semaines)." if v.window_assumed else "."))
    if v.rules_only:
        base += " Estimation sans IA : plafond de veille atteint."
    if v.pressure < 0:
        base += " Pas d'urgence à acheter."
    return base
