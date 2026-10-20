"""Threats 12, 14, 17 and 19: secrets, dependencies, file permissions, logs (docs/SECURITY.md)."""

from __future__ import annotations

import json
import logging
import stat
import tomllib
from pathlib import Path

from fastapi.testclient import TestClient

from goulot.api.context import AppContext
from goulot.db.backup import backup
from goulot.security.logs import setup_logging
from goulot.security.redact import redact, register_secret
from goulot.security.secrets import set_secret
from goulot.settings import REPO_ROOT
from tests.conftest import MemoryKeyring, api_routes, unsafe_headers

FAKE_SECRETS = {
    "anthropic": "sk-ant-api03-FAKEFAKEFAKEFAKEFAKEFAKEFAKE-1234",
    "telegram_bot": "123456789:AAFakeTelegramTokenForTestsOnly_abcdefghij",
    "smtp_password": "Mot2PasseSmtpTresSecret!",
    "eia": "eiaFAKEkeyFORtests0123456789",
    "github_token": "github_pat_FAKE0123456789abcdefghijklmnop",
}

# Direct dependencies approved in CLAUDE.md (« Stack »), plus the ones the user validated in docs/DECISIONS.md.
ALLOWED_PYTHON = {
    "anthropic", "apscheduler", "cryptography", "defusedxml", "fastapi", "httpx2", "jsonschema", "keyring",
    "networkx", "nh3", "pydantic", "pyyaml", "searoute", "uvicorn", "webauthn",
}
ALLOWED_PYTHON_DEV = {"bandit", "httpx", "mypy", "pip-audit", "pytest", "ruff"}
ALLOWED_NPM = {"react", "react-dom", "lightweight-charts", "maplibre-gl", "@xyflow/react", "@ibm/plex-sans", "@ibm/plex-mono"}
ALLOWED_NPM_DEV = {"vite", "@vitejs/plugin-react", "typescript", "@types/react", "@types/react-dom", "vitest"}


def test_no_secret_in_responses(
    client: TestClient, ctx: AppContext, opened: tuple[str, str], fake_keyring: MemoryKeyring
) -> None:
    csrf, vault_token = opened
    for name, value in FAKE_SECRETS.items():
        set_secret(name, value)
    secrets = [*FAKE_SECRETS.values(), *(v for (s, _), v in fake_keyring.store.items() if s == "goulot-vault")]
    assert len(secrets) == len(FAKE_SECRETS) + 1  # the vault master key is checked too
    client.put("/api/private/coverage/acrylonitrile", json={"stock_weeks": 3}, headers=unsafe_headers(csrf, vault_token))
    bodies: list[str] = []
    for route in api_routes(client.app):
        if "GET" in route.methods and "{" not in route.path:
            r = client.get(route.path, headers={"X-Vault-Token": vault_token})
            bodies.append(r.text + json.dumps(dict(r.headers)))
    bodies.append(client.post("/api/private/vault/rekey-grant", json={}, headers=unsafe_headers(csrf, vault_token)).text)
    joined = "\n".join(bodies)
    for value in secrets:
        assert value not in joined
        assert value[-12:] not in joined
    status = client.get("/api/settings/secrets").json()
    assert status["anthropic"] == "configuré" and status["bls"] == "absent"
    assert set(status.values()) <= {"configuré", "absent"}


def test_log_redaction(tmp_path: Path) -> None:
    log_file = setup_logging(tmp_path / "logs")
    register_secret("valeur-secrete-chargee-du-trousseau")
    log = logging.getLogger("goulot.test")
    log.info("clé %s", FAKE_SECRETS["anthropic"])
    log.info("telegram %s", FAKE_SECRETS["telegram_bot"])
    log.info("github ghp_ABCDEFGHIJKLMNOPQRSTUVWX0123 et %s", FAKE_SECRETS["github_token"])
    log.info("headers Authorization: Bearer abc.def.ghi x-api-key: sk-something-long Ocp-Apim-Subscription-Key: zz11")
    log.info('payload {"password": "hunter2hunter2", "token": "tok-123456789"} smtp_password=Mot2Passe')
    log.info("GET https://api.eia.gov/v2/petroleum/pri/spt/data?api_key=EIAKEY123456&frequency=daily")
    log.info("GET https://api.census.gov/data/x?get=A&key=CENSUSKEY98765")
    log.info("GET https://firms.modaps.eosdis.nasa.gov/api/area/csv/FIRMSKEY0001/VIIRS_SNPP_NRT/world/1")
    log.info("chargé : valeur-secrete-chargee-du-trousseau")
    try:
        raise RuntimeError(f"échec avec {FAKE_SECRETS['anthropic']}")
    except RuntimeError:
        log.exception("erreur")
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = log_file.read_text(encoding="utf-8")
    for leaked in (
        FAKE_SECRETS["anthropic"], FAKE_SECRETS["telegram_bot"], FAKE_SECRETS["github_token"],
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWX0123", "abc.def.ghi", "sk-something-long", "zz11", "hunter2hunter2",
        "tok-123456789", "Mot2Passe", "EIAKEY123456", "CENSUSKEY98765", "FIRMSKEY0001",
        "valeur-secrete-chargee-du-trousseau",
    ):
        assert leaked not in text, leaked
    assert "frequency=daily" in text and "VIIRS_SNPP_NRT" in text  # the rest of the line survives
    assert stat.S_IMODE(log_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(log_file.parent.stat().st_mode) == 0o700
    assert redact("rien de secret ici") == "rien de secret ici"


def test_file_permissions(client: TestClient, ctx: AppContext, opened: tuple[str, str]) -> None:
    csrf, vault_token = opened
    client.put("/api/private/coverage/acrylonitrile", json={"stock_weeks": 3}, headers=unsafe_headers(csrf, vault_token))
    log_file = setup_logging(ctx.settings.log_dir)
    logging.getLogger("goulot.test").info("ligne")
    target = backup(ctx.db, ctx.settings.backup_dir)
    with ctx.db.connect() as conn:  # keep a connection open so that the WAL files exist
        conn.execute("SELECT 1")
        paths = [ctx.settings.db_path, target, log_file]
        paths += [p for p in (Path(f"{ctx.settings.db_path}-wal"), Path(f"{ctx.settings.db_path}-shm")) if p.exists()]
        for path in paths:
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    for directory in (ctx.settings.home, ctx.settings.data_dir, ctx.settings.backup_dir, ctx.settings.log_dir):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory


def _name(spec: str) -> str:
    return spec.split(";")[0].split("[")[0].split("=")[0].split(">")[0].split("<")[0].split("~")[0].strip().lower()


def test_dependencies_allowlist() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    direct = {_name(d) for d in project["project"]["dependencies"]}
    dev = {_name(d) for d in project["dependency-groups"]["dev"]}
    assert direct <= ALLOWED_PYTHON, direct - ALLOWED_PYTHON
    assert dev <= ALLOWED_PYTHON_DEV, dev - ALLOWED_PYTHON_DEV
    assert project["tool"]["uv"]["package"] is False
    lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    assert "sha256:" in lock  # hashes pinned
    for name in direct | dev:
        assert f'name = "{name}"' in lock, name

    package = json.loads((REPO_ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    npm = set(package.get("dependencies", {}))
    npm_dev = set(package.get("devDependencies", {}))
    assert npm <= ALLOWED_NPM, npm - ALLOWED_NPM
    assert npm_dev <= ALLOWED_NPM_DEV, npm_dev - ALLOWED_NPM_DEV
    for version in [*package.get("dependencies", {}).values(), *package.get("devDependencies", {}).values()]:
        assert version[0].isdigit(), f"version not pinned exactly: {version}"
    npm_lock = json.loads((REPO_ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    for path, meta in npm_lock["packages"].items():
        if path:
            assert meta.get("integrity", "").startswith("sha512-") or meta.get("link"), path
