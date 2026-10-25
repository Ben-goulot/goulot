"""Phase 5: signed updates, installation beside, health check, rollback and pause (docs/UPDATES.md, « Tests
obligatoires »). The real update sequence runs; only GitHub, uv, launchd and the new version's processes are
replaced (`Hooks`). Throw-away signing keys."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from goulot.api.context import AppContext
from goulot.db.connection import Database, set_setting
from goulot.ledger import chain
from goulot.private import coverage, records
from goulot.settings import Settings
from goulot.updater import UPDATER_PROTOCOL
from goulot.updater.install import Hooks, Offer, Updater
from goulot.updater.manifest import Manifest, UpdateRefused, key_id, verify_signature
from goulot.updater.restore import RestoreError
from goulot.updater.state import Layout, load_state
from tests.conftest import setup_vault

LOCK = b"""version = 1
[[package]]
name = "goulot"
version = "0.1.0"
source = { virtual = "." }

[[package]]
name = "idna"
version = "3.10"
source = { registry = "https://pypi.org/simple" }
wheels = [{ url = "https://files.pythonhosted.org/packages/aa/bb/idna-3.10-py3-none-any.whl", hash = "sha256:00" }]
"""


def _keypair() -> tuple[Ed25519PrivateKey, tuple[str, str]]:
    private = Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return private, (key_id(raw), base64.b64encode(raw).decode())


def _tar(files: dict[str, bytes], special: tarfile.TarInfo | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if special is not None:
            tar.addfile(special)
    return buffer.getvalue()


def _release(version: str, key: Ed25519PrivateKey, *, archive: bytes | None = None,
             tamper_manifest: bool = False) -> Offer:
    data = archive if archive is not None else _tar({
        "goulot/__init__.py": f'__version__ = "{version}"\n'.encode(),
        "pyproject.toml": b"[project]\nname = 'goulot'\n",
        "frontend/dist/index.html": b"<!doctype html>",
        "uv.lock": LOCK,
    })
    manifest = {"name": "goulot", "version": version, "commit": "a" * 40, "created_at": "2026-10-22T04:00:00+00:00",
                "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                "min_updater_version": UPDATER_PROTOCOL, "migrations": ["0001_init.sql"],
                "notes_fr": "Changements :\n- essai"}
    raw = json.dumps(manifest).encode()
    signature = key.sign(raw)
    if tamper_manifest:
        raw = raw.replace(b"essai", b"piege")
    return Offer(raw, signature, lambda size: data)


@dataclass
class Fake:
    """Stand-in for GitHub, uv, launchd and the new version's processes."""
    offer: Offer | None = None
    healthy: bool = True
    check_fails: bool = False
    approver: str | None = "Ben-goulot"
    calls: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)

    def approved(self, manifest: Manifest) -> str:
        self.calls.append(f"approved {manifest.version}")
        if self.approver is None:
            raise UpdateRefused("version publiée sans votre approbation sur GitHub")
        return self.approver

    def hooks(self, db_path: Path, keys: tuple[tuple[str, str], ...]) -> Hooks:
        def fetch() -> Offer:
            assert self.offer is not None
            return self.offer

        def release_check(release: Path) -> None:
            self.calls.append(f"check {release.name}")
            if self.check_fails:
                raise UpdateRefused("test de santé échoué : registre")

        def migrate(release: Path) -> None:
            self.calls.append(f"migrate {release.name}")
            with Database(db_path).transaction() as conn:  # an additive migration of the new version
                conn.execute(f"CREATE TABLE IF NOT EXISTS feature_{release.name.replace('.', '_')} (id INTEGER)")

        return Hooks(
            fetch=fetch,
            sync_env=lambda release: self.calls.append(f"sync {release.name}"),
            release_check=release_check,
            migrate=migrate,
            stop_app=lambda: self.calls.append("stop"),
            start_app=lambda: self.calls.append("start"),
            healthy=lambda version: self.healthy,
            approved=self.approved,
            notify=lambda key, text: self.alerts.append(text),
            keys=keys,
        )


@pytest.fixture
def signer() -> tuple[Ed25519PrivateKey, tuple[str, str]]:
    return _keypair()


@pytest.fixture
def fake() -> Fake:
    return Fake()


@pytest.fixture
def updater(ctx: AppContext, settings: Settings, fake: Fake,
            signer: tuple[Ed25519PrivateKey, tuple[str, str]]) -> Updater:
    return Updater(Layout(settings.home), settings.db_path, settings.backup_dir,
                   fake.hooks(settings.db_path, (signer[1],)))


def _install(updater: Updater, fake: Fake, version: str, key: Ed25519PrivateKey) -> str:
    fake.offer = _release(version, key)
    outcome = updater.run()
    return outcome.status


def _tables(db_path: Path) -> set[str]:
    with Database(db_path).connect() as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_update_installs_signed_release(updater: Updater, fake: Fake, settings: Settings,
                                        signer: tuple[Ed25519PrivateKey, tuple[str, str]]) -> None:
    assert _install(updater, fake, "0.5.0", signer[0]) == "installed"
    layout = Layout(settings.home)
    assert layout.current_version() == "0.5.0"
    assert (layout.release_dir("0.5.0") / "goulot" / "__init__.py").stat().st_mode & 0o777 == 0o600
    state = load_state(layout)
    assert state.highest_installed == "0.5.0" and state.last_install["key_id"] == signer[1][0]
    assert state.last_install["approved_by"] == "Ben-goulot"
    assert fake.calls[:4] == ["approved 0.5.0", "sync 0.5.0", "check 0.5.0", "stop"]  # approval before download
    assert any("installé" in a for a in fake.alerts)


def test_update_tampered_rejected(updater: Updater, fake: Fake, settings: Settings,
                                  signer: tuple[Ed25519PrivateKey, tuple[str, str]]) -> None:
    fake.offer = _release("0.5.0", signer[0], tamper_manifest=True)
    assert updater.run().status == "refused"
    good = _release("0.5.1", signer[0])
    fake.offer = Offer(good.raw_manifest, good.signature, lambda size: b"\x1f\x8b" + b"0" * (size - 2))
    outcome = updater.run()
    assert outcome.status == "refused" and "empreinte" in outcome.message
    layout = Layout(settings.home)
    assert layout.current_version() is None and layout.installed() == []
    assert "sync 0.5.1" not in fake.calls


def test_untrusted_key_rejected(updater: Updater, fake: Fake, settings: Settings) -> None:
    stranger, _ = _keypair()
    fake.offer = _release("0.5.0", stranger)
    outcome = updater.run()
    assert outcome.status == "refused" and "clé inconnue" in outcome.message
    assert Layout(settings.home).current_version() is None
    with pytest.raises(UpdateRefused):
        verify_signature(b"{}", b"\x00" * 64, ())


def test_update_downgrade_rejected(updater: Updater, fake: Fake, settings: Settings,
                                   signer: tuple[Ed25519PrivateKey, tuple[str, str]]) -> None:
    for version in ("0.5.0", "0.5.1", "0.5.3"):
        assert _install(updater, fake, version, signer[0]) == "installed"
    assert updater.rollback().version == "0.5.1"  # back to 0.5.1; 0.5.3 stays the highest ever installed
    layout = Layout(settings.home)
    for older in ("0.5.0", "0.5.2", "0.5.3"):
        fake.offer = _release(older, signer[0])
        assert updater.run().status == "up_to_date", older
        assert layout.current_version() == "0.5.1"
    assert updater.check().status == "up_to_date"


def test_update_rejected_version_not_reinstalled(updater: Updater, fake: Fake, settings: Settings,
                                                 signer: tuple[Ed25519PrivateKey, tuple[str, str]]) -> None:
    assert _install(updater, fake, "0.5.0", signer[0]) == "installed"
    fake.check_fails = True
    assert _install(updater, fake, "0.5.1", signer[0]) == "refused"
    fake.check_fails = False
    assert _install(updater, fake, "0.5.1", signer[0]) == "up_to_date"
    layout = Layout(settings.home)
    assert layout.current_version() == "0.5.0" and "0.5.1" in load_state(layout).rejected
    assert not layout.release_dir("0.5.1").exists()


@pytest.mark.parametrize("member", ["../evil.py", "/etc/evil", "goulot/../../evil", "link", "hard", "fifo"])
def test_update_path_traversal_rejected(updater: Updater, fake: Fake, settings: Settings,
                                        signer: tuple[Ed25519PrivateKey, tuple[str, str]], member: str) -> None:
    special = None
    files = {"goulot/__init__.py": b'__version__ = "0.5.0"\n'}
    if member == "link":
        special = tarfile.TarInfo("goulot/link")
        special.type, special.linkname = tarfile.SYMTYPE, "/Users"
    elif member == "hard":
        special = tarfile.TarInfo("goulot/hard")
        special.type, special.linkname = tarfile.LNKTYPE, "goulot/__init__.py"
    elif member == "fifo":
        special = tarfile.TarInfo("goulot/fifo")
        special.type = tarfile.FIFOTYPE
    else:
        files[member] = b"print('pwned')"
    fake.offer = _release("0.5.0", signer[0], archive=_tar(files, special))
    outcome = updater.run()
    assert outcome.status == "refused" and "archive" in outcome.message
    layout = Layout(settings.home)
    assert layout.installed() == [] and not list(layout.releases.glob(".*partial"))
    assert not (settings.home / "evil.py").exists() and not (settings.home.parent / "evil.py").exists()


def test_update_healthcheck_rollback(updater: Updater, fake: Fake, settings: Settings,
                                     signer: tuple[Ed25519PrivateKey, tuple[str, str]]) -> None:
    assert _install(updater, fake, "0.5.0", signer[0]) == "installed"
    fake.healthy = False
    outcome = _install(updater, fake, "0.5.1", signer[0])
    assert outcome == "rolled_back"
    layout = Layout(settings.home)
    assert layout.current_version() == "0.5.0"
    assert "feature_0_5_1" not in _tables(settings.db_path)  # the base is the switch backup again
    assert "feature_0_5_0" in _tables(settings.db_path)
    state = load_state(layout)
    assert "0.5.1" in state.rejected and state.consecutive_failures == 1 and not state.paused
    assert any("retour automatique à 0.5.0" in a for a in fake.alerts)
    assert fake.calls[-1] == "start"


def test_update_manual_rollback_restores_backup(updater: Updater, fake: Fake, settings: Settings,
                                                signer: tuple[Ed25519PrivateKey, tuple[str, str]]) -> None:
    with pytest.raises(RestoreError):
        updater.rollback()  # nothing installed
    assert _install(updater, fake, "0.5.0", signer[0]) == "installed"
    with pytest.raises(RestoreError):
        updater.rollback()  # the version before is the working copy: not a version to go back to
    assert _install(updater, fake, "0.5.1", signer[0]) == "installed"
    layout = Layout(settings.home)
    state = load_state(layout)
    backup_file = settings.backup_dir / state.switch_into("0.5.1").backup  # type: ignore[union-attr]
    moved = backup_file.with_suffix(".away")
    backup_file.rename(moved)
    with pytest.raises(RestoreError, match="introuvable"):
        updater.rollback()
    assert layout.current_version() == "0.5.1" and "feature_0_5_1" in _tables(settings.db_path)
    moved.rename(backup_file)
    outcome = updater.rollback()
    assert outcome.status == "rolled_back" and layout.current_version() == "0.5.0"
    assert "feature_0_5_1" not in _tables(settings.db_path)
    assert "0.5.1" in load_state(layout).rejected


def test_rollback_keeps_ledger_and_private_inputs(ctx: AppContext, updater: Updater, fake: Fake,
                                                  settings: Settings,
                                                  signer: tuple[Ed25519PrivateKey, tuple[str, str]]) -> None:
    setup_vault(ctx)
    with ctx.db.transaction() as conn:
        chain.append(conn, "verdict", {"verdict": "ras"}, material_id="m1")
        set_setting(conn, "alerts_discreet", "0")
    kept = records.add(ctx.db, ctx.vault, "quote", {"material_id": "m1", "date": "2026-10-01"})
    erased = records.add(ctx.db, ctx.vault, "signal", {"material_id": "m1", "type": "delay"})
    assert _install(updater, fake, "0.5.0", signer[0]) == "installed"
    assert _install(updater, fake, "0.5.1", signer[0]) == "installed"
    # After the switch: new ledger entries (one with private fields), new inputs, an erased record, a setting.
    with ctx.db.transaction() as conn:
        chain.append(conn, "verdict", {"verdict": "pot"}, material_id="m1")
        chain.append(conn, "signal", {"scope": "personal"}, material_id="m1", private={"supplier": "canari"},
                     vault=ctx.vault)
        set_setting(conn, "alerts_discreet", "1")
    added = records.add(ctx.db, ctx.vault, "purchase", {"material_id": "m1", "quantity": 7})
    coverage.put(ctx.db, ctx.vault, "m1", coverage.Coverage(stock_weeks=6))
    assert records.delete(ctx.db, ctx.vault, "signal", erased, "m1")
    with Database(settings.db_path).connect() as conn:
        before = chain.verify(conn)
    assert updater.rollback().status == "rolled_back"
    db = Database(settings.db_path)
    with db.connect() as conn:
        after = chain.verify(conn)
        assert after.ok and after.entries == before.entries == 3 and after.head == before.head
        assert chain.private_fields(conn, ctx.vault, 3) == {"supplier": "canari"}
        ids = {k: {r["id"] for r in records.read_all(conn, ctx.vault, k)} for k in ("quote", "signal", "purchase")}
        assert conn.execute("SELECT value FROM settings WHERE key = 'alerts_discreet'").fetchone()[0] == "1"
        assert conn.execute("SELECT COUNT(*) FROM vault_tokens").fetchone()[0] == 0
    assert ids == {"quote": {kept}, "signal": set(), "purchase": {added}}
    assert coverage.list_all(db, ctx.vault)[0]["stock_weeks"] == 6
    assert "feature_0_5_1" not in _tables(settings.db_path)


def test_rollback_refused_when_ledger_diverges(ctx: AppContext, updater: Updater, fake: Fake,
                                               settings: Settings,
                                               signer: tuple[Ed25519PrivateKey, tuple[str, str]]) -> None:
    assert _install(updater, fake, "0.5.0", signer[0]) == "installed"
    with ctx.db.transaction() as conn:
        chain.append(conn, "verdict", {"verdict": "ras"}, material_id="m1")
    assert _install(updater, fake, "0.5.1", signer[0]) == "installed"
    with ctx.db.transaction() as conn:  # the ledger after the switch no longer extends the backup's
        conn.execute("DROP TRIGGER ledger_no_update")
        conn.execute("UPDATE ledger SET hash = ? WHERE seq = 1", ("f" * 64,))
    with pytest.raises(RestoreError):
        updater.rollback()
    assert Layout(settings.home).current_version() == "0.5.1"
    assert "feature_0_5_1" in _tables(settings.db_path)


def test_update_pause_after_two_failures(updater: Updater, fake: Fake, settings: Settings,
                                         signer: tuple[Ed25519PrivateKey, tuple[str, str]]) -> None:
    assert _install(updater, fake, "0.5.0", signer[0]) == "installed"
    fake.healthy = False
    assert _install(updater, fake, "0.5.1", signer[0]) == "rolled_back"
    assert _install(updater, fake, "0.5.2", signer[0]) == "rolled_back"
    layout = Layout(settings.home)
    assert load_state(layout).paused
    assert any("en pause" in a for a in fake.alerts)
    fake.healthy = True
    assert _install(updater, fake, "0.5.3", signer[0]) == "paused"
    assert layout.current_version() == "0.5.0"
    updater.resume()
    assert _install(updater, fake, "0.5.3", signer[0]) == "installed"


def test_update_mode_ask_and_off(updater: Updater, fake: Fake, settings: Settings,
                                 signer: tuple[Ed25519PrivateKey, tuple[str, str]]) -> None:
    fake.offer = _release("0.5.0", signer[0])
    assert updater.run("off").status == "disabled"
    assert updater.run("ask").status == "available" and Layout(settings.home).current_version() is None
    assert load_state(Layout(settings.home)).available == "0.5.0"
    assert updater.run("auto").status == "installed"


def test_update_too_new_for_updater_refused(updater: Updater, fake: Fake,
                                            signer: tuple[Ed25519PrivateKey, tuple[str, str]]) -> None:
    offer = _release("0.5.0", signer[0])
    manifest = json.loads(offer.raw_manifest)
    manifest["min_updater_version"] = UPDATER_PROTOCOL + 1
    raw = json.dumps(manifest).encode()
    fake.offer = Offer(raw, signer[0].sign(raw), offer.archive)
    outcome = updater.run()
    assert outcome.status == "up_to_date" and "bootstrap" in outcome.message


def test_key_rotation(tmp_path: Path) -> None:
    from goulot.updater import signing

    old, old_pub = _keypair()
    new, new_pub = _keypair()
    raw = b'{"name": "goulot"}'
    assert verify_signature(raw, old.sign(raw), (old_pub,)) == old_pub[0]
    with pytest.raises(UpdateRefused):
        verify_signature(raw, new.sign(raw), (old_pub,))  # not trusted yet
    assert verify_signature(raw, new.sign(raw), (old_pub, new_pub)) == new_pub[0]  # rotation release
    assert verify_signature(raw, old.sign(raw), (old_pub, new_pub)) == old_pub[0]
    with pytest.raises(UpdateRefused):
        verify_signature(raw, old.sign(raw), (new_pub,))  # old key removed by a later release
    # trust.py is edited in place, between its markers, and keeps the current key during a rotation.
    trust = tmp_path / "trust.py"
    trust.write_text((Path(signing.__file__).parent / "trust.py").read_text(encoding="utf-8"), encoding="utf-8")
    signing.write_trusted(trust, [])
    assert signing.read_trusted(trust) == []
    signing.write_trusted(trust, [old_pub])
    signing.write_trusted(trust, [*signing.read_trusted(trust), new_pub])
    assert signing.read_trusted(trust) == [old_pub, new_pub]
    namespace: dict[str, object] = {}
    exec(compile(trust.read_text(encoding="utf-8"), str(trust), "exec"), namespace)  # noqa: S102 - our own file
    assert namespace["TRUSTED_KEYS"] == (old_pub, new_pub)


@pytest.mark.parametrize("source", [
    'source = { registry = "https://evil.example/simple" }',
    'source = { git = "https://github.com/evil/idna" }',
    'source = { path = "/tmp/idna" }',
])
def test_update_lock_outside_pypi_rejected(updater: Updater, fake: Fake, settings: Settings,
                                           signer: tuple[Ed25519PrivateKey, tuple[str, str]], source: str) -> None:
    """A signed release whose uv.lock points outside PyPI is refused before uv runs (CLAUDE.md, rule 6)."""
    lock = LOCK.replace(b'source = { registry = "https://pypi.org/simple" }', source.encode())
    fake.offer = _release("0.5.0", signer[0], archive=_tar({"goulot/__init__.py": b"", "uv.lock": lock}))
    outcome = updater.run()
    assert outcome.status == "refused" and "uv.lock" in outcome.message
    assert not any(c.startswith("sync") for c in fake.calls)
    assert Layout(settings.home).installed() == []
    wheel_elsewhere = LOCK.replace(b"https://files.pythonhosted.org/", b"https://evil.example/")
    fake.offer = _release("0.5.1", signer[0], archive=_tar({"goulot/__init__.py": b"", "uv.lock": wheel_elsewhere}))
    assert updater.run().status == "refused" and not any(c.startswith("sync") for c in fake.calls)


def test_update_unapproved_release_rejected(updater: Updater, fake: Fake, settings: Settings,
                                            signer: tuple[Ed25519PrivateKey, tuple[str, str]]) -> None:
    """Signed but published without the owner's approval on GitHub (docs/UPDATES.md, step 2): never installed,
    nor even announced in « me demander » mode."""
    fake.approver = None
    fake.offer = _release("0.5.0", signer[0])
    assert updater.check().status == "refused"
    assert updater.run("ask").status == "refused"
    layout = Layout(settings.home)
    assert layout.installed() == [] and layout.current_version() is None
    assert "0.5.0" in load_state(layout).rejected and not any(c.startswith("sync") for c in fake.calls)
    fake.approver = "Ben-goulot"
    assert _install(updater, fake, "0.5.1", signer[0]) == "installed"
    assert load_state(layout).last_install["approved_by"] == "Ben-goulot"
