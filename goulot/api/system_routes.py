"""Routes of Settings > « Mises à jour » and « Bilan sécurité » (docs/SECURITY.md, « Page Bilan sécurité »;
docs/UPDATES.md). Public state only: no secret value, no private data."""

from __future__ import annotations

import re
import subprocess  # nosec B404 - fixed system tool, absolute path, no shell
import sys
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from goulot import __version__
from goulot.api.context import AppContext
from goulot.db.connection import get_setting, set_setting
from goulot.doctor import FAIL, OK, WARN, Check, check_filevault, check_firewall, check_ledger, check_listening
from goulot.llm.budget import watch_mode
from goulot.security.audit import audit
from goulot.security.secrets import SECRETS, has_secret
from goulot.settings import REPO_ROOT
from goulot.updater.state import Layout, load_state
from goulot.updater.trust import TRUSTED_KEYS

MODE_LABELS = {"auto": "automatique", "ask": "me demander", "off": "désactivé"}
STATUS = {OK: "ok", WARN: "warn", FAIL: "fail"}


class ModeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["auto", "ask", "off"]


def check_screen_lock() -> Check:
    try:
        out = subprocess.run(["/usr/sbin/sysadminctl", "-screenLock", "status"], capture_output=True,  # nosec B603
                             text=True, timeout=10, check=False)
        text = out.stdout + out.stderr
    except (OSError, subprocess.TimeoutExpired):
        text = ""
    if "immediate" in text:
        return Check(OK, "Verrouillage de l'écran", "mot de passe exigé immédiatement")
    if m := re.search(r"delay is (\d+) seconds", text):
        return Check(WARN, "Verrouillage de l'écran", f"mot de passe exigé après {m[1]} s : réglez « immédiatement » "
                     "dans Réglages Système > Écran verrouillé")
    if "off" in text:
        return Check(WARN, "Verrouillage de l'écran", "aucun mot de passe au réveil : activez-le dans Réglages "
                     "Système > Écran verrouillé")
    return Check(WARN, "Verrouillage de l'écran", "illisible : vérifiez Réglages Système > Écran verrouillé "
                 "(« immédiatement »)")


def update_mode(ctx: AppContext) -> str:
    with ctx.db.connect() as conn:
        mode = get_setting(conn, "update_mode", "auto") or "auto"
    return mode if mode in MODE_LABELS else "auto"


def updates_status(ctx: AppContext) -> dict[str, Any]:
    layout = Layout(ctx.settings.home)
    state = load_state(layout)
    installed = REPO_ROOT.resolve().parent == layout.releases.resolve()
    return {
        "version": __version__,
        "installed": installed,
        "mode": update_mode(ctx),
        "paused": state.paused,
        "highest_installed": state.highest_installed,
        "rejected": state.rejected,
        "available": state.available,
        "last_check": state.last_check,
        "last_result": state.last_result,
        "last_install": state.last_install,
        "versions": layout.installed(),
        "trusted_keys": [kid for kid, _ in TRUSTED_KEYS],
    }


def _update_checks(ctx: AppContext, now: datetime) -> list[Check]:
    s = updates_status(ctx)
    if not s["installed"]:
        return [Check(WARN, "Mises à jour", "copie de travail : pas encore installé (« ./bin/goulot dev "
                      "bootstrap »)"),
                Check(WARN, "Audit des dépendances", "fait par la CI à chaque push ; à chaque version une fois "
                      "installé")]
    checks = []
    last = s["last_check"]
    stale = last is None or now - datetime.fromisoformat(last) > timedelta(hours=48)
    when = last[:16].replace("T", " ") if last else "jamais"
    detail = f"mode {MODE_LABELS[s['mode']]} ; dernière vérification {when}"
    if s["paused"]:
        checks.append(Check(FAIL, "Mises à jour",
                            f"en pause après deux échecs : « goulot update --resume » ; {detail}"))
    elif s["mode"] == "off":
        checks.append(Check(WARN, "Mises à jour", f"désactivées ; {detail}"))
    elif stale:
        checks.append(Check(WARN, "Mises à jour", f"pas de vérification depuis 48 h ; {detail}"))
    else:
        checks.append(Check(OK, "Mises à jour", f"{detail} ({s['last_result'] or '—'})"))
    install = s["last_install"]
    if install:
        checks.append(Check(OK, "Signature de la version",
                            f"{install['version']} installée le {install['at'][:10]}, signature vérifiée (clé "
                            f"{install['key_id']})"))
        checks.append(Check(OK, "Audit des dépendances",
                            f"pip-audit et npm audit sans alerte à la publication de {install['version']}"))
    else:
        checks.append(Check(WARN, "Signature de la version", "aucune installation enregistrée"))
    return checks


def security_report(ctx: AppContext) -> list[Check]:
    now = datetime.now(UTC)
    checks: list[Check] = []
    if sys.platform == "darwin":
        checks += [check_filevault(), check_firewall(), check_screen_lock()]
    checks.append(check_listening(ctx.settings))
    configured = [s for s in SECRETS if has_secret(s.name)]
    anthropic = has_secret("anthropic")
    checks.append(Check(OK if anthropic else WARN, "Secrets du trousseau",
                        f"{len(configured)} configuré(s), clé Anthropic {'présente' if anthropic else 'absente'}"))
    v = ctx.vault
    passkeys = ctx.passkeys.count()
    if not v.is_configured():
        checks.append(Check(WARN, "Coffre", "non configuré : « goulot setup »"))
    else:
        problems = ([] if v.recovery_confirmed() else ["clé de secours non confirmée"]) + (
            [] if passkeys else ["aucune clé d'accès"])
        detail = f"{passkeys} clé(s) d'accès, fermeture après {int(ctx.vault_tokens.sliding_ttl() // 60)} min"
        checks.append(Check(WARN if problems else OK, "Coffre", " ; ".join([*problems, detail])))
    checks.append(check_ledger(ctx.db))
    checks += _update_checks(ctx, now)
    mode = watch_mode(ctx.budget)
    share = mode.spent / mode.cap if mode.cap else 0
    checks.append(Check(OK if share < 0.8 else WARN, "Dépense IA du mois",
                        f"{mode.spent:.2f} $ sur {mode.cap:.2f} $ ({mode.mode})"))
    checks.append(Check(WARN, "Jeton GitHub", "présent : vérifiez sa date d'expiration sur github.com")
                  if has_secret("github_token") else Check(OK, "Jeton GitHub", "aucun (dépôt public)"))
    return checks


def build_router(ctx: AppContext) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/updates")
    def updates() -> dict[str, Any]:
        return updates_status(ctx)

    @router.put("/settings/update-mode")
    def set_update_mode(body: ModeBody) -> dict[str, str]:
        with ctx.db.transaction() as conn:
            set_setting(conn, "update_mode", body.mode)
            audit(conn, "update_mode_changed", mode=body.mode)
        return {"mode": body.mode}

    @router.get("/security/report")
    def report() -> dict[str, Any]:
        return {"items": [{"status": STATUS[c.status], "label": c.label, "detail": c.detail}
                          for c in security_report(ctx)]}

    return router
