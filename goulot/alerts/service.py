"""Alerts (docs/SPEC.md, section 9): what triggers them, how they are worded, grouped and sent.

Triggers: a verdict change; a new confirmed event of severity hint ≥ 3; AI spending at 80 % and 100 % of the watch
cap (with the offer to raise it when a severity-3 event is under way); a source silent for 24 hours; a watch not
verified for 12 hours; a security event. At most one alert per material and per hour, except « Anticiper stock ».
During the break-in, verdict alerts end with « (rodage) ». « Alertes discrètes »: no material name at all.
Texts are written by the code; titles that come from articles are clipped and neutralized; no link, no private data.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from goulot.alerts.defang import defang
from goulot.collect.runner import MISSING_KEY
from goulot.db.connection import Database, get_setting
from goulot.llm.budget import CRISIS, ECONOMY, RULES_ONLY, WatchMode

log = logging.getLogger("goulot.alerts")

THROTTLE = timedelta(hours=1)
SILENT_LIMIT = timedelta(hours=24)
UNVERIFIED_LIMIT = timedelta(hours=12)
TITLE_CHARS = 120
SECURITY_KINDS = {
    "unlock_blocked": "Ouverture du coffre bloquée après des échecs répétés.",
    "passkey_registered": "Une nouvelle clé d'accès a été enregistrée sur le coffre.",
    "passkey_enrollment_refused": "Tentative d'enregistrement d'une clé d'accès refusée.",
    "llm_circuit_open": "Appels à l'IA mis en pause une heure (disjoncteur).",
}


@dataclass
class Alert:
    kind: str
    dedupe_key: str
    text: str
    material_id: str | None = None
    urgent: bool = False


Sender = Callable[[str], None]


def _name(conn: sqlite3.Connection, material_id: str) -> str:
    row = conn.execute("SELECT name FROM materials WHERE id = ?", (material_id,)).fetchone()
    return str(row[0]) if row else "une matière"


def verdict_alerts(conn: sqlite3.Connection, changed: Sequence[tuple[str, object]], at: datetime) -> list[Alert]:
    out = []
    for material_id, v in changed:
        verdict = getattr(v, "verdict", "")
        previous = conn.execute(
            "SELECT verdict FROM verdicts WHERE material_id = ? AND scope = 'market' AND current = 0 "
            "ORDER BY created_at DESC LIMIT 1", (material_id,)).fetchone()
        if previous is None and verdict == "ras":
            continue  # a first « Rien à signaler » is not news
        row = conn.execute("SELECT break_in FROM verdicts WHERE material_id = ? AND scope = 'market' "
                           "AND current = 1", (material_id,)).fetchone()
        text = f"Goulot · {_name(conn, material_id)} : {getattr(v, 'label', verdict)}."
        window = getattr(v, "window_weeks", None)
        if verdict in ("ant", "pot") and window is not None:
            text += f" Fenêtre d'action : {max(window, 0):.0f} semaine{'s' if window >= 2 else ''}"
            text += " (délai fournisseur supposé)." if getattr(v, "window_assumed", True) else "."
        if verdict == "unverified":
            text = f"Goulot · {_name(conn, material_id)} : veille non vérifiée depuis plus de 12 heures."
        text += " Détail dans l'app."
        if row and row[0]:
            text += " (rodage)"
        out.append(Alert("verdict", f"verdict-{material_id}-{at:%Y%m%dT%H%M%S}", text, material_id,
                         urgent=verdict == "ant"))
    return out


def personal_alerts(conn: sqlite3.Connection, changed: Sequence[tuple[str, object]], at: datetime) -> list[Alert]:
    """A personal verdict is announced by the material, the verdict and the action window, with « motif dans
    l'app »: never X (with the window it would reveal the contractual lead time), never a supplier, share, stock,
    price or signal (docs/SPEC.md, section 9)."""
    out = []
    for material_id, p in changed:
        v = getattr(p, "verdict", None)
        verdict = getattr(v, "verdict", "")
        previous = conn.execute(
            "SELECT verdict FROM verdicts WHERE material_id = ? AND scope = 'personal' AND current = 0 "
            "ORDER BY created_at DESC LIMIT 1", (material_id,)).fetchone()
        if (previous is None and verdict == "ras") or verdict == "unverified":
            continue  # a first calm is not news; « non vérifié » is already sent for the market verdict
        label = {"ant": "Anticiper stock", "pot": "Problème potentiel", "ras": "Rien à signaler"}[verdict]
        text = f"Goulot · {_name(conn, material_id)} : {label} (pour ton approvisionnement)."
        window = getattr(v, "window_weeks", None)
        if verdict in ("ant", "pot") and window is not None:
            text += (" Fenêtre d'action dépassée." if window < 0 else
                     f" Fenêtre d'action : {window:.0f} semaine{'s' if window >= 2 else ''}.")
        text += " Motif dans l'app."
        row = conn.execute("SELECT break_in FROM verdicts WHERE material_id = ? AND scope = 'personal' "
                           "AND current = 1", (material_id,)).fetchone()
        if row and row[0]:
            text += " (rodage)"
        out.append(Alert("verdict", f"personal-{material_id}-{at:%Y%m%dT%H%M%S}", text, material_id,
                         urgent=verdict == "ant"))
    return out


def reminder_alerts(expired_signals: int, at: datetime) -> list[Alert]:
    """Supplier signals at 90 days: a reminder without material, site or signal type."""
    if expired_signals <= 0:
        return []
    return [Alert("reminder", f"reminder-signals-{at:%G-W%V}",
                  "Goulot : un rappel t'attend dans l'app (coffre). Confirme ou clos ce qui doit l'être.")]


def event_alerts(conn: sqlite3.Connection) -> list[Alert]:
    out = []
    for e in conn.execute("SELECT id, material_id, title, place FROM public_events WHERE confirmation = 'confirmed' "
                          "AND severity_hint >= 3 AND origin IN ('news', 'policy', 'hazard', 'logistics')"):
        # public events only: an event of Ma chaîne acts through the verdict, never alone
        title = defang(str(e["title"]))[:TITLE_CHARS]
        place = f" ({defang(str(e['place']))[:60]})" if e["place"] and e["place"] != "unknown" else ""
        out.append(Alert("event", f"event-{e['id']}", f"Goulot · {_name(conn, e['material_id'])} : nouvel "
                         f"événement confirmé, {title}{place}. Détail dans l'app.", e["material_id"]))
    return out


def budget_alerts(conn: sqlite3.Connection, mode: WatchMode, at: datetime, serious_ongoing: bool,
                  headroom: float) -> list[Alert]:
    month = at.strftime("%Y-%m")
    out = []
    if mode.mode in (ECONOMY, CRISIS):
        out.append(Alert("budget", f"budget-80-{month}", f"Goulot : 80 % du plafond de veille atteint "
                         f"({mode.spent:.2f} $ sur {mode.cap:.2f} $). Mode économie : seuls les événements "
                         "corroborés sont analysés."))
    if mode.mode == RULES_ONLY:
        text = (f"Goulot : plafond de veille atteint ({mode.spent:.2f} $ sur {mode.cap:.2f} $). La veille continue "
                "par règles seules, sans IA.")
        if serious_ongoing and headroom > 0:
            text += (f" Un événement grave est en cours : tu peux relever le plafond de ce mois dans l'app "
                     f"(jusqu'à {headroom:.2f} $ de plus).")
        out.append(Alert("budget", f"budget-100-{month}", text))
    return out


def health_alerts(conn: sqlite3.Connection, at: datetime) -> list[Alert]:
    out = []
    day = at.strftime("%Y-%m-%d")
    for r in conn.execute("SELECT source, MAX(CASE WHEN ok = 1 THEN started_at END) AS last_ok, MAX(started_at) "
                          "AS last_run FROM collect_runs GROUP BY source"):
        silent = r["last_ok"] is None or at - datetime.fromisoformat(r["last_ok"]) > SILENT_LIMIT
        awake = bool(r["last_run"]) and at - datetime.fromisoformat(r["last_run"]) < timedelta(hours=6)
        last = conn.execute("SELECT error FROM collect_runs WHERE source = ? ORDER BY id DESC LIMIT 1",
                            (r["source"],)).fetchone()
        if last is not None and last[0].endswith(MISSING_KEY):
            # Not a silent source: one that was never given its key. Said once, not every day.
            out.append(Alert("missing_key", f"missing-key-{r['source']}", f"Goulot : la source {r['source']} "
                             "attend sa clé (« ./bin/goulot setup ») ; elle reste inactive en attendant."))
            continue
        if silent and awake:
            out.append(Alert("silent_source", f"silent-{r['source']}-{day}",
                             f"Goulot : la source {r['source']} ne répond plus depuis plus de 24 heures."))
    last_ok = conn.execute("SELECT MAX(started_at) FROM cycles WHERE ok = 1").fetchone()[0]
    last_run = conn.execute("SELECT MAX(started_at) FROM cycles").fetchone()[0]
    if last_run and (last_ok is None or at - datetime.fromisoformat(last_ok) > UNVERIFIED_LIMIT):
        out.append(Alert("unverified", f"unverified-{day}", "Goulot : veille non vérifiée depuis plus de 12 heures "
                         "alors que le Mac est allumé. « Rien à signaler » devient « Non vérifié »."))
    return out


def security_alerts(conn: sqlite3.Connection, since_id: int) -> list[Alert]:
    return [Alert("security", f"security-{r['id']}", f"Goulot, sécurité : {SECURITY_KINDS[r['kind']]}", urgent=True)
            for r in conn.execute("SELECT id, kind FROM audit_log WHERE id > ? ORDER BY id", (since_id,))
            if r["kind"] in SECURITY_KINDS]


def dispatch(db: Database, alerts: list[Alert], sender: Sender | None, at: datetime) -> int:
    """Record and send new alerts (once per dedupe key), at most one per material per hour unless urgent."""
    sent = 0
    with db.transaction() as conn:
        discreet = get_setting(conn, "alerts_discreet") == "1"
    for alert in alerts:
        with db.transaction() as conn:
            if conn.execute("SELECT 1 FROM alerts WHERE dedupe_key = ?", (alert.dedupe_key,)).fetchone():
                continue
            if alert.material_id and not alert.urgent:
                recent = conn.execute("SELECT 1 FROM alerts WHERE material_id = ? AND created_at >= ? AND sent_at "
                                      "IS NOT NULL", (alert.material_id, (at - THROTTLE).isoformat())).fetchone()
                if recent:
                    continue  # grouped: the app shows it; the next alert will carry the latest state
            text = "Goulot : un verdict a changé, ouvre l'app." if discreet and alert.material_id else alert.text
            text = defang(text)
            conn.execute("INSERT INTO alerts (kind, material_id, dedupe_key, text, created_at) VALUES (?, ?, ?, ?, ?)",
                         (alert.kind, alert.material_id, alert.dedupe_key, text, at.isoformat()))
        error = ""
        if sender is not None:
            try:
                sender(text)
            except Exception as exc:
                error = type(exc).__name__
                log.warning("alert not sent: %s", error)
        with db.transaction() as conn:
            conn.execute("UPDATE alerts SET sent_at = ?, error = ? WHERE dedupe_key = ?",
                         (at.isoformat() if sender is not None and not error else None, error, alert.dedupe_key))
        sent += int(sender is not None and not error)
    return sent
