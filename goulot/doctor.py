"""`goulot doctor`: health of the installation, in French, green / orange / red.

Reads no private data: encrypted fields are decrypted to prove they still can be, and nothing is kept.
"""

from __future__ import annotations

import ipaddress
import platform
import re
import stat
import subprocess  # nosec B404 - fixed system tools, absolute paths, no shell
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from goulot.db.backup import last_backup_age
from goulot.db.connection import Database
from goulot.net.allowlist import load_allowlist
from goulot.private import coverage
from goulot.security.clock import SystemClock
from goulot.security.fs import DIR_MODE, FILE_MODE
from goulot.security.secrets import has_secret
from goulot.settings import Settings, validate_bind_hosts
from goulot.vault.keys import Vault, VaultError
from goulot.vault.passkeys import PasskeyService

OK, WARN, FAIL = "OK", "ATTENTION", "ÉCHEC"


@dataclass(frozen=True)
class Check:
    status: str
    label: str
    detail: str


def _run(args: list[str]) -> str:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)  # noqa: S603  # nosec B603
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout + out.stderr


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def check_permissions(settings: Settings) -> Check:
    bad: list[str] = []
    for d in (settings.home, settings.data_dir, settings.backup_dir, settings.log_dir):
        if d.exists() and _mode(d) != DIR_MODE:
            bad.append(f"{d.name} ({oct(_mode(d))})")
    for d in (settings.data_dir, settings.backup_dir, settings.log_dir):
        if d.exists():
            for f in d.iterdir():
                if f.is_file() and _mode(f) != FILE_MODE:
                    bad.append(f"{f.name} ({oct(_mode(f))})")
    if bad:
        return Check(FAIL, "Droits des fichiers", "à corriger : " + ", ".join(bad[:5]))
    return Check(OK, "Droits des fichiers", "dossiers 0700, fichiers 0600")


def check_listening(settings: Settings) -> Check:
    out = _run(["/usr/sbin/lsof", "-nP", f"-iTCP:{settings.port}", "-sTCP:LISTEN"])
    addresses = re.findall(r"TCP (\S+):\d+ \(LISTEN\)", out)
    if not addresses:
        return Check(OK, "Écoute réseau", f"serveur arrêté (port {settings.port} libre)")
    for addr in addresses:
        host = addr.strip("[]")
        if host == "*" or not ipaddress.ip_address(host).is_loopback:
            return Check(FAIL, "Écoute réseau", f"le port {settings.port} écoute sur {addr} : arrêtez ce programme")
    return Check(OK, "Écoute réseau", "interface de bouclage uniquement : " + ", ".join(sorted(set(addresses))))


def check_filevault() -> Check:
    out = _run(["/usr/bin/fdesetup", "status"])
    if "FileVault is On" in out:
        return Check(OK, "FileVault", "activé")
    if "FileVault is Off" in out:
        return Check(WARN, "FileVault", "désactivé : activez-le dans Réglages Système > Confidentialité et sécurité")
    return Check(WARN, "FileVault", "état illisible (lancez « ./bin/goulot doctor » dans votre Terminal)")


def check_firewall() -> Check:
    out = _run(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"])
    if "enabled" in out.lower() and "disabled" not in out.lower():
        return Check(OK, "Pare-feu macOS", "activé")
    if out:
        return Check(WARN, "Pare-feu macOS", "désactivé : activez-le dans Réglages Système > Réseau > Coupe-feu")
    return Check(WARN, "Pare-feu macOS", "état illisible")


def run_checks(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    ok_py = sys.version_info >= (3, 14)
    checks.append(Check(OK if ok_py else FAIL, "Python", platform.python_version()))
    machine = platform.machine()
    mac = platform.mac_ver()[0] or "inconnu"
    checks.append(Check(OK, "Mac", f"macOS {mac}, processeur {machine}"))
    try:
        validate_bind_hosts(settings.bind_hosts)
        checks.append(Check(OK, "Adresse d'écoute", ", ".join(settings.bind_hosts) + f", port {settings.port}"))
    except ValueError as exc:
        checks.append(Check(FAIL, "Adresse d'écoute", str(exc)))
    try:
        allowlist = load_allowlist(settings.config_dir)
        checks.append(Check(OK, "Liste blanche", f"{len(allowlist.hosts)} hôtes, tous en HTTPS"))
    except (OSError, ValueError, KeyError) as exc:
        checks.append(Check(FAIL, "Liste blanche", f"config/sources.yaml illisible : {exc}"))

    if not settings.db_path.exists():
        checks.append(Check(WARN, "Base", "absente : lancez « ./bin/goulot setup »"))
        checks.append(check_filevault())
        checks.append(check_firewall())
        return checks
    db = Database(settings.db_path)
    with db.connect() as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    pending = db.pending_migrations()
    if integrity != "ok":
        checks.append(Check(FAIL, "Base", f"intégrité : {integrity}"))
    elif pending:
        checks.append(Check(WARN, "Base", f"migrations en attente : {pending} (lancez « ./bin/goulot start »)"))
    else:
        checks.append(Check(OK, "Base", f"intègre, journal {journal.upper()}, migrations à jour"))
    checks.append(check_permissions(settings))

    checks.append(
        Check(OK, "Clé Anthropic", "configurée")
        if has_secret("anthropic")
        else Check(WARN, "Clé Anthropic", "absente : « ./bin/goulot setup »")
    )

    vault = Vault(db)
    if not vault.is_configured():
        checks.append(Check(WARN, "Coffre", "non configuré : « ./bin/goulot setup »"))
    else:
        details: list[str] = []
        status = OK
        if not vault.recovery_confirmed():
            status = WARN
            details.append("clé de secours non confirmée")
        passkeys = PasskeyService(db, SystemClock(), settings.origin).count()
        if passkeys == 0:
            status = WARN
            details.append("aucune clé d'accès : « ./bin/goulot vault add-passkey »")
        if not vault.master_available():
            status = FAIL
            details.append("clé maîtresse absente du trousseau : « ./bin/goulot vault recover »")
        else:
            try:
                n = coverage.verify_all(db, vault)
                details.append(f"{n} champ(s) chiffré(s) déchiffrables")
            except VaultError as exc:
                status = FAIL
                details.append(f"clé indisponible : {exc}")
            except Exception:  # DecryptionError: blocking, logged without content
                status = FAIL
                details.append("un champ chiffré ne se déchiffre plus")
        details.append(f"{passkeys} clé(s) d'accès")
        checks.append(Check(status, "Coffre", " ; ".join(details)))

    age = last_backup_age(settings.backup_dir, time.time())
    if age is None:
        checks.append(Check(WARN, "Sauvegarde", "aucune pour l'instant (quotidienne, serveur lancé)"))
    elif age > 26 * 3600:
        checks.append(Check(WARN, "Sauvegarde", f"la dernière date de {int(age // 3600)} h"))
    else:
        checks.append(Check(OK, "Sauvegarde", f"la dernière date de {int(age // 3600)} h"))
    checks.append(check_listening(settings))
    checks.append(check_filevault())
    checks.append(check_firewall())
    return checks

