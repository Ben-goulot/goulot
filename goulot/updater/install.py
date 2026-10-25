"""The update steps (docs/UPDATES.md, « L'updater »): check, authenticate, install beside, test, back up, switch,
watch, notify; automatic rollback when the new version does not answer, manual rollback, pause after 2 failures.

Every side effect outside the Goulot folder (network, uv, the new version's own code, launchd, the health probe,
alerts) goes through `Hooks`, so that the tests drive the real sequence without any of them.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from goulot.db.backup import backup
from goulot.db.connection import Database
from goulot.security.fs import ensure_private_dir
from goulot.updater import archive
from goulot.updater.manifest import (
    Manifest,
    UpdateRefused,
    check_archive,
    check_installable,
    parse_manifest,
    verify_signature,
)
from goulot.updater.restore import RestoreError, restore
from goulot.updater.state import VERSIONS_KEPT, Layout, State, Switch, load_state, save_state

log = logging.getLogger("goulot.updater")

WORKCOPY = "copie-de-travail"


@dataclass
class Offer:
    """A release found on GitHub: its signed manifest and a way to fetch its archive."""
    raw_manifest: bytes
    signature: bytes
    archive: Callable[[int], bytes]


@dataclass
class Hooks:
    fetch: Callable[[], Offer]
    sync_env: Callable[[Path], None]          # uv sync --locked --no-dev --no-build
    release_check: Callable[[Path], None]     # the new version's `doctor --release`; raises UpdateRefused
    migrate: Callable[[Path], None]           # the new version's `db migrate`, on the real base
    stop_app: Callable[[], None]
    start_app: Callable[[], None]
    healthy: Callable[[str], bool]            # /healthz answers this version within 60 s
    approved: Callable[[Manifest], str]       # who approved its publication on GitHub; raises UpdateRefused
    notify: Callable[[str, str], None]        # (dedupe key, text)
    keys: tuple[tuple[str, str], ...]


@dataclass
class Outcome:
    status: str          # up_to_date, installed, refused, rolled_back, paused, disabled, offline, available
    message: str
    version: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@contextlib.contextmanager
def single_run(layout: Layout) -> Iterator[None]:
    ensure_private_dir(layout.home)
    with open(layout.lock_file, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateRefused("une mise à jour est déjà en cours") from exc
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


class Updater:
    def __init__(self, layout: Layout, db_path: Path, backup_dir: Path, hooks: Hooks) -> None:
        self.layout = layout
        self.db_path = db_path
        self.backup_dir = backup_dir
        self.hooks = hooks

    # --- step 3: install beside ----------------------------------------------------------------------------
    def install_beside(self, manifest: Manifest, offer: Offer) -> Path:
        data = offer.archive(manifest.size)
        check_archive(manifest, data)
        ensure_private_dir(self.layout.releases)
        target = self.layout.release_dir(manifest.version)
        if self.layout.current_version() == manifest.version:
            raise UpdateRefused("cette version est déjà active")
        if target.exists():
            shutil.rmtree(target)  # leftover of an attempt that never became active
        partial = self.layout.releases / f".{manifest.version}.partial"
        if partial.exists():
            shutil.rmtree(partial)
        try:
            archive.extract(data, partial)
        except BaseException:
            shutil.rmtree(partial, ignore_errors=True)
            raise
        partial.rename(target)
        archive.check_lock_sources(target)
        self.hooks.sync_env(target)
        return target

    # --- the whole sequence --------------------------------------------------------------------------------
    def run(self, mode: str = "auto") -> Outcome:
        with single_run(self.layout):
            state = load_state(self.layout)
            state.last_check = _now()
            try:
                return self._run(state, mode)
            finally:
                save_state(self.layout, state)

    def _run(self, state: State, mode: str) -> Outcome:
        if mode == "off":
            state.last_result = "désactivées dans les réglages"
            return Outcome("disabled", "Mises à jour désactivées dans les réglages.")
        if state.paused:
            state.last_result = "en pause après deux échecs"
            return Outcome("paused", "Mises à jour en pause après deux échecs : « goulot update --resume ».")
        try:
            offer = self.hooks.fetch()
        except UpdateRefused as exc:
            return self._refused(state, None, str(exc))
        except Exception as exc:  # network trouble: try again next time, not a failure of a release
            state.last_result = f"vérification impossible ({type(exc).__name__})"
            return Outcome("offline", "Vérification impossible pour l'instant : nouvel essai au prochain passage.")
        try:
            key = verify_signature(offer.raw_manifest, offer.signature, self.hooks.keys)
            manifest = parse_manifest(offer.raw_manifest)
        except UpdateRefused as exc:
            return self._refused(state, None, str(exc))
        try:
            check_installable(manifest, highest_installed=state.highest_installed, rejected=state.rejected)
        except UpdateRefused as exc:
            state.available = None
            state.last_result = "à jour"
            return Outcome("up_to_date", f"Aucune nouvelle version ({exc}).")
        try:
            approver = self.hooks.approved(manifest)
        except UpdateRefused as exc:
            return self._refused(state, manifest.version, str(exc))
        except Exception as exc:  # GitHub unreachable: try again next time
            state.last_result = f"vérification impossible ({type(exc).__name__})"
            return Outcome("offline", "Vérification impossible pour l'instant : nouvel essai au prochain passage.")
        if mode == "ask":
            state.available = manifest.version
            state.last_result = f"version {manifest.version} disponible"
            self.hooks.notify(f"update-available:{manifest.version}",
                              f"Goulot {manifest.version} est disponible : « goulot update --install ».")
            return Outcome("available", f"Version {manifest.version} disponible.", manifest.version)
        try:
            release = self.install_beside(manifest, offer)
            self.hooks.release_check(release)
        except UpdateRefused as exc:
            return self._refused(state, manifest.version, str(exc))
        except Exception as exc:  # uv or the disk: the release itself is not at fault, it is not rejected
            shutil.rmtree(self.layout.release_dir(manifest.version), ignore_errors=True)
            return self._refused(state, manifest.version, f"installation impossible ({type(exc).__name__})",
                                 reject=False)
        return self.switch(state, manifest, key, approver)

    def check(self) -> Outcome:
        """`goulot update --check`: what the latest release is, without installing nor counting a failure."""
        state = load_state(self.layout)
        try:
            offer = self.hooks.fetch()
            verify_signature(offer.raw_manifest, offer.signature, self.hooks.keys)
            manifest = parse_manifest(offer.raw_manifest)
        except UpdateRefused as exc:
            return Outcome("refused", f"Dernière release refusée : {exc}.")
        except Exception as exc:
            return Outcome("offline", f"Vérification impossible ({type(exc).__name__}).")
        try:
            check_installable(manifest, highest_installed=state.highest_installed, rejected=state.rejected)
        except UpdateRefused as exc:
            return Outcome("up_to_date", f"À jour ({exc}).", manifest.version)
        try:
            approver = self.hooks.approved(manifest)
        except UpdateRefused as exc:
            return Outcome("refused", f"Version {manifest.version} refusée : {exc}.", manifest.version)
        except Exception as exc:
            return Outcome("offline", f"Vérification impossible ({type(exc).__name__}).")
        return Outcome("available", f"Version {manifest.version} disponible, signature valide, approuvée par "
                       f"{approver}.", manifest.version)

    def _refused(self, state: State, version: str | None, reason: str, *, reject: bool = True) -> Outcome:
        if version is not None and reject:
            state.reject(version)
            if self.layout.current_version() != version:
                shutil.rmtree(self.layout.release_dir(version), ignore_errors=True)
        state.record_failure()
        state.last_result = f"refusée : {reason}"
        label = f"Goulot {version}" if version else "Une mise à jour"
        text = f"{label} n'a pas été installée : {reason}."
        if state.paused:
            text += " Mises à jour en pause : « goulot update --resume » après vérification."
        self.hooks.notify(f"update-refused:{version or 'unknown'}:{_now()}", text)
        log.warning("update refused: %s", reason)
        return Outcome("refused", text, version)

    # --- steps 5 to 8: back up, switch, watch, notify ------------------------------------------------------
    def switch(self, state: State, manifest: Manifest, key: str, approver: str) -> Outcome:
        version = manifest.version
        previous = self.layout.current_version() or WORKCOPY
        db = Database(self.db_path)
        backup_path = backup(db, self.backup_dir, label=f"switch-{previous}-to-{version}")
        sw = Switch(previous, version, backup_path.name, _now())
        self.hooks.stop_app()
        try:
            self.hooks.migrate(self.layout.release_dir(version))
            self.layout.point_current(version)
        except Exception as exc:
            try:
                self._put_back(previous, backup_path, version)
            except RestoreError as restore_exc:
                self.hooks.start_app()  # `current` still names the previous version: it starts again
                return self._rollback_failed(state, version, restore_exc)
            self.hooks.start_app()
            return self._refused(state, version, f"migration impossible ({type(exc).__name__})")
        state.switches.append(sw)
        state.raise_highest(version)
        self.hooks.start_app()
        if not self.hooks.healthy(version):
            self.hooks.stop_app()
            try:
                self._put_back(previous, backup_path, version)
            except RestoreError as exc:
                return self._rollback_failed(state, version, exc)
            self.hooks.start_app()
            state.reject(version)
            state.record_failure()
            state.last_result = f"{version} annulée : ne répondait pas après la bascule"
            text = f"Goulot {version} ne répondait pas : retour automatique à {previous}."
            if state.paused:
                text += " Mises à jour en pause : « goulot update --resume » après vérification."
            self.hooks.notify(f"update-rolled-back:{version}", text)
            return Outcome("rolled_back", text, version)
        state.consecutive_failures = 0
        state.available = None
        state.last_install = {"version": version, "at": _now(), "key_id": key, "commit": manifest.commit,
                              "approved_by": approver}
        state.last_result = f"{version} installée"
        self.cleanup(state)
        notes = manifest.notes_fr.strip()
        text = f"Goulot {version} installé." + (f"\n{notes[:1500]}" if notes else "")
        self.hooks.notify(f"update-installed:{version}", text)
        return Outcome("installed", f"Goulot {version} installé.", version)

    def _rollback_failed(self, state: State, version: str, exc: RestoreError) -> Outcome:
        """The new version stays in place, stopped; the updater pauses and the user is told."""
        state.paused = True
        state.reject(version)
        state.last_result = f"retour arrière impossible : {exc}"
        text = (f"Goulot {version} ne répond pas et le retour arrière a échoué : {exc}. "
                "Lancez « goulot doctor », puis « goulot update --rollback ».")
        self.hooks.notify(f"update-rollback-failed:{version}", text)
        return Outcome("refused", text, version)

    def _put_back(self, previous: str, backup_path: Path, abandoned: str) -> None:
        """Restore the base and the previous link (the app is stopped)."""
        restore(self.db_path, backup_path, self.backup_dir, abandoned)
        if previous == WORKCOPY:
            if self.layout.current.is_symlink():
                self.layout.current.unlink()
        else:
            self.layout.point_current(previous)

    # --- manual rollback -----------------------------------------------------------------------------------
    def rollback(self) -> Outcome:
        with single_run(self.layout):
            state = load_state(self.layout)
            current = self.layout.current_version()
            if current is None:
                raise RestoreError("aucune version installée")
            sw = state.switch_into(current)
            if sw is None or sw.from_version == WORKCOPY or sw.from_version not in self.layout.installed():
                raise RestoreError("aucune version précédente encore installée : rien n'a été modifié")
            backup_path = self.backup_dir / sw.backup
            if not backup_path.exists():
                raise RestoreError(f"sauvegarde de bascule {sw.backup} introuvable : rien n'a été modifié")
            self.hooks.stop_app()
            try:
                self._put_back(sw.from_version, backup_path, current)
            except RestoreError:
                self.hooks.start_app()
                raise
            self.hooks.start_app()
            state.reject(current)
            state.last_result = f"retour manuel de {current} à {sw.from_version}"
            save_state(self.layout, state)
            ok = self.hooks.healthy(sw.from_version)
            text = f"Retour à Goulot {sw.from_version} ; la version {current} ne sera plus installée."
            self.hooks.notify(f"update-manual-rollback:{current}", text)
            return Outcome("rolled_back", text + ("" if ok else " Attention : l'app ne répond pas encore."),
                           sw.from_version)

    def resume(self) -> None:
        with single_run(self.layout):
            state = load_state(self.layout)
            state.paused = False
            state.consecutive_failures = 0
            state.last_result = "reprise par l'utilisateur"
            save_state(self.layout, state)

    # --- housekeeping --------------------------------------------------------------------------------------
    def cleanup(self, state: State) -> None:
        """Keep the 3 latest versions (always the active one and the one before it) and the switch backups of
        the versions still installed."""
        current = self.layout.current_version()
        into = state.switch_into(current) if current else None
        before = into.from_version if into else None
        installed = self.layout.installed()
        keep = set(installed[-VERSIONS_KEPT:]) | {v for v in (current, before) if v}
        for version in installed:
            if version not in keep:
                shutil.rmtree(self.layout.release_dir(version), ignore_errors=True)
        still = set(self.layout.installed())
        kept_switches = [s for s in state.switches if s.from_version in still and s.to_version in still]
        for s in state.switches:
            if s not in kept_switches:
                (self.backup_dir / s.backup).unlink(missing_ok=True)
        state.switches = kept_switches
        abandoned = sorted(self.backup_dir.glob("goulot-abandoned-*.sqlite"))
        for old in abandoned[:-VERSIONS_KEPT]:
            old.unlink()
