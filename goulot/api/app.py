"""FastAPI application. Public routes, and only them: GET /healthz, GET /auth, POST /auth/exchange and the
frontend's static files. Everything under /api/ needs a session; /api/private/ also needs a vault token."""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from goulot import __version__
from goulot.api.chain_routes import build_router
from goulot.api.context import AppContext
from goulot.api.middleware import SecurityMiddleware
from goulot.api.watch_routes import build_router as build_watch_router
from goulot.db.connection import set_setting
from goulot.private import coverage
from goulot.security.audit import audit
from goulot.security.secrets import secrets_status
from goulot.security.sessions import COOKIE_NAME, Session, consume_one_time_token, create_one_time_token
from goulot.security.tokens import digest
from goulot.settings import SESSION_ABSOLUTE_TTL, VAULT_TOKEN_SLIDING_MAX, VAULT_TOKEN_SLIDING_MIN
from goulot.vault.keys import VaultError
from goulot.vault.passkeys import PasskeyError, UnlockBlocked, parse_options
from goulot.vault.tokens import VaultToken

log = logging.getLogger("goulot.api")

_PLACEHOLDER = (
    "<!doctype html><html lang=\"fr\"><head><meta charset=\"utf-8\"><title>Goulot</title></head>"
    "<body><p>Interface non construite : lancez « npm --prefix frontend run build ».</p></body></html>"
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExchangeBody(_Strict):
    token: str = Field(min_length=20, max_length=128)


class CredentialBody(_Strict):
    credential: dict[str, Any]


class RegisterOptionsBody(_Strict):
    grant: str | None = Field(default=None, max_length=128)


class RegisterVerifyBody(_Strict):
    credential: dict[str, Any]
    label: str = Field(default="Clé d'accès", max_length=60)
    grant: str | None = Field(default=None, max_length=128)


class LockMinutesBody(_Strict):
    minutes: int = Field(ge=VAULT_TOKEN_SLIDING_MIN // 60, le=VAULT_TOKEN_SLIDING_MAX // 60)


def _session(request: Request) -> Session:
    session: Session = request.state.session
    return session


def create_app(ctx: AppContext) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, title="Goulot")
    app.state.ctx = ctx
    app.add_middleware(SecurityMiddleware)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "Requête refusée."
        return JSONResponse({"detail": detail}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def internal_error(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error: %s", type(exc).__name__)
        return JSONResponse({"detail": "Erreur interne."}, status_code=500)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": "Données invalides."}, status_code=422)

    # --- public ----------------------------------------------------------------------------------------
    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post("/auth/exchange")
    def auth_exchange(body: ExchangeBody) -> Response:
        token = ctx.sessions.exchange(body.token)
        if token is None:
            raise HTTPException(401, "Lien expiré ou déjà utilisé : relancez « goulot open ».")
        response = JSONResponse({"ok": True})
        response.set_cookie(
            COOKIE_NAME, token, max_age=SESSION_ABSOLUTE_TTL, path="/", httponly=True, samesite="strict", secure=False
        )
        return response

    # --- session ---------------------------------------------------------------------------------------
    @app.get("/api/session")
    def session_info(request: Request) -> dict[str, str]:
        return {"csrf_token": ctx.sessions.csrf_token(_session(request)), "version": __version__}

    @app.post("/api/session/logout")
    def logout(request: Request) -> Response:
        ctx.sessions.revoke(_session(request).id)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.get("/api/settings/secrets")
    def settings_secrets() -> dict[str, str]:
        return secrets_status()

    # --- vault -----------------------------------------------------------------------------------------
    def _optional_vault_token(request: Request) -> VaultToken | None:
        return ctx.vault_tokens.validate(request.headers.get("x-vault-token"), _session(request).id, extend=False)

    @app.get("/api/vault/status")
    def vault_status(request: Request) -> dict[str, Any]:
        token = _optional_vault_token(request)  # read without extending: polling never keeps the vault open
        blocked = ctx.passkeys.blocked_until()
        return {
            "configured": ctx.vault.is_configured(),
            "recovery_confirmed": ctx.vault.recovery_confirmed(),
            "passkeys": ctx.passkeys.count(),
            "unlocked": token is not None,
            "expires_in": int(token.expires_at - ctx.clock.time()) if token else 0,
            "blocked_for": int(blocked - ctx.clock.time()) if blocked else 0,
            "lock_minutes": int(ctx.vault_tokens.sliding_ttl() // 60),
        }

    @app.post("/api/vault/unlock/options")
    def unlock_options(request: Request) -> Response:
        try:
            options = ctx.passkeys.authentication_options(_session(request).id)
        except UnlockBlocked:
            raise HTTPException(429, "Trop d'échecs : ouverture bloquée 15 minutes.") from None
        except PasskeyError as exc:
            raise HTTPException(400, str(exc)) from None
        return JSONResponse({"options": parse_options(options)})

    @app.post("/api/vault/unlock/verify")
    def unlock_verify(request: Request, body: CredentialBody) -> dict[str, Any]:
        session = _session(request)
        try:
            ctx.passkeys.verify_authentication(session.id, body.credential)
        except UnlockBlocked:
            raise HTTPException(429, "Trop d'échecs : ouverture bloquée 15 minutes.") from None
        except PasskeyError:
            raise HTTPException(403, "Ouverture refusée.") from None
        token, ttl = ctx.vault_tokens.issue(session.id)
        return {"vault_token": token, "expires_in": int(ttl)}

    @app.post("/api/vault/lock")
    def vault_lock(request: Request) -> dict[str, bool]:
        ctx.vault_tokens.revoke_session(_session(request).id)
        return {"ok": True}

    def _grant_valid(grant: str | None, *, consume: bool) -> bool:
        if not grant:
            return False
        now = ctx.clock.time()
        with ctx.db.transaction() as conn:
            if consume:
                return consume_one_time_token(conn, grant, "enroll", now)
            row = conn.execute(
                "SELECT 1 FROM launch_tokens WHERE token_hash = ? AND purpose = 'enroll' AND used_at IS NULL "
                "AND expires_at >= ?",
                (digest(grant), now),
            ).fetchone()
            return row is not None

    def _may_enroll(request: Request, grant: str | None, *, consume: bool) -> bool:
        """First passkey: an enrollment grant issued in the Terminal after typing the recovery key.
        Later ones: the same, or a fresh signature (< 2 min) from an existing passkey."""
        token = _optional_vault_token(request)
        if token is not None and ctx.vault_tokens.is_fresh(token) and ctx.passkeys.count() > 0:
            return True
        return _grant_valid(grant, consume=consume)

    @app.post("/api/vault/passkeys/register/options")
    def register_options(request: Request, body: RegisterOptionsBody) -> Response:
        if not ctx.vault.is_configured():
            raise HTTPException(409, "Coffre non configuré : lancez « goulot setup ».")
        if not _may_enroll(request, body.grant, consume=False):
            with ctx.db.transaction() as conn:
                audit(conn, "passkey_enrollment_refused")
            raise HTTPException(403, "Preuve requise : clé de secours (Terminal) ou Touch ID récent.")
        options = ctx.passkeys.registration_options(_session(request).id)
        return JSONResponse({"options": parse_options(options)})

    @app.post("/api/vault/passkeys/register/verify")
    def register_verify(request: Request, body: RegisterVerifyBody) -> dict[str, Any]:
        if not _may_enroll(request, body.grant, consume=True):
            raise HTTPException(403, "Preuve requise : clé de secours (Terminal) ou Touch ID récent.")
        try:
            passkey_id = ctx.passkeys.verify_registration(_session(request).id, body.credential, body.label)
        except PasskeyError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"id": passkey_id}

    # --- private (vault token required by the middleware) ----------------------------------------------
    @app.get("/api/private/passkeys")
    def passkeys_list() -> list[dict[str, Any]]:
        return [p.__dict__ for p in ctx.passkeys.list()]

    @app.post("/api/private/passkeys/{passkey_id}/revoke")
    def passkey_revoke(passkey_id: str) -> dict[str, bool]:
        try:
            ctx.passkeys.revoke(passkey_id)
        except PasskeyError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"ok": True}

    @app.put("/api/private/vault/lock-minutes")
    def vault_lock_minutes(body: LockMinutesBody) -> dict[str, int]:
        with ctx.db.transaction() as conn:
            set_setting(conn, "vault_lock_minutes", str(body.minutes))
        return {"minutes": body.minutes}

    @app.post("/api/private/vault/rekey-grant")
    def rekey_grant() -> dict[str, str]:
        """Proof, for `goulot vault new-recovery-key` in the Terminal, that the vault is open in the app."""
        return {"code": create_one_time_token(ctx.db, ctx.clock, "rekey")}

    material_ref = re.compile(coverage.MATERIAL_REF_PATTERN)

    def _check_ref(ref: str) -> str:
        if not material_ref.match(ref):
            raise HTTPException(422, "Données invalides.")
        return ref

    @app.get("/api/private/coverage")
    def coverage_list() -> list[dict[str, Any]]:
        try:
            return coverage.list_all(ctx.db, ctx.vault)
        except VaultError:
            raise HTTPException(503, "Clé du coffre indisponible.") from None

    @app.put("/api/private/coverage/{ref}")
    def coverage_put(ref: str, body: coverage.Coverage) -> dict[str, bool]:
        coverage.put(ctx.db, ctx.vault, _check_ref(ref), body)
        return {"ok": True}

    @app.delete("/api/private/coverage/{ref}")
    def coverage_delete(ref: str) -> dict[str, bool]:
        return {"ok": coverage.delete(ctx.db, _check_ref(ref))}

    app.include_router(build_router(ctx))
    app.include_router(build_watch_router(ctx))

    # --- frontend (static, no data) --------------------------------------------------------------------
    dist = ctx.settings.frontend_dist
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    def _index() -> Response:
        index = dist / "index.html"
        if index.is_file():
            return FileResponse(index, media_type="text/html")
        return HTMLResponse(_PLACEHOLDER)

    @app.get("/auth")
    def auth_page() -> Response:
        return _index()

    @app.get("/{page:path}")
    def spa(page: str) -> Response:
        if page.startswith("api/") or page == "api":
            raise HTTPException(404, "Introuvable.")
        candidate = (dist / page).resolve()
        if page and candidate.is_file() and dist.resolve() in candidate.parents and candidate.suffix in (
            ".ico", ".svg", ".png", ".woff2", ".txt",
        ):
            return FileResponse(candidate)
        return _index()

    return app
