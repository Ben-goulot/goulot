"""Phase 5 pieces around the update sequence: health test of a version, release archive, version numbers,
release notes, LaunchAgent templates, the working-copy guard and the Settings routes."""

from __future__ import annotations

import io
import plistlib
import subprocess
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from goulot.api.context import AppContext
from goulot.private import coverage
from goulot.settings import REPO_ROOT, Settings
from goulot.updater import packaging
from goulot.updater.healthcheck import HealthFailure, release_check
from goulot.updater.state import Layout
from goulot.updater.system import render_agent
from tests.conftest import login, setup_vault, unsafe_headers


def test_release_check_on_a_copy(ctx: AppContext, settings: Settings, tmp_path: Path) -> None:
    setup_vault(ctx)
    coverage.put(ctx.db, ctx.vault, "m1", coverage.Coverage(stock_weeks=4))
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>")
    checked = release_check(Settings(home=settings.home, log_dir_override=settings.log_dir, frontend_dist=dist),
                            REPO_ROOT)
    assert any("déchiffrables" in c for c in checked) and any("routes" in c for c in checked)
    assert not list(settings.home.glob("healthcheck-*"))  # the copy is gone
    with pytest.raises(HealthFailure):
        release_check(settings, tmp_path)  # code not loaded from the tested version


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)  # noqa: S603, S607


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for name, text in {"goulot/__init__.py": '__version__ = "0.5.0"\n', "goulot/db/migrations/0001_init.sql": "",
                       "pyproject.toml": "", "uv.lock": "version = 1\n", ".python-version": "3.14\n",
                       "tests/test_x.py": "", "docs/SPEC.md": "", "frontend/dist/index.html": "<html>"}.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    _git(root, "init", "-q")
    _git(root, "add", "goulot", "pyproject.toml", "uv.lock", ".python-version", "tests", "docs")
    _git(root, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "Premier essai")
    return root


def test_archive_deterministic_and_limited(repo: Path, tmp_path: Path) -> None:
    a = packaging.build_archive("0.5.1", tmp_path / "a", repo, mtime=0)
    b = packaging.build_archive("0.5.1", tmp_path / "b", repo, mtime=0)
    assert a["sha256"] == b["sha256"] and a["migrations"] == ["0001_init.sql"]
    with tarfile.open(fileobj=io.BytesIO((tmp_path / "a" / "goulot-0.5.1.tar.gz").read_bytes())) as tar:
        names = tar.getnames()
        init = tar.extractfile("goulot/__init__.py").read()  # type: ignore[union-attr]
    assert "frontend/dist/index.html" in names and "uv.lock" in names
    assert not any(n.startswith(("tests/", "docs/")) for n in names)
    assert init == b'__version__ = "0.5.1"\n'


def test_next_version() -> None:
    assert packaging.next_version(None, "patch", "0.5.0") == "0.5.0"
    assert packaging.next_version("v0.5.0", "patch", "0.5.0") == "0.5.1"
    assert packaging.next_version("v0.5.3", "minor", "0.5.0") == "0.6.0"
    assert packaging.next_version("v0.5.3", "patch", "1.0.0") == "1.0.0"


def test_release_notes(repo: Path) -> None:
    _git(repo, "tag", "v0.5.0")
    (repo / "uv.lock").write_text('version = 1\n[[package]]\nname = "httpx2"\nversion = "1.2.0"\n')
    _git(repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qam", "Deps update")
    notes = packaging.release_notes("v0.5.0", repo)
    assert "- Deps update" in notes and "httpx2 1.2.0 (ajoutée)" in notes and "Premier essai" not in notes


def test_launch_agents_point_at_current(settings: Settings) -> None:
    current = Layout(settings.home).current
    for label in ("com.goulot.app", "com.goulot.updater"):
        data = plistlib.loads(render_agent(REPO_ROOT / "ops" / "launchd" / f"{label}.plist", current, settings.log_dir))
        assert data["Label"] == label and data["Umask"] == 0o077
        assert data["ProgramArguments"][0] == f"{current}/.venv/bin/python"
        assert "Projects" not in plistlib.dumps(data).decode()  # never the working copy
    updater = plistlib.loads(render_agent(REPO_ROOT / "ops" / "launchd" / "com.goulot.updater.plist", current,
                                          settings.log_dir))
    assert updater["StartCalendarInterval"] == {"Hour": 4, "Minute": 30} and updater["RunAtLoad"]


def test_workcopy_refuses_once_installed(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    from goulot.cli import main as cli

    layout = Layout(settings.home)
    layout.releases.mkdir(parents=True)
    (layout.releases / "0.5.0").mkdir()
    layout.point_current("0.5.0")
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    with pytest.raises(SystemExit, match="Goulot est installé"):
        cli.main(["doctor"])
    with pytest.raises(SystemExit, match="Goulot installé"):
        from goulot.cli.updates import cmd_update

        cmd_update(settings, cli.build_parser().parse_args(["update", "--check"]))


def test_settings_routes(client: TestClient, ctx: AppContext) -> None:
    csrf = login(client, ctx)
    status = client.get("/api/updates").json()
    assert status["mode"] == "auto" and status["installed"] is False
    assert client.put("/api/settings/update-mode", json={"mode": "ask"}, headers=unsafe_headers(csrf)).status_code == 200
    assert client.get("/api/updates").json()["mode"] == "ask"
    assert client.put("/api/settings/update-mode", json={"mode": "x"}, headers=unsafe_headers(csrf)).status_code == 422
    labels = {i["label"] for i in client.get("/api/security/report").json()["items"]}
    assert {"Écoute réseau", "Coffre", "Mises à jour", "Dépense IA du mois", "Registre des conseils"} <= labels
