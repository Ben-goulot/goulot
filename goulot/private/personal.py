"""Personal verdict (docs/SPEC.md, section 5): the same rules as the market verdict, with the user's paths, shares,
stocks, lead times and thresholds, `s_perso` instead of `s`, and their supplier signals as events.

- `s_perso`: for a confirmed disrupting event (or a supplier signal) on a site of the user's paths, at least 2 when
  the exposure `e` reaches 10 %, at least 3 when it reaches 50 %; otherwise `s`.
- Physical rule only on the user's paths (`e > 0`); the price rule is the market's, with the user's threshold.
- X from the touched site to the user's direct supplier; W = X minus the lead time of the link to the user (or the
  lead time of the coverage, or 4 weeks, shown as an assumption).
- Signals: force majeure and rupture (s_perso ≥ 3), allocation and longer lead time (s_perso ≥ 2; a lead time longer
  by 2 weeks or 50 % replaces that of the link for W), announced rise (price rule met with probability 1, X = weeks
  until it applies), back to normal (closes the site's earlier signals). A signal expires after 90 days.

Everything here is computed on the Mac and stays in the vault: the explanation names the user's sites.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from goulot.pipeline.verdict import (
    ANTICIPATE,
    DEFAULT_SUPPLIER_LEAD_WEEKS,
    DEFAULT_THRESHOLD_PCT,
    POTENTIAL,
    PRICE_ONLY_X_WEEKS,
    RAS,
    UNKNOWN_X_WEEKS,
    UNVERIFIED,
    EventInput,
    Verdict,
    effect_of,
)
from goulot.private.mychain import HIGH_SHARE, WEIGHS_SHARE, Model

DISRUPTING = {"industrial_outage", "natural_disaster", "extreme_weather", "labor_action", "infrastructure_failure",
              "armed_conflict", "civil_unrest", "cyber_incident", "export_restriction", "sanctions_trade_policy"}
LIVE_STATUSES = {"announced", "ongoing", "escalating"}
SIGNAL_TYPES = {"delay": "délai allongé", "price_increase": "hausse annoncée", "allocation": "allocation",
                "force_majeure": "force majeure", "rupture": "rupture", "back_to_normal": "retour à la normale"}
TENSION_SIGNALS = {"price_increase", "allocation", "force_majeure", "rupture"}
SIGNAL_LIFETIME = timedelta(days=90)
SIGNAL_RELIABILITY = 0.9
LONGER_LEAD_WEEKS = 2.0
LONGER_LEAD_RATIO = 0.5


@dataclass
class PersonalEvent:
    event: EventInput
    event_type: str
    event_status: str
    title: str
    sites: list[str] = field(default_factory=list)  # sites of Ma chaîne touched (private links)
    public: bool = True


def active_signals(signals: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """Signals still in force: not older than 90 days, not closed by a later « retour à la normale » of their site."""
    out = []
    for s in signals:
        if s["type"] == "back_to_normal" or date.fromisoformat(s["date"]) + SIGNAL_LIFETIME < today:
            continue
        if not _closed(s, signals):
            out.append(s)
    return out


def _closed(s: dict[str, Any], signals: list[dict[str, Any]]) -> bool:
    return any(o["type"] == "back_to_normal" and o.get("site_id") == s.get("site_id") and o["date"] >= s["date"]
               and o["id"] != s["id"] for o in signals)


def expiring(signals: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """Signals that reached 90 days without a new confirmation (reminder to the user)."""
    return [s for s in signals if s["type"] != "back_to_normal" and not _closed(s, signals)
            and date.fromisoformat(s["date"]) + SIGNAL_LIFETIME < today]


@dataclass
class Retained:
    id: str
    rule: str
    x: float
    x_low: float
    x_high: float
    window: float
    window_assumed: bool
    exposure: float
    s: int
    s_perso: int
    sites: list[str]
    reason: str


@dataclass
class Personal:
    verdict: Verdict
    retained: list[Retained]
    exposures: dict[str, dict[str, Any]]
    plan_b: list[dict[str, Any]]
    threshold_pct: float
    revision: int | None
    signals_used: list[str]


def _lead_weeks(lead_days: float | None, coverage: dict[str, Any] | None) -> tuple[float, bool]:
    """Lead time of the link to the user, else that of the coverage, else 4 weeks (an assumption)."""
    if lead_days is not None:
        return lead_days / 7, False
    if coverage and coverage.get("supplier_lead_time_days") is not None:
        return float(coverage["supplier_lead_time_days"]) / 7, False
    return DEFAULT_SUPPLIER_LEAD_WEEKS, True


def compute(events: list[PersonalEvent], signals: list[dict[str, Any]], model: Model | None, revision: int | None,
            coverage: dict[str, Any] | None, *, commodities: set[str], instruments: set[str],
            delays: dict[str, dict[str, float]], last_prices: dict[str, float], today: date, verified: bool = True,
            rules_only: bool = False) -> Personal:
    thr = float(coverage["price_threshold_pct"]) if coverage and coverage.get("price_threshold_pct") else \
        DEFAULT_THRESHOLD_PCT
    live = active_signals(signals, today)
    overrides: dict[str, float] = {}  # site -> lead time of its outgoing link, replaced by a longer-lead signal
    if model is not None:
        for sig in live:
            if sig["type"] == "delay" and sig.get("new_lead_days") is not None and sig.get("site_id") in model.sites:
                old = sig.get("old_lead_days")
                new = float(sig["new_lead_days"])
                if old is None or new - float(old) >= 7 * LONGER_LEAD_WEEKS or new >= (1 + LONGER_LEAD_RATIO) * old:
                    overrides[sig["site_id"]] = new
    anticipate: list[Retained] = []
    potential: list[Retained] = []
    exposures: dict[str, dict[str, Any]] = {}
    pressure = 0
    touched_sites: set[str] = set()

    for pe in events:
        ev = pe.event
        eff = effect_of(ev, commodities, instruments, delays, last_prices, rules_only)
        s = eff.severity
        if model is not None:
            e = model.event_exposure(pe.sites, ev.node_keys)
            on_path = e > 0
            timing = _event_delay(model, pe, overrides)
        else:
            e, on_path = 1.0, bool(ev.node_keys)
            timing = None
        disrupting = pe.event_type in DISRUPTING and pe.event_status in LIVE_STATUSES
        s_perso = s
        if model is not None and on_path and ev.confirmed and disrupting:
            s_perso = max(s, 3 if e >= HIGH_SHARE else 2 if e >= WEIGHS_SHARE else s)
        if timing is not None:
            x, xl, xh, buffers, lead_days = timing
        else:
            x = eff.x_weeks * 7 if eff.x_weeks is not None else UNKNOWN_X_WEEKS * 7
            xl = (eff.x_low or eff.x_weeks or UNKNOWN_X_WEEKS) * 7
            xh = (eff.x_high or eff.x_weeks or UNKNOWN_X_WEEKS) * 7
            buffers, lead_days = 0.0, None
        lead_w, assumed = _lead_weeks(lead_days, coverage)
        window = x / 7 - lead_w
        absorbed = eff.duration_high < buffers
        physical = on_path and s_perso >= 2 and not absorbed
        price = not rules_only and eff.p_up is not None and eff.rise_pct is not None and eff.rise_pct >= thr
        backed = ev.confirmed or ev.best_lineage >= 0.7
        exposures[ev.id] = {"exposure_pct": round(100 * e, 1), "s": s, "s_perso": s_perso, "on_path": on_path,
                            "x_weeks": round(x / 7, 1) if on_path else None, "sites": pe.sites,
                            "title": pe.title, "public": pe.public}
        if on_path:
            touched_sites |= set(pe.sites)
        retained = None
        if ev.confirmed and ((physical and e >= WEIGHS_SHARE and window <= 2) or (price and (eff.p_up or 0) >= 0.5)):
            rule = "physique" if physical and e >= WEIGHS_SHARE and window <= 2 else "prix"
            retained = (anticipate, rule)
        elif backed and ((physical and e > 0) or (price and (eff.p_up or 0) >= 0.25)):
            retained = (potential, "physique" if physical and e > 0 else "prix")
        if retained is not None:
            target, rule = retained
            if rule == "prix":
                x = xl = xh = PRICE_ONLY_X_WEEKS * 7
                window = PRICE_ONLY_X_WEEKS - lead_w
            target.append(Retained(ev.id, rule, x / 7, xl / 7, xh / 7, window, assumed, e, s, s_perso, pe.sites,
                                   pe.title))
        pressure = max(pressure, _event_pressure(ev, s_perso, eff, thr, rules_only))
        if pressure <= 0 and ev.confirmed:
            if eff.loosening >= 3 or (not rules_only and (eff.p_down or 0) >= 0.5 and (eff.fall_pct or 0) >= thr):
                pressure = min(pressure, -2)
            elif eff.loosening >= 2 or (not rules_only and (eff.p_down or 0) >= 0.25 and (eff.fall_pct or 0) >= thr):
                pressure = min(pressure, -1)

    used = []
    for sig in live:
        result = _signal(sig, model, overrides, coverage, thr, today)
        if result is None:
            continue
        used.append(sig["id"])
        target_list, retained_signal, sig_pressure = result
        (anticipate if target_list == ANTICIPATE else potential).append(retained_signal)
        exposures[sig["id"]] = {"exposure_pct": round(100 * retained_signal.exposure, 1), "s": 0,
                                "s_perso": retained_signal.s_perso, "on_path": True,
                                "x_weeks": round(retained_signal.x, 1), "sites": retained_signal.sites,
                                "title": retained_signal.reason, "public": False}
        touched_sites |= set(retained_signal.sites)
        pressure = max(pressure, sig_pressure)

    chosen = anticipate or potential
    verdict = ANTICIPATE if anticipate else POTENTIAL if potential else RAS
    if verdict == RAS and not verified:
        verdict = UNVERIFIED
    first = min(chosen, key=lambda r: r.x) if chosen else None
    v = Verdict(verdict=verdict, pressure=pressure, x_weeks=round(first.x, 1) if first else None,
                x_low=round(first.x_low, 1) if first else None, x_high=round(first.x_high, 1) if first else None,
                window_weeks=round(first.window, 1) if first else None,
                window_assumed=first.window_assumed if first else True, rules_only=rules_only,
                event_ids=[r.id for r in chosen])
    v.explanation = explain(v, chosen, model, coverage)
    plan_b = model.plan_b(touched_sites) if model is not None and chosen else []
    return Personal(v, chosen, exposures, plan_b, thr, revision, used)


def _event_delay(model: Model, pe: PersonalEvent, overrides: dict[str, float]
                 ) -> tuple[float, float, float, float, float | None] | None:
    """Earliest arrival, in days, of an event at the user's direct supplier: (x, low, high, buffers, lead to user)."""
    best: tuple[float, float, float, float, float | None] | None = None
    touched = set(pe.sites) | {s.id for s in model.doc.sites if s.node_key and s.node_key in set(pe.event.node_keys)}
    for sid in touched:
        d = model.private_delay(sid, overrides)
        if d is not None and (best is None or d["days"] < best[0]):
            best = (d["days"], d["days"], d["days"], d["buffers"], d["lead_days"])
    for site in model.doc.sites:
        if model.unexplained(site.id) <= 0:
            continue
        for node in pe.event.node_keys:
            if node not in model.nodes or model.chain_fraction(site, frozenset({node})) <= 0:
                continue
            upstream = model.node_delay(node, site)
            own = model.private_delay(site.id, overrides)
            if upstream is None or own is None:
                continue
            stock = site.stock_days or 0.0
            x = upstream["days"] + stock + own["days"]
            if best is None or x < best[0]:
                best = (x, upstream["low"] + stock + own["days"], upstream["high"] + stock + own["days"],
                        upstream.get("buffers", 0.0) + stock + own["buffers"], own["lead_days"])
    return best


def _event_pressure(ev: EventInput, s_perso: int, eff: Any, thr: float, rules_only: bool) -> int:
    def up(p: float) -> bool:
        return not rules_only and (eff.p_up or 0) >= p and (eff.rise_pct or 0) >= thr

    if (ev.confirmed and s_perso >= 3) or up(0.5):
        return 2
    if ((ev.confirmed or ev.best_lineage >= 0.7) and s_perso >= 2) or up(0.25):
        return 1
    return 0


def _signal(sig: dict[str, Any], model: Model | None, overrides: dict[str, float], coverage: dict[str, Any] | None,
            thr: float, today: date) -> tuple[str, Retained, int] | None:
    """A signal as a private confirmed event (reliability 0.9, no corroboration needed)."""
    site = sig.get("site_id")
    label = SIGNAL_TYPES.get(sig["type"], sig["type"])
    if sig["type"] == "price_increase":
        pct = float(sig.get("pct") or 0)
        if pct < thr:
            return None
        effective = date.fromisoformat(sig.get("effective_date") or sig["date"])
        x = max(0.0, (effective - today).days / 7)
        lead_w, assumed = _lead_weeks(None, coverage)
        reason = f"Signal fournisseur : {label} de {pct:g} % au {effective.isoformat()}"
        return ANTICIPATE, Retained(sig["id"], "prix", x, x, x, x - lead_w, assumed, 1.0, 0, 0,
                                    [site] if site else [], reason), 2
    if sig["type"] not in ("force_majeure", "rupture", "allocation", "delay"):
        return None
    floor = 3 if sig["type"] in ("force_majeure", "rupture") else 2
    if model is not None and site in model.sites:
        e = model.exposure.get(site, 0.0)
        d = model.private_delay(site, overrides)
        x_days = d["days"] if d else 0.0
        lead_days = d["lead_days"] if d else None
    else:
        e, x_days, lead_days = 1.0, 0.0, None
    if e <= 0:
        return None
    lead_w, assumed = _lead_weeks(lead_days, coverage)
    s_perso = max(floor, 3 if e >= HIGH_SHARE else 2 if e >= WEIGHS_SHARE else 0)
    window = x_days / 7 - lead_w
    target = ANTICIPATE if s_perso >= 2 and e >= WEIGHS_SHARE and window <= 2 else POTENTIAL
    name = model.sites[site].name if model is not None and site in model.sites else "ton fournisseur"
    reason = f"Signal fournisseur : {label} chez {name}"
    return target, Retained(sig["id"], "physique", x_days / 7, x_days / 7, x_days / 7, window, assumed, e, 0,
                            s_perso, [site] if site else [], reason), 2 if s_perso >= 3 else 1


def explain(v: Verdict, retained: list[Retained], model: Model | None, coverage: dict[str, Any] | None) -> str:
    """French explanation written by the code; it names the user's sites, so it stays in the vault."""
    if v.verdict == UNVERIFIED:
        return "Aucun cycle de veille réussi depuis plus de 12 heures : rien ne garantit qu'il n'y a rien à signaler."
    if v.verdict == RAS:
        return "Aucun événement ni signal ne menace tes chemins d'approvisionnement."
    parts = []
    for r in sorted(retained, key=lambda r: r.x)[:3]:
        names = [model.sites[s].name for s in r.sites if model is not None and s in model.sites]
        where = f" ({', '.join(names)})" if names else ""
        share = f", {100 * r.exposure:.0f} % de ton volume" if r.rule == "physique" else ""
        parts.append(f"{r.reason}{where}{share}")
    text = " ; ".join(parts) + "."
    if v.x_low is not None and v.x_high is not None:
        x_text = (f"{v.x_low:.0f} semaine{'s' if v.x_low >= 2 else ''}" if round(v.x_low) == round(v.x_high)
                  else f"{v.x_low:.0f} à {v.x_high:.0f} semaines")
        text += f" Délai avant impact chez ton fournisseur : {x_text}."
    if v.window_weeks is not None:
        if v.window_weeks < 0:
            text += " Fenêtre d'action dépassée : le délai de commande est plus long que le délai avant impact."
        else:
            text += f" Fenêtre d'action : {v.window_weeks:.0f} semaine{'s' if v.window_weeks >= 2 else ''}" + (
                " (délai fournisseur supposé de 4 semaines)." if v.window_assumed else ".")
    stock = (coverage or {}).get("stock_weeks")
    if stock is not None:
        text += f" Ton stock : {stock:g} semaine{'s' if stock >= 2 else ''}."
    if v.rules_only:
        text += " Estimation sans IA : plafond de veille atteint."
    return text
