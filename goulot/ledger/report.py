"""What the Fiabilité screen reads from the ledger (docs/SPEC.md, section 7): each advice put in words (what was
announced, its window, what was observed), the calibration of the announced probabilities, what Goulot knew on the
day of an advice, and the public export of the history. Read only: nothing here writes to the ledger, and only
market entries are read (`chain.entries` leaves the personal scope out).
"""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import date, timedelta
from typing import Any

from goulot.ledger import chain, outcomes, rules
from goulot.ledger.rules import Series

ADVICE_KINDS = ("verdict", "hypothesis", "pressure", "missed_move")
GOOD = {"confirmed", "correct_calm", "range_hit", "right_direction"}
SHOW_BRIER = 30  # resolved hypotheses before the Brier score is shown
SHOW_DIAGRAM = 50  # before the calibration diagram is shown
MONTHS = ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc.")
VERDICT_WORDS = {"ant": "Anticiper stock", "pot": "Problème potentiel", "ras": "Rien à signaler",
                 "unverified": "Non vérifié"}


def fr_day(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]}"


def fr_pct(value: float, digits: int = 1) -> str:
    text = f"{value:+.{digits}f}".replace(".", ",")
    return text[:-2] if digits == 1 and text.endswith(",0") else text


def _num(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def hashes(conn: sqlite3.Connection) -> dict[int, tuple[str, str]]:
    return {r[0]: (r[1], r[2]) for r in conn.execute("SELECT seq, prev_hash, hash FROM ledger")}


def version_label(entry: dict[str, Any]) -> str:
    """« modèle · prompt · schéma » of a hypothesis; for a verdict, those of the analyses behind it."""
    v = entry.get("versions") or {}
    if "model" in v:
        return f"{v['model']} · {v['prompt']} · {v['schema']}"
    return " + ".join(s.replace("|", " · ") for s in v.get("analyses", []))


def _deadline(entry: dict[str, Any], issued: date, limits: dict[str, float],
              series: Series | None) -> tuple[date | None, bool]:
    """(deadline, estimated): the end of the window the outcome rules look at."""
    kind = entry["kind"]
    if kind == "verdict":
        if entry.get("verdict") in ("ant", "pot"):
            return issued + timedelta(weeks=max(entry.get("x_weeks") or 4.0, 4.0) + 2), False
        if entry.get("verdict") == "ras":
            return issued + rules.CALM_WINDOW, False
        return None, False
    sessions = rules.SESSIONS.get(entry.get("horizon", ""), rules.PRESSURE_SESSIONS)
    point = series.nth_after(issued, sessions) if series is not None and series.frequency == "daily" else None
    if point is not None:
        return point[0], False
    return issued + timedelta(days=round(sessions * 7 / 5)), True


def announced(entry: dict[str, Any], limits: dict[str, float], deadline: date | None) -> str:
    kind = entry["kind"]
    thr, mat = _num(limits["threshold_pct"]), _num(limits["materiality_pct"])
    if kind == "verdict":
        if entry.get("verdict") in ("ant", "pot") and deadline is not None:
            return f"hausse de {thr} % avant le {fr_day(deadline)}"
        if entry.get("verdict") == "ras":
            return f"pas de hausse de {thr} % sous 8 semaines"
        return "aucun conseil (veille non vérifiée)"
    if kind == "hypothesis":
        sessions = rules.SESSIONS.get(entry["horizon"], 20)
        unit = " %" if entry.get("move_unit", "percent") == "percent" else ""
        return (f"{fr_pct(entry['move_p10'])}{unit} à {fr_pct(entry['move_p90'])}{unit} sous {sessions} "
                f"séance{'s' if sessions > 1 else ''}, "
                f"probabilité {round(100 * entry['probability'])} %")
    if kind == "pressure":
        word = "hausse" if entry["pressure"] > 0 else "baisse"
        return f"pression à la {word} : au moins {mat} % sous 20 séances"
    return f"hausse de {thr} % non signalée"


def observed(entry: dict[str, Any], outcome: dict[str, Any] | None, issued: date) -> str:
    if entry["kind"] == "missed_move":
        return f"seuil franchi le {fr_day(date.fromisoformat(entry['day']))}"
    if outcome is None:
        return "en cours"
    pct, result = outcome.get("observed_pct"), outcome["result"]
    if result == rules.UNRESOLVED:
        return outcome.get("detail") or "rien d'observable"
    if pct is None:
        return "aucun mouvement notable"
    if result == "confirmed" and outcome.get("lead_weeks") is not None:
        crossed = issued + timedelta(days=round(7 * outcome["lead_weeks"]))
        return f"{fr_pct(pct)} % franchi le {fr_day(crossed)}"
    if result in ("false_alarm", "correct_calm"):
        return f"au plus {fr_pct(pct)} %"
    return f"{fr_pct(pct)} %"


def _band_of(entry: dict[str, Any], hypotheses: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The 20-session range behind an advice: the hypothesis itself, or for a verdict the latest one of its events."""
    if entry["kind"] == "hypothesis":
        return entry if entry["horizon"] == "20d" else None
    if entry["kind"] != "verdict" or not entry.get("event_ids"):
        return None
    found = [h for h in hypotheses if h["event_id"] in entry["event_ids"] and h["horizon"] == "20d"
             and h["at"] <= entry["at"] and h.get("move_unit", "percent") == "percent"]
    return found[-1] if found else None


def rows(conn: sqlite3.Connection, material_id: str | None, series_for: Any) -> list[dict[str, Any]]:
    """Every market advice of the ledger, in words, oldest first. `series_for(material_id)`: its best series."""
    entries = chain.entries(conn, material_id=material_id, kinds=(*ADVICE_KINDS, "outcome"))
    outcome_of = {e["of_seq"]: e for e in entries if e["kind"] == "outcome"}
    hypotheses = [e for e in entries if e["kind"] == "hypothesis"]
    links = hashes(conn)
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM materials")}
    series_cache: dict[str, Series | None] = {}
    out = []
    for e in entries:
        if e["kind"] == "outcome":
            continue
        m = e["material_id"] or ""
        if m not in series_cache:
            series_cache[m] = series_for(m) if m in names else None
        issued = date.fromisoformat(e["at"][:10])
        limits = outcomes.thresholds_at(conn, e["at"])
        deadline, estimated = (None, False) if e["kind"] == "missed_move" else _deadline(
            e, issued, limits, series_cache[m])
        outcome = outcome_of.get(e["seq"])
        band = _band_of(e, hypotheses)
        band_outcome = outcome_of.get(band["seq"]) if band else None
        prev_hash, own_hash = links[e["seq"]]
        out.append({
            **{k: v for k, v in e.items() if k not in ("inputs_digest",)},
            "outcome": outcome,
            "material_name": names.get(m, "matière supprimée") if m else "",
            "material_deleted": bool(m) and m not in names,
            "announced": announced(e, limits, deadline),
            "deadline": deadline.isoformat() if deadline else None,
            "deadline_estimated": estimated,
            "observed": observed(e, outcome, issued),
            "good": None if outcome is None or outcome["result"] == rules.UNRESOLVED else outcome["result"] in GOOD,
            "band": None if band is None else {
                "seq": band["seq"], "p10": band["move_p10"], "p90": band["move_p90"],
                "probability": band["probability"], "start": band["at"][:10],
                "observed_pct": band_outcome.get("observed_pct") if band_outcome else None,
                "hit": None if band_outcome is None or band_outcome["result"] == rules.UNRESOLVED
                else band_outcome["result"] == "range_hit"},
            "version": version_label(e),
            "hash": own_hash, "prev_hash": prev_hash,
        })
    return out


def version_changes(advices: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The first day each new model, prompt or schema version gave a hypothesis (none for the first version)."""
    seen: set[str] = set()
    out = []
    for a in advices:
        if a["kind"] != "hypothesis" or not a["version"]:
            continue
        if a["version"] not in seen:
            if seen:
                out.append({"day": a["at"][:10], "version": a["version"]})
            seen.add(a["version"])
    return out


def calibration(conn: sqlite3.Connection, material_id: str | None) -> dict[str, Any]:
    """Announced probabilities against what happened. The event a probability is about: a move beyond the
    materiality threshold in the announced direction (prompts/impact_v1.md), as the outcome rule found it."""
    hyps = {e["seq"]: e for e in chain.entries(conn, material_id=material_id, kinds=("hypothesis",))}
    pairs = [(float(hyps[o["of_seq"]]["probability"]), 1.0 if o.get("detail") == "sens juste" else 0.0)
             for o in chain.entries(conn, material_id=material_id, kinds=("outcome",))
             if o["of_seq"] in hyps and o["result"] in ("range_hit", "range_missed")]
    n = len(pairs)
    out: dict[str, Any] = {"resolved": n, "show_brier": n >= SHOW_BRIER, "show_diagram": n >= SHOW_DIAGRAM,
                           "brier": None, "base_brier": None, "base_rate": None, "bins": [], "verdict": ""}
    if n == 0:
        out["verdict"] = f"Aucune hypothèse résolue : le score s'affiche à partir de {SHOW_BRIER}."
        return out
    rate = sum(y for _, y in pairs) / n
    brier = sum((p - y) ** 2 for p, y in pairs) / n
    base = sum((rate - y) ** 2 for _, y in pairs) / n
    bins: dict[int, list[tuple[float, float]]] = {}
    for p, y in pairs:
        bins.setdefault(min(int(p * 10), 9), []).append((p, y))
    out.update({
        "brier": round(brier, 3), "base_brier": round(base, 3), "base_rate": round(rate, 3),
        "bins": [{"p": round(sum(p for p, _ in v) / len(v), 3), "observed": round(sum(y for _, y in v) / len(v), 3),
                  "n": len(v)} for _, v in sorted(bins.items())],
    })
    if n < SHOW_BRIER:
        out["verdict"] = f"{n} hypothèses résolues : le score s'affiche à partir de {SHOW_BRIER}."
        return out
    bias = sum(p for p, _ in pairs) / n - rate
    lean = (", avec une tendance à surestimer" if bias > 0.05 else ", avec une tendance à sous-estimer"
            if bias < -0.05 else "")
    if brier < base - 0.005:
        text = f"Mieux que le taux de base : les probabilités annoncées apportent de l'information{lean}."
    elif brier <= base + 0.005:
        text = f"Pas mieux que le taux de base : les probabilités annoncées n'apportent pas encore d'information{lean}."
    else:
        text = f"Moins bien que le taux de base : annoncer toujours la fréquence moyenne ferait mieux{lean}."
    if n < SHOW_DIAGRAM:
        text += f" Diagramme à partir de {SHOW_DIAGRAM} hypothèses résolues ({n} pour l'instant)."
    out["verdict"] = text
    return out


def _event_ids(conn: sqlite3.Connection, entry: dict[str, Any]) -> list[str]:
    if entry["kind"] == "verdict":
        return list(entry.get("event_ids") or [])
    if entry["kind"] == "hypothesis":
        return [entry["event_id"]]
    if entry["kind"] == "pressure":
        of = chain.entries(conn, kinds=("verdict",))
        return next((list(v.get("event_ids") or []) for v in of if v["seq"] == entry.get("of_verdict_seq")), [])
    return []


def knew(conn: sqlite3.Connection, entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The public events behind an advice, with the sources Goulot had read by that day. Their texts are external
    data: shown as text."""
    out: list[dict[str, Any]] = []
    for event_id in _event_ids(conn, entry):
        e = conn.execute("SELECT id, title, place, first_seen_at, confirmation FROM public_events WHERE id = ?",
                         (event_id,)).fetchone()
        if e is None:
            out.append({"id": event_id, "title": "événement effacé depuis", "place": "", "first_seen": "",
                        "confirmed": None, "sources": []})
            continue
        sources = [{"title": r["title"], "url": r["url"], "publisher": r["publisher"]} for r in conn.execute(
            "SELECT a.title, a.url, COALESCE(NULLIF(a.provider_domain, ''), a.domain) AS publisher "
            "FROM event_articles ea JOIN articles a ON a.id = ea.article_id WHERE ea.event_id = ? "
            "AND a.first_seen_at <= ? ORDER BY a.first_seen_at LIMIT 8", (event_id, entry["at"]))]
        out.append({"id": e["id"], "title": e["title"], "place": e["place"], "first_seen": e["first_seen_at"][:10],
                    "confirmed": e["confirmation"] == "confirmed", "sources": sources})
    return out


CSV_COLUMNS = ("n", "date", "matiere", "type", "conseil", "annonce", "echeance", "constate", "resultat",
               "sur_indice", "versions", "empreinte", "empreinte_precedente")
RESULT_WORDS = {"confirmed": "confirmé", "false_alarm": "fausse alerte", "correct_calm": "calme juste",
                "missed_rise": "hausse manquée", "range_hit": "fourchette juste", "range_missed": "fourchette manquée",
                "right_direction": "bon sens", "wrong_direction": "mauvais sens", "no_effect": "sans effet",
                "unresolved": "non résolu"}
KIND_WORDS = {"verdict": "verdict", "hypothesis": "hypothèse de prix", "pressure": "pression de prix",
              "missed_move": "hausse non signalée"}


def _cell(value: Any) -> str:
    """A text a spreadsheet would run as a formula is neutralized."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "@", "\t", "\r") or (text[:1] in ("+", "-") and not text[1:2].isdigit()):
        return "'" + text
    return text


def advice_word(a: dict[str, Any]) -> str:
    if a["kind"] == "verdict":
        label = VERDICT_WORDS.get(a.get("verdict", ""), a.get("verdict", ""))
        return f"{label} à {_num(a['x_weeks'])} sem." if a.get("verdict") == "pot" and a.get("x_weeks") else label
    if a["kind"] == "hypothesis":
        return f"{a['instrument_id']} ({a['horizon']})"
    if a["kind"] == "pressure":
        return f"pression {'+' if a['pressure'] > 0 else ''}{a['pressure']}"
    return "hausse non signalée"


def export_csv(advices: list[dict[str, Any]]) -> str:
    """Public history: market advices only, with their hashes, so that anyone can check the chain."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(CSV_COLUMNS)
    for a in advices:
        o = a["outcome"]
        w.writerow([_cell(x) for x in (
            a["seq"], a["at"], a["material_name"], KIND_WORDS[a["kind"]], advice_word(a), a["announced"],
            a["deadline"] or "", a["observed"],
            RESULT_WORDS.get(o["result"], o["result"]) if o else ("—" if a["kind"] == "missed_move" else "en cours"),
            "oui" if o and o.get("on_index") else "", a["version"], a["hash"], a["prev_hash"])])
    return "﻿" + buf.getvalue()  # BOM: accents read right by a spreadsheet
