"""Request gate, applied to every request before any route (docs/SECURITY.md, threats 2 to 4 and 24).

Order: Host allowlist (421) → redirect of pages asked on 127.0.0.1 → rate limit → body size → Origin and
Sec-Fetch-Site on unsafe methods → session on /api/ → CSRF on unsafe /api/ methods → vault token on
/api/private/. Deny by default: a new /api/ route is protected without doing anything. Security headers go on
every response, errors included.
"""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from goulot.api.context import AppContext
from goulot.security.sessions import COOKIE_NAME
from goulot.settings import MAX_REQUEST_BODY

log = logging.getLogger("goulot.http")

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data: blob: https://tiles.openfreemap.org; font-src 'self'; "
    "connect-src 'self' https://tiles.openfreemap.org; worker-src 'self' blob:; object-src 'none'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
PUBLIC_POST = frozenset({"/auth/exchange"})
AUTH_PATHS = frozenset({"/auth", "/auth/exchange"})


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def apply_headers(path: str, response: Response) -> Response:
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    if path.startswith("/api/") or path in AUTH_PATHS or path == "/healthz":
        response.headers["Cache-Control"] = "no-store"
    return response


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        response = await self._gate(request, call_next, path)
        log.info("%s %s %s", request.method, path, response.status_code)
        return apply_headers(path, response)

    async def _gate(self, request: Request, call_next: RequestResponseEndpoint, path: str) -> Response:
        ctx: AppContext = request.app.state.ctx
        settings = ctx.settings
        host = request.headers.get("host", "")
        if host not in settings.allowed_hosts:
            return _error(421, "Hôte refusé.")
        is_api = path.startswith("/api/")

        if host.startswith("127.0.0.1:") and request.method in ("GET", "HEAD") and not is_api and path != "/healthz":
            # WebAuthn needs a hostname: pages are always served on localhost.
            query = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(f"{settings.origin}{path}{query}", status_code=307)

        bucket = "auth" if path in AUTH_PATHS else "api" if is_api else None
        if bucket and not ctx.limiter.allow(bucket):
            return _error(429, "Trop de requêtes, réessayez dans une minute.")

        unsafe = request.method in UNSAFE_METHODS
        if unsafe:
            length = request.headers.get("content-length")
            if length is None and request.method != "DELETE":
                return _error(411, "Longueur de requête requise.")
            if length is not None and (not length.isdigit() or int(length) > MAX_REQUEST_BODY):
                return _error(413, "Requête trop volumineuse.")
            if request.headers.get("origin") != settings.origin:
                return _error(403, "Origine refusée.")
            if not is_api and path not in PUBLIC_POST:
                return _error(405, "Méthode refusée.")
        fetch_site = request.headers.get("sec-fetch-site")
        if (is_api or path in PUBLIC_POST) and fetch_site is not None and fetch_site not in ("same-origin", "none"):
            return _error(403, "Origine refusée.")

        ctx.vault_tokens.check_clock()

        if is_api:
            session = ctx.sessions.validate(request.cookies.get(COOKIE_NAME))
            if session is None:
                return _error(401, "Session requise : lancez « goulot open ».")
            request.state.session = session
            if unsafe and not ctx.sessions.check_csrf(session, request.headers.get("x-csrf-token")):
                return _error(403, "Jeton CSRF invalide.")
            if path.startswith("/api/private/"):
                token = ctx.vault_tokens.validate(request.headers.get("x-vault-token"), session.id, extend=True)
                if token is None:
                    return _error(403, "Coffre fermé.")
                request.state.vault_token = token
        return await call_next(request)
