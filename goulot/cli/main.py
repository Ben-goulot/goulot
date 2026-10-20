"""Command line: `./bin/goulot <commande>` (phases 1 to 4), then `goulot <commande>` (phase 5).

No command reads private data (docs/SECURITY.md, CLI and import). Commands that touch keys are run by the
user in their own Terminal: setup, vault recover, vault new-recovery-key, vault add-passkey.
"""

from __future__ import annotations

import argparse
import getpass
import socket
import sys
import webbrowser

from goulot import __version__
from goulot.db.backup import backup
from goulot.db.connection import Database, get_setting, set_setting
from goulot.doctor import FAIL, OK, WARN, run_checks
from goulot.security.audit import audit
from goulot.security.clock import SystemClock
from goulot.security.fs import ensure_private_dir
from goulot.security.logs import setup_logging
from goulot.security.sessions import consume_one_time_token, create_one_time_token, revoke_everything
from goulot.settings import BindError, Settings, load_settings
from goulot.vault.keys import Vault, VaultError
from goulot.vault.recovery import RecoveryKeyError
from goulot.vault.tokens import VaultTokenService

LATER = {
    "update": "Mises à jour automatiques : disponibles en phase 5.",
    "export": "Export de conformité : disponible en phase 6.",
}
CLEAR_SCREEN = "\033[2J\033[3J\033[H"
ALERT_SECRETS = frozenset({"telegram_bot", "smtp_password"})
PRINT_HELP = "affiche le lien sans ouvrir le navigateur par défaut (pour le coller dans un autre navigateur)"


def _db(settings: Settings, *, create: bool = False) -> Database:
    if not create and not settings.db_path.exists():
        sys.exit("Base absente : lancez d'abord « ./bin/goulot setup ».")
    ensure_private_dir(settings.home)
    db = Database(settings.db_path)
    db.migrate()
    return db


def _server_running(settings: Settings) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", settings.port)) == 0


def _open_url(url: str, *, print_only: bool = False) -> None:
    if print_only:
        print("Collez ce lien dans le navigateur de votre choix (valable peu de temps, utilisable une seule fois) :")
        print(f"  {url}")
        return
    print("Ouverture du navigateur… Si rien ne s'ouvre, collez ce lien dans Safari ou Chrome (valable peu de temps) :")
    print(f"  {url}")
    webbrowser.open(url)


# --- start, open, doctor, backup, panic ----------------------------------------------------------------
def cmd_start(settings: Settings, _: argparse.Namespace) -> None:
    from goulot.server import PortBusy, run

    setup_logging(settings.log_dir, console=True)
    print(f"Goulot {__version__} : {settings.origin} (Ctrl+C pour arrêter)")
    try:
        run(settings)
    except PortBusy as exc:
        sys.exit(str(exc))


def cmd_open(settings: Settings, args: argparse.Namespace) -> None:
    db = _db(settings)
    if not _server_running(settings):
        sys.exit("Le serveur ne tourne pas : lancez « ./bin/goulot start » dans un autre onglet du Terminal.")
    token = create_one_time_token(db, _clock(), "login")
    _open_url(f"{settings.origin}/auth#t={token}", print_only=args.print_only)


def cmd_doctor(settings: Settings, _: argparse.Namespace) -> None:
    checks = run_checks(settings)
    marks = {OK: "✅", WARN: "🟠", FAIL: "❌"}
    for c in checks:
        print(f"{marks[c.status]} {c.label} : {c.detail}")
    failed = [c for c in checks if c.status == FAIL]
    print("\nTout est au vert." if not failed and all(c.status == OK for c in checks) else "")
    if failed:
        sys.exit(1)


def cmd_backup(settings: Settings, _: argparse.Namespace) -> None:
    path = backup(_db(settings), settings.backup_dir)
    print(f"Sauvegarde écrite : {path}")


def cmd_panic(settings: Settings, _: argparse.Namespace) -> None:
    if settings.db_path.exists():
        revoke_everything(_db(settings))
        print("✅ Sessions, liens de connexion et jetons de coffre révoqués ; coffre fermé.")
    print(
        "\nÀ faire maintenant :\n"
        "1. Arrêtez le serveur (Ctrl+C dans son onglet du Terminal).\n"
        "2. Révoquez la clé API Anthropic : https://platform.claude.com/settings/keys\n"
        "   puis créez-en une nouvelle dans l'espace de travail « goulot » et lancez « ./bin/goulot setup ».\n"
        "3. Si vous en avez un : révoquez le jeton GitHub (https://github.com/settings/tokens).\n"
        "4. Si vous utilisez Telegram : envoyez /revoke à @BotFather pour changer le jeton du bot.\n"
        "5. Si la clé de signature des mises à jour est en cause : procédure de docs/UPDATES.md (« Clé compromise »)."
    )


# --- setup ---------------------------------------------------------------------------------------------
def _ask_secret(label: str) -> str:
    return getpass.getpass(f"{label} (saisie masquée, Entrée pour passer) : ").strip()


def _show_and_confirm_recovery(recovery_key: str) -> bool:
    print(
        "\n================ CLÉ DE SECOURS ================\n"
        f"\n    {recovery_key}\n\n"
        "Notez-la maintenant (papier ou gestionnaire de mots de passe). Elle ne sera plus jamais affichée.\n"
        "Sans elle ET sans sauvegarde Time Machine chiffrée hors du Mac, la perte du Mac emporte vos données privées.\n"
        "Ne la donnez à personne, pas même à Claude.\n"
        "================================================\n"
    )
    input("Appuyez sur Entrée une fois la clé notée…")
    print(CLEAR_SCREEN, end="")
    from goulot.vault.recovery import decode

    expected = decode(recovery_key)
    for attempt in range(3):
        typed = getpass.getpass("Retapez la clé de secours en entier pour confirmer (saisie masquée) : ")
        try:
            if decode(typed) == expected:
                return True
            print("Ce n'est pas la même clé.")
        except RecoveryKeyError as exc:
            print(str(exc))
        if attempt < 2:
            print("Réessayez.")
    return False


def _setup_secrets(settings: Settings) -> None:
    from goulot.security.secrets import SECRETS, has_secret, masked, set_secret

    print("\n— Secrets (enregistrés dans le trousseau macOS, jamais ailleurs) —")
    for spec in SECRETS:
        if spec.name in ALERT_SECRETS:  # asked in the alert channel section, only when relevant
            continue
        state = "déjà configuré" if has_secret(spec.name) else "absent"
        value = _ask_secret(f"{spec.label} [{state}]")
        if not value:
            continue
        set_secret(spec.name, value)
        print(f"  Enregistré ({masked(value)}).")
        if spec.name == "anthropic":
            _test_anthropic(settings, value)
        elif spec.name in PRICE_KEY_TESTS:
            _test_price_key(settings, spec.name, value.strip())


PRICE_KEY_TESTS = {"eia": "EIA", "census": "Census Bureau"}


def _test_price_key(settings: Settings, name: str, key: str) -> None:
    """One small request with the key just typed (a few days of Brent, one month of customs data)."""
    from datetime import date, timedelta

    import httpx2

    from goulot.net.allowlist import load_allowlist
    from goulot.net.client import build_client
    from goulot.prices import sources

    client = build_client(load_allowlist(settings.config_dir))
    try:
        if name == "eia":
            sources.eia(client, key, "RBRTE", date.today() - timedelta(days=14))
        else:
            sources.census_unit_prices(client, key, "811292", date(date.today().year - 1, 1, 1))
        print(f"  Test : clé acceptée par l'API {PRICE_KEY_TESTS[name]} ({len(key)} caractères).")
    except sources.PriceSourceError as exc:
        print(f"  Test : clé refusée ou inactive ({exc}). Longueur saisie : {len(key)} caractères. Vérifiez le "
              "lien d'activation de l'e-mail, puis relancez « ./bin/goulot setup » et collez-la de nouveau.")
    except httpx2.HTTPError as exc:
        print(f"  Test impossible pour l'instant ({type(exc).__name__}) ; la clé est enregistrée.")
    finally:
        client.close()


def _test_anthropic(settings: Settings, key: str) -> None:
    import anthropic

    from goulot.llm.client import build_anthropic_client
    from goulot.net.allowlist import load_allowlist

    try:
        client = build_anthropic_client(key, load_allowlist(settings.config_dir))
        client.models.retrieve("claude-sonnet-5-5")  # free call
        print("  Test : clé acceptée par l'API Anthropic.")
    except anthropic.AuthenticationError:
        print("  Test : clé refusée. Vérifiez-la dans la console Anthropic (espace « goulot »).")
    except anthropic.APIError as exc:
        print(f"  Test impossible pour l'instant ({type(exc).__name__}) ; la clé est enregistrée.")


def _setup_vault(db: Database) -> None:
    vault = Vault(db)
    print("\n— Coffre (données privées chiffrées, ouvertes par Touch ID) —")
    if not vault.is_configured():
        print(
            "Le coffre chiffre vos données d'achat (stock, délais, Ma chaîne). Une clé de secours va s'afficher\n"
            "une seule fois : elle permet de tout récupérer sur un nouveau Mac."
        )
        input("Appuyez sur Entrée pour créer le coffre…")
        try:
            recovery_key = vault.create()
        except VaultError as exc:
            sys.exit(str(exc))
    elif vault.recovery_confirmed():
        print("Coffre déjà configuré, clé de secours confirmée.")
        return
    else:
        print("Coffre configuré, mais la clé de secours n'a jamais été confirmée : une nouvelle va être émise.")
        input("Appuyez sur Entrée pour continuer…")
        recovery_key = vault.new_recovery_key()
    if _show_and_confirm_recovery(recovery_key):
        vault.mark_recovery_confirmed()
        print("✅ Clé de secours confirmée.")
    else:
        print("🟠 Clé non confirmée : relancez « ./bin/goulot setup » pour en recevoir une nouvelle.")


def _ask_alert_secret(name: str, label: str) -> None:
    """Asks for an alert secret, or only whether to replace it when the keychain already holds one."""
    from goulot.security.secrets import has_secret, masked, set_secret

    if has_secret(name) and input(f"{label} déjà enregistré. Le remplacer ? (o/N) ").strip().lower() != "o":
        return
    value = _ask_secret(label)
    if value:
        set_secret(name, value)
        print(f"  Enregistré ({masked(value)}).")


def _setup_alerts(db: Database) -> None:
    print("\n— Canal d'alerte (envoi réel branché en phase 3) —")
    with db.connect() as conn:
        current = get_setting(conn, "alert_channel")
        current_chat_id = get_setting(conn, "telegram_chat_id")
    hint = {"telegram": " [actuel : Telegram]", "email": " [actuel : e-mail]"}.get(current or "", "")
    choice = input(f"Telegram (t), e-mail (e) ou plus tard (Entrée){hint} ? ").strip().lower()
    if choice == "t":
        prompt = "Identifiant de votre discussion Telegram (nombre)"
        if current_chat_id:
            prompt += f" [{current_chat_id}, Entrée pour le garder]"
        chat_id = input(f"{prompt} : ").strip() or (current_chat_id or "")
        if chat_id.lstrip("-").isdigit():
            with db.transaction() as conn:
                set_setting(conn, "alert_channel", "telegram")
                set_setting(conn, "telegram_chat_id", chat_id)
        _ask_alert_secret("telegram_bot", "Jeton du bot Telegram")
        return
    with db.transaction() as conn:
        if choice == "e":
            set_setting(conn, "alert_channel", "email")
            set_setting(conn, "smtp_host", input("Serveur SMTP (ex. smtp.exemple.fr) : ").strip())
            set_setting(conn, "smtp_port", input("Port SMTP en TLS (465 ou 587) : ").strip() or "465")
            set_setting(conn, "smtp_user", input("Identifiant SMTP : ").strip())
            set_setting(conn, "alert_email_to", input("Adresse qui reçoit les alertes : ").strip())
    if choice == "e":
        _ask_alert_secret("smtp_password", "Mot de passe SMTP")


def cmd_setup(settings: Settings, _: argparse.Namespace) -> None:
    import platform

    print(f"Goulot {__version__} : configuration.")
    print(f"macOS {platform.mac_ver()[0]}, processeur {platform.machine()}.")
    setup_logging(settings.log_dir)
    db = _db(settings, create=True)
    _setup_secrets(settings)
    _setup_vault(db)
    _setup_alerts(db)
    print(
        "\n— Clé d'accès (Touch ID) —\n"
        "Pour ouvrir le coffre depuis le navigateur : lancez « ./bin/goulot start » dans un autre onglet,\n"
        "puis « ./bin/goulot vault add-passkey ».\n"
        "\nConfiguration terminée. Vérification : « ./bin/goulot doctor »."
    )


# --- vault -------------------------------------------------------------------------------------------
def _clock() -> SystemClock:
    return SystemClock()


def cmd_vault(settings: Settings, args: argparse.Namespace) -> None:
    action = args.vault_action
    db = _db(settings)
    vault = Vault(db)
    if action == "status":
        from goulot.vault.passkeys import PasskeyService

        print(f"Coffre configuré : {'oui' if vault.is_configured() else 'non'}")
        print(f"Clé de secours confirmée : {'oui' if vault.recovery_confirmed() else 'non'}")
        print(f"Clé maîtresse dans le trousseau : {'oui' if vault.master_available() else 'non'}")
        print(f"Clés d'accès : {PasskeyService(db, _clock(), settings.origin).count()}")
    elif action == "lock":
        VaultTokenService(db, _clock()).revoke_all("cli")
        print("Coffre fermé.")
    elif action == "add-passkey":
        if not vault.is_configured():
            sys.exit("Coffre non configuré : lancez « ./bin/goulot setup ».")
        if not _server_running(settings):
            sys.exit("Le serveur ne tourne pas : lancez « ./bin/goulot start » dans un autre onglet du Terminal.")
        typed = getpass.getpass("Clé de secours (saisie masquée) : ")
        try:
            ok = vault.check_recovery_key(typed)
        except RecoveryKeyError as exc:
            sys.exit(str(exc))
        if not ok:
            with db.transaction() as conn:
                audit(conn, "enroll_grant_refused")
            sys.exit("Clé de secours refusée.")
        login = create_one_time_token(db, _clock(), "login")
        grant = create_one_time_token(db, _clock(), "enroll")
        with db.transaction() as conn:
            audit(conn, "enroll_grant_issued")
        print("Lien d'enrôlement valable 10 minutes, utilisable une seule fois.")
        _open_url(f"{settings.origin}/auth#t={login}&g={grant}", print_only=args.print_only)
    elif action == "recover":
        typed = getpass.getpass("Clé de secours (saisie masquée) : ")
        try:
            vault.recover(typed, replace_existing=args.replace)
        except (RecoveryKeyError, VaultError) as exc:
            sys.exit(str(exc))
        print("✅ Clé maîtresse restaurée dans le trousseau. Vérification : « ./bin/goulot doctor ».")
    elif action == "new-recovery-key":
        typed = getpass.getpass("Clé de secours actuelle (saisie masquée ; Entrée si elle est perdue) : ")
        proven = False
        if typed.strip():
            try:
                proven = vault.check_recovery_key(typed)
            except RecoveryKeyError as exc:
                sys.exit(str(exc))
        else:
            code = input("Code affiché par l'app, coffre ouvert (Réglages > Coffre > Nouvelle clé de secours) : ")
            with db.transaction() as conn:
                proven = consume_one_time_token(conn, code.strip(), "rekey", _clock().time())
        if not proven:
            sys.exit("Preuve refusée : rien n'a changé.")
        recovery_key = vault.new_recovery_key()
        if _show_and_confirm_recovery(recovery_key):
            vault.mark_recovery_confirmed()
            print("✅ Nouvelle clé confirmée ; l'ancienne ne fonctionne plus.")
        else:
            print("🟠 Clé non confirmée : relancez la commande.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="goulot", description="Veille des goulots d'approvisionnement.")
    parser.add_argument("--version", action="version", version=f"goulot {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="assistant de configuration (dans votre Terminal)")
    sub.add_parser("start", help="lance le serveur sur http://localhost:8742")
    open_ = sub.add_parser("open", help="ouvre l'interface avec un lien à usage unique")
    open_.add_argument("--print", dest="print_only", action="store_true", help=PRINT_HELP)
    sub.add_parser("doctor", help="vérifie l'installation")
    sub.add_parser("backup", help="sauvegarde la base maintenant")
    sub.add_parser("panic", help="révoque tout et affiche la marche à suivre")
    vault = sub.add_parser("vault", help="coffre des données privées")
    vsub = vault.add_subparsers(dest="vault_action", required=True)
    vsub.add_parser("status")
    vsub.add_parser("lock")
    add_passkey = vsub.add_parser("add-passkey", help="enregistre une clé d'accès Touch ID (clé de secours demandée)")
    add_passkey.add_argument("--print", dest="print_only", action="store_true", help=PRINT_HELP)
    recover = vsub.add_parser("recover", help="restaure la clé maîtresse avec la clé de secours")
    recover.add_argument("--replace", action="store_true", help="remplace une autre clé maîtresse du trousseau")
    vsub.add_parser("new-recovery-key", help="émet une nouvelle clé de secours")
    ledger = sub.add_parser("ledger", help="registre des conseils")
    lsub = ledger.add_subparsers(dest="ledger_action", required=True)
    lsub.add_parser("verify", help="contrôle la chaîne d'empreintes du registre")
    history = sub.add_parser("history", help="historique des prix")
    hsub = history.add_subparsers(dest="history_action", required=True)
    backfill = hsub.add_parser("backfill", help="télécharge maintenant tout l'historique des séries d'une matière")
    backfill.add_argument("--cas", required=True, help="numéro CAS d'une matière déjà ajoutée et validée")
    inputs = hsub.add_parser("inputs", help="montre les matières premières de la chaîne type et les séries trouvées")
    inputs.add_argument("--cas", required=True, help="numéro CAS d'une matière déjà ajoutée et validée")
    cases = sub.add_parser("cases", help="bibliothèque de cas comparables")
    csub = cases.add_subparsers(dest="cases_action", required=True)
    build = csub.add_parser("build", help="prépare les cas passés d'une matière validée (devis, puis confirmation)")
    build.add_argument("--cas", required=True, help="numéro CAS d'une matière déjà ajoutée et validée")
    dev = sub.add_parser("dev", help="commandes de développement (dans votre Terminal)")
    dsub = dev.add_subparsers(dest="dev_action", required=True)
    rec = dsub.add_parser("record", help="reconstruction réelle d'un CAS de test, réponses IA enregistrées")
    rec.add_argument("--cas", required=True, help="107-13-1, 554-13-2 ou 7440-55-3")
    rec.add_argument("--force", action="store_true", help="repart de zéro (sinon : reprise après un échec)")
    rw = dsub.add_parser("record-watch", help="enregistre le tri et l'analyse du scénario de veille (moins de 1 $)")
    rw.add_argument("--force", action="store_true",
                    help="recommence : les réponses de tri inchangées sont reprises sans être repayées")
    pr = dsub.add_parser("probe", help="essai à blanc des schémas de sortie (moins de 0,15 $)")
    pr.add_argument("--only", help="noms séparés par des virgules, par exemple « cas »")
    rc = dsub.add_parser("record-cases", help="prépare les cas comparables d'un CAS de test, réponses enregistrées")
    rc.add_argument("--cas", required=True, help="107-13-1, 554-13-2 ou 7440-55-3")
    rc.add_argument("--force", action="store_true", help="repart de zéro (sinon : reprise après un échec)")
    dsub.add_parser("check-sources", help="interroge depuis le Mac les sources injoignables du bac à sable (sans IA)")
    for later in ("keygen", "release", "bootstrap"):
        p = dsub.add_parser(later, help="disponible en phase 5")
        p.add_argument("rest", nargs=argparse.REMAINDER)
    for name, message in LATER.items():
        p = sub.add_parser(name, help=message)
        p.add_argument("rest", nargs=argparse.REMAINDER)
    return parser


def _print_inputs(db: Database, material_id: str, name: str) -> None:
    """Chain type only (public data, never Ma chaîne): the material nodes of tiers 0 to 2 and the series matched."""
    from goulot.prices.collect import EXCHANGES, direct_inputs
    from goulot.prices.nbs import match_node

    with db.connect() as conn:
        nodes = conn.execute("SELECT node_key, node_type, name, product, tier FROM chain_nodes WHERE material_id = ? "
                             "AND valid_to IS NULL AND (tier <= 1 OR (tier = 2 AND node_type = 'material')) "
                             "ORDER BY tier, node_type, node_key", (material_id,)).fetchall()
        direct = {r["node_key"] for r in direct_inputs(conn, material_id)}
    print(f"Cases de la chaîne type de « {name} » (rang 0 : la matière achetée ; « intrant direct » : une matière "
          "qui y mène sans passer par une autre matière) :")
    for n in nodes:
        found = [p.fr for ex in EXCHANGES if (p := ex.match_node(n["node_key"], n["product"], n["name"]))]
        official = match_node(n["node_key"], n["product"], n["name"])
        if official is not None:
            found.append(f"{official.fr} (prix officiel chinois)")
        series = ", ".join(found) if found else "aucune série trouvée"
        mark = " · intrant direct" if n["node_key"] in direct else ""
        print(f"  rang {n['tier']} · {n['node_type']} · {n['node_key']}{mark} · nom « {n['name']} » · produit "
              f"« {n['product']} » → {series}")
    if not nodes:
        print("  aucune case : la chaîne type est vide.")
    print("Seuls les intrants directs reçoivent une série proposée. Copiez ce résultat à Claude.")


def cmd_history(settings: Settings, args: argparse.Namespace) -> None:
    from goulot.chain.identify import normalize_cas
    from goulot.net.allowlist import load_allowlist
    from goulot.net.client import build_client
    from goulot.prices.collect import backfill
    from goulot.security.secrets import get_secret

    cas = normalize_cas(args.cas)
    if cas is None:
        sys.exit("Numéro CAS invalide.")
    db = _db(settings)
    with db.connect() as conn:
        row = conn.execute("SELECT id, name, status FROM materials WHERE cas = ?", (cas,)).fetchone()
    if row is None or row["status"] != "validated":
        sys.exit("Aucune matière validée avec ce numéro CAS : ajoutez-la et validez sa chaîne dans l'app.")
    if args.history_action == "inputs":
        _print_inputs(db, row["id"], row["name"])
        return
    print(f"Historique des prix de « {row['name']} » : cela peut prendre quelques minutes, sans coût d'IA.")
    written = backfill(db, build_client(load_allowlist(settings.config_dir)), get_secret, row["id"], print)
    print(f"Terminé : {sum(written.values())} points enregistrés sur {len(written)} source(s).")
    with db.connect() as conn:
        missing = [r[0] for r in conn.execute(
            "SELECT DISTINCT i.source FROM instruments i JOIN instrument_links l ON l.instrument_id = i.id "
            "WHERE l.material_id = ? AND i.last_point_at IS NULL", (row["id"],))]
    keyed = {"eia": ("eia", "EIA"), "us_census_trade": ("census", "Census"), "bls_ppi": ("bls", "BLS"),
             "fred": ("fred", "FRED")}
    without = sorted(keyed[s][1] for s in missing if s in keyed and not get_secret(keyed[s][0]))
    if without:
        print(f"Sans clé, pas de données pour : {', '.join(without)} (facultatif ; « ./bin/goulot setup » "
              "pour en ajouter).")


def cmd_cases(settings: Settings, args: argparse.Namespace) -> None:
    from goulot.api.context import MissingAPIKey, build_context
    from goulot.chain.identify import normalize_cas
    from goulot.history import library

    cas = normalize_cas(args.cas)
    if cas is None:
        sys.exit("Numéro CAS invalide.")
    _db(settings)  # refuses an absent database
    ctx = build_context(settings)
    with ctx.db.connect() as conn:
        row = conn.execute("SELECT id, name, status FROM materials WHERE cas = ?", (cas,)).fetchone()
        running = row is not None and conn.execute(
            "SELECT 1 FROM jobs WHERE material_id = ? AND kind = 'cases' AND state = 'running'", (row["id"],)
        ).fetchone()
    if row is None or row["status"] != "validated":
        sys.exit("Aucune matière validée avec ce numéro CAS : ajoutez-la et validez sa chaîne dans l'app.")
    if running:
        sys.exit("Une préparation de cas est déjà en cours pour cette matière.")
    quote = library.cases_quote()
    print(f"Cas comparables de « {row['name']} » : recherche web ({quote.searches_max} recherches au plus), puis "
          "structuration ; les cas arrivent en brouillon, à vérifier dans l'app (écran « Bibliothèque de cas »).")
    print(f"Devis : environ {quote.estimate_usd:.2f} $, {quote.max_usd:.2f} $ au plus, sur le plafond des "
          f"reconstructions ({ctx.budget.remaining('reconstruction'):.2f} $ restants ce mois-ci).")
    if ctx.budget.remaining("reconstruction") < quote.max_usd:
        sys.exit("Plafond mensuel des reconstructions insuffisant pour ce devis : rien n'est lancé.")
    if input("Lancer ? (o/N) ").strip().lower() != "o":
        sys.exit("Rien n'est lancé.")
    try:
        backend = ctx.llm_backend()
    except MissingAPIKey as exc:
        sys.exit(str(exc))
    http = ctx.http_client()
    try:
        job_id = library.start_job(ctx.db, row["id"])
        cost = library.prepare(library.CaseDeps(ctx.db, ctx.budget, backend, http), row["id"], job_id)
    finally:
        http.close()
    with ctx.db.connect() as conn:
        message = conn.execute("SELECT message FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
    print(f"{message} Coût réel : {cost:.2f} $.")


def cmd_ledger(settings: Settings, args: argparse.Namespace) -> None:
    from goulot.ledger.chain import verify

    with _db(settings).connect() as conn:
        result = verify(conn)
    if result.ok:
        print(f"✅ Registre intact : {result.entries} entrée(s), dernière empreinte {result.head[:16]}…")
        return
    sys.exit(f"❌ Registre altéré à l'entrée n° {result.at_seq} : {result.problem}.")


def cmd_dev(settings: Settings, args: argparse.Namespace) -> None:
    if args.dev_action not in ("record", "probe", "record-watch", "check-sources", "record-cases"):
        print("Signature et publication des mises à jour : disponibles en phase 5.")
        return
    from goulot.cli.dev import cmd_dev as run_dev

    run_dev(settings, args)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command in LATER:
        print(LATER[args.command])
        return
    try:
        settings = load_settings()
    except BindError as exc:
        sys.exit(str(exc))
    handlers = {
        "setup": cmd_setup,
        "start": cmd_start,
        "open": cmd_open,
        "doctor": cmd_doctor,
        "backup": cmd_backup,
        "panic": cmd_panic,
        "vault": cmd_vault,
        "dev": cmd_dev,
        "ledger": cmd_ledger,
        "history": cmd_history,
        "cases": cmd_cases,
    }
    try:
        handlers[args.command](settings, args)
    except KeyboardInterrupt:
        print("\nInterrompu.")
