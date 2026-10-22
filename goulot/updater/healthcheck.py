"""`goulot doctor --release <dossier>`: the health test of a new version, run by that version's own code in a
separate process, before it becomes active (docs/UPDATES.md, step 4).

On a copy of the base: imports of every module, migrations, ledger chain intact after migration, every encrypted
field still decrypts with the master key, then the app started in memory (no network port) and a few routes called
with a session created in the copy, through the regular login link exchange: production code has no way around
authentication. The copy is deleted at the end. Prints the result, never any content.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import pkgutil
import shutil
import tempfile
from pathlib import Path
from typing import Any

import goulot
from goulot import __version__
from goulot.db.backup import backup
from goulot.db.connection import Database
from goulot.security.clock import SystemClock
from goulot.security.fs import ensure_private_dir
from goulot.settings import REPO_ROOT, Settings

ROUTES = ("/api/session", "/api/vault/status", "/api/settings/secrets", "/api/budget", "/api/materials",
          "/api/markets")


class HealthFailure(Exception):
    pass


def _import_everything() -> int:
    count = 0
    for info in pkgutil.walk_packages(goulot.__path__, "goulot."):
        if ".cli." in info.name or info.name.endswith("__main__"):
            continue
        importlib.import_module(info.name)
        count += 1
    return count


async def _call(app: Any, method: str, path: str, headers: dict[str, str], body: bytes = b"") -> tuple[
        int, list[tuple[bytes, bytes]], bytes]:
    """One HTTP request straight into the ASGI app: no socket, no network."""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8742),
    }
    sent = False
    status = 0
    out_headers: list[tuple[bytes, bytes]] = []
    chunks: list[bytes] = []

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            await asyncio.sleep(3600)
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status, out_headers
        if message["type"] == "http.response.start":
            status = message["status"]
            out_headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    return status, out_headers, b"".join(chunks)


def _app_answers(home: Path, port: int) -> int:
    from goulot.api.app import create_app
    from goulot.api.context import build_context
    from goulot.security.sessions import COOKIE_NAME, create_one_time_token

    test_settings = Settings(home=home, log_dir_override=home / "logs", port=port)
    ctx = build_context(test_settings, SystemClock())
    app = create_app(ctx)
    origin = test_settings.origin
    base = {"host": f"localhost:{port}"}

    async def scenario() -> int:
        status, _, raw = await _call(app, "GET", "/healthz", base)
        if status != 200 or json.loads(raw).get("version") != __version__:
            raise HealthFailure("/healthz")
        status, _, _ = await _call(app, "GET", "/api/session", base)
        if status != 401:
            raise HealthFailure("une route de l'API répond sans session")
        token = create_one_time_token(ctx.db, ctx.clock, "login")
        body = json.dumps({"token": token}).encode()
        status, headers, _ = await _call(app, "POST", "/auth/exchange", {
            **base, "origin": origin, "content-type": "application/json", "content-length": str(len(body))}, body)
        cookie = next((v.decode() for k, v in headers if k == b"set-cookie" and v.startswith(COOKIE_NAME.encode())),
                      None)
        if status != 200 or cookie is None:
            raise HealthFailure("ouverture de session")
        session_headers = {**base, "cookie": cookie.split(";", 1)[0]}
        for route in ROUTES:
            status, _, _ = await _call(app, "GET", route, session_headers)
            if status != 200:
                raise HealthFailure(f"{route} : HTTP {status}")
        return len(ROUTES)

    try:
        return asyncio.run(scenario())
    finally:
        ctx.jobs.shutdown(wait=False)


def release_check(settings: Settings, release_dir: Path) -> list[str]:
    """Raise HealthFailure at the first problem; return what was checked."""
    from goulot.ledger.chain import verify
    from goulot.private import coverage, records
    from goulot.vault.keys import Vault

    done: list[str] = []
    if REPO_ROOT.resolve() != release_dir.resolve():
        raise HealthFailure(f"code chargé depuis {REPO_ROOT}, pas depuis la version testée")
    done.append(f"{_import_everything()} modules importés")
    if not (settings.frontend_dist / "index.html").exists():
        raise HealthFailure("interface absente de la version")
    ensure_private_dir(settings.home)
    work = Path(tempfile.mkdtemp(prefix="healthcheck-", dir=settings.home))
    try:
        ensure_private_dir(work)
        home = work / "home"
        db_copy = home / "data" / "goulot.sqlite"
        if settings.db_path.exists():
            made = backup(Database(settings.db_path), work / "copy", label="healthcheck")
            ensure_private_dir(db_copy.parent)
            made.rename(db_copy)
        db = Database(db_copy)
        applied = db.migrate()
        done.append(f"migrations appliquées sur une copie : {len(applied)}")
        with db.connect() as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise HealthFailure("intégrité de la copie de la base")
            result = verify(conn)
        if not result.ok:
            raise HealthFailure(f"registre altéré après migration (entrée {result.at_seq})")
        done.append(f"registre intact ({result.entries} entrée(s))")
        vault = Vault(db)
        if vault.is_configured():
            if not vault.master_available():
                raise HealthFailure("clé maîtresse du coffre illisible dans le trousseau")
            try:
                n = coverage.verify_all(db, vault) + records.verify_all(db, vault)
            except Exception as exc:
                raise HealthFailure("un champ chiffré ne se déchiffre plus") from exc
            done.append(f"{n} champ(s) chiffré(s) déchiffrables")
        done.append(f"{_app_answers(home, settings.port)} routes de l'app en mémoire")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return done
