"""Update commands (docs/UPDATES.md): `goulot update`, `goulot db migrate`, `goulot doctor --release`, and the
`dev keygen`, `dev release` and `dev bootstrap` commands run by the user in their own Terminal."""

from __future__ import annotations

import argparse
import os
import platform
import sys
from pathlib import Path

from goulot import __version__
from goulot.db.connection import Database, get_setting
from goulot.settings import REPO_ROOT, Settings
from goulot.updater.install import Updater
from goulot.updater.manifest import UpdateRefused
from goulot.updater.restore import RestoreError
from goulot.updater.state import Layout, load_state, save_state

MODES = ("auto", "ask", "off")
LAUNCHER = Path.home() / ".local" / "bin" / "goulot"


def is_workcopy() -> bool:
    return (REPO_ROOT / ".git").exists()


def runs_installed(layout: Layout) -> bool:
    return REPO_ROOT.resolve().parent == layout.releases.resolve()


def update_mode(settings: Settings) -> str:
    if not settings.db_path.exists():
        return "auto"
    with Database(settings.db_path).connect() as conn:
        mode = get_setting(conn, "update_mode", "auto")
    return mode if mode in MODES else "auto"


def _updater(settings: Settings, layout: Layout) -> Updater:
    from goulot.updater.system import find_uv, live_hooks

    uv = load_state(layout).uv_path or find_uv()
    if uv is None:
        sys.exit("uv introuvable : réinstallez-le, puis relancez.")
    return Updater(layout, settings.db_path, settings.backup_dir, live_hooks(settings, uv))


def cmd_update(settings: Settings, args: argparse.Namespace) -> None:
    layout = Layout(settings.home)
    if not runs_installed(layout):
        sys.exit("Les mises à jour se gèrent depuis Goulot installé : « goulot update » (pas « ./bin/goulot »). "
                 "Première installation : « ./bin/goulot dev bootstrap ».")
    state = load_state(layout)
    if not any((args.check, args.install, args.auto, args.rollback, args.resume)):
        print(f"Version active : {layout.current_version()} ; plus haute installée : {state.highest_installed}")
        print(f"Versions installées : {', '.join(layout.installed())}")
        print(f"Versions rejetées : {', '.join(state.rejected) or 'aucune'}")
        print(f"Mode : {update_mode(settings)} ; dernière vérification : {state.last_check or 'jamais'} "
              f"({state.last_result or '—'})" + (" ; EN PAUSE" if state.paused else ""))
        return
    updater = _updater(settings, layout)
    try:
        if args.check:
            print(updater.check().message)
            return
        if args.resume:
            updater.resume()
            print("Mises à jour reprises.")
            return
        if args.rollback:
            print(updater.rollback().message)
            return
        outcome = updater.run("auto" if args.install else update_mode(settings))
    except (UpdateRefused, RestoreError) as exc:
        sys.exit(f"❌ {exc}")
    print(outcome.message)
    if outcome.status in ("refused", "rolled_back"):
        sys.exit(1)


def cmd_db(settings: Settings, args: argparse.Namespace) -> None:
    if args.db_action == "migrate":
        applied = Database(settings.db_path).migrate()
        print(f"Migrations appliquées : {applied or 'aucune'}")


def cmd_release_check(settings: Settings, release: str) -> None:
    from goulot.updater.healthcheck import HealthFailure, release_check

    try:
        for line in release_check(settings, Path(release)):
            print(f"✅ {line}")
    except HealthFailure as exc:
        sys.exit(f"❌ {exc}")
    print(f"Goulot {__version__} : test de santé réussi.")


# --- dev keygen, release, bootstrap (user's Terminal) -----------------------------------------------------
def _require_workcopy() -> None:
    if not is_workcopy():
        sys.exit("Commande de la copie de travail : lancez-la avec « ./bin/goulot » depuis ~/Projects/goulot.")


def _gh() -> str:
    from goulot.updater.signing import find_gh

    gh = find_gh()
    if gh is None:
        sys.exit("gh (l'outil GitHub) est introuvable : « brew install gh », puis « gh auth login ».")
    return gh


def cmd_keygen(args: argparse.Namespace) -> None:
    from goulot.updater import signing
    from goulot.updater.trust import REPOSITORY

    _require_workcopy()
    trust_file = REPO_ROOT / "goulot" / "updater" / "trust.py"
    try:
        if args.activate:
            signing.activate_rotation(REPOSITORY, _gh())
            print("✅ La nouvelle clé signe désormais les versions. Dites-le à Claude : il retirera l'ancienne clé "
                  "de trust.py dans une prochaine version.")
            return
        if args.rotate:
            pair = signing.prepare_rotation(trust_file)
            print(f"✅ Nouvelle clé {pair.key_id} préparée (trousseau seulement) et ajoutée à trust.py.\n"
                  "Publiez une version (encore signée par l'ancienne clé), attendez son installation, puis : "
                  "« ./bin/goulot dev keygen --activate ».")
            return
        print("Création de la clé de signature des mises à jour. Elle ne s'affiche jamais : elle part directement "
              "dans le secret GitHub et dans le trousseau (élément « goulot-signing »).")
        pair = signing.keygen(trust_file, REPOSITORY, _gh())
    except signing.KeygenError as exc:
        sys.exit(f"❌ {exc}")
    print(f"✅ Clé {pair.key_id} créée : secret GitHub « {signing.SECRET_NAME} » (environnement "
          f"« {signing.ENVIRONMENT} »), copie de secours dans le trousseau, clé publique dans "
          "goulot/updater/trust.py.\nDites à Claude que c'est fait : il vérifiera trust.py et le committera.")


def cmd_dev_release(args: argparse.Namespace) -> None:
    import subprocess  # nosec B404 - fixed program, argument list

    from goulot.updater.trust import REPOSITORY

    _require_workcopy()
    out = subprocess.run([_gh(), "workflow", "run", "release.yml", "--repo", REPOSITORY, "--ref", "main",  # noqa: S603  # nosec B603
                          "-f", f"bump={args.bump}"], capture_output=True, text=True, timeout=60, check=False)
    if out.returncode != 0:
        sys.exit(f"❌ Lancement impossible : {out.stderr.strip()[:300]}")
    print(f"✅ Publication lancée. Suivez-la sur https://github.com/{REPOSITORY}/actions/workflows/release.yml\n"
          "Quand GitHub vous le demande (notification sur le téléphone), lisez les notes du job « build », "
          "puis approuvez le déploiement « release ».")


def write_launcher(source: Path, target: Path = LAUNCHER) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".new")
    tmp.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    os.chmod(tmp, 0o700)
    os.replace(tmp, target)


def cmd_bootstrap(settings: Settings, _: argparse.Namespace) -> None:
    from goulot.updater import system
    from goulot.updater.trust import TRUSTED_KEYS

    _require_workcopy()
    if sys.platform != "darwin":
        sys.exit("Goulot s'installe sur macOS uniquement.")
    print(f"Mac : macOS {platform.mac_ver()[0] or 'inconnu'}, processeur {platform.machine()}.")
    if not TRUSTED_KEYS:
        sys.exit("Aucune clé de confiance dans trust.py : lancez d'abord « ./bin/goulot dev keygen ».")
    if not settings.db_path.exists():
        sys.exit("Base absente : lancez d'abord « ./bin/goulot setup ».")
    uv = system.find_uv()
    if uv is None:
        sys.exit("uv introuvable : « brew install uv ».")
    if system.port_open(settings.port) and not system.agent_loaded(system.APP_LABEL):
        sys.exit("Le serveur lancé à la main tourne encore : arrêtez-le (Ctrl+C dans son onglet), puis relancez.")
    layout = Layout(settings.home)
    print("Installation de la dernière version publiée : téléchargement, vérification de la signature, "
          "installation à côté, test de santé, sauvegarde de la base, bascule.")
    print("Les deux LaunchAgents démarrent Goulot à l'ouverture de session et vérifient les mises à jour chaque "
          f"nuit à 4 h 30 (fichiers {system.agent_path(system.APP_LABEL).name} et "
          f"{system.agent_path(system.UPDATER_LABEL).name} dans ~/Library/LaunchAgents).")
    agents = input("Installer les deux LaunchAgents ? (o/N) ").strip().lower() == "o"
    state = load_state(layout)
    state.uv_path = uv
    save_state(layout, state)
    hooks = system.live_hooks(settings, uv)
    loaded: list[str] = []

    def start() -> None:
        version = layout.current_version()
        if agents and version and not loaded:
            loaded.extend(system.install_agents(layout.release_dir(version), layout, settings))
        else:
            system.start_app()

    hooks.start_app = start
    if not agents:
        hooks.healthy = lambda _version: True  # tested in memory by doctor --release; started by hand
    try:
        outcome = Updater(layout, settings.db_path, settings.backup_dir, hooks).run("auto")
    except (UpdateRefused, RestoreError) as exc:
        sys.exit(f"❌ {exc}")
    print(outcome.message)
    if outcome.status != "installed":
        sys.exit(1)
    write_launcher(layout.current / "ops" / "launchd" / "goulot.sh")
    print(f"✅ Lanceur : {LAUNCHER}")
    if str(LAUNCHER.parent) not in os.environ.get("PATH", "").split(":"):
        print("Pour taper simplement « goulot », ajoutez ~/.local/bin à votre PATH (une fois) :\n"
              "  echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.zprofile\npuis ouvrez un nouvel onglet.")
    if agents:
        print("✅ LaunchAgents installés : Goulot tourne en arrière-plan. Ouvrez-le avec « goulot open ».")
    else:
        print("Sans LaunchAgent : lancez « goulot start » dans un onglet du Terminal, puis « goulot open ».")
    print("La copie de travail (« ./bin/goulot ») ne sert plus qu'aux commandes « dev ».")
