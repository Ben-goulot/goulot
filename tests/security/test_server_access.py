"""Threats 1 to 4: network access, DNS rebinding, CSRF, unauthenticated access (docs/SECURITY.md)."""

from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient

from goulot.api.context import AppContext
from goulot.api.middleware import CSP
from goulot.cli.main import main as cli_main
from goulot.security.sessions import create_one_time_token
from goulot.security.tokens import digest
from goulot.server import bind_sockets
from goulot.settings import BindError, load_settings, validate_bind_hosts
from tests.conftest import ORIGIN, api_routes, db_bytes, fill_path, login, unsafe_headers

PUBLIC_ROUTES = {("GET", "/healthz"), ("GET", "/auth"), ("POST", "/auth/exchange"), ("GET", "/{page:path}")}


def test_bind_loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    for bad in ("0.0.0.0", "::", "192.168.1.10", "10.0.0.2", "localhost", "", "127.0.0.2"):
        with pytest.raises(BindError):
            validate_bind_hosts((bad,))
    with pytest.raises(BindError):
        validate_bind_hosts(())
    with pytest.raises(BindError):
        bind_sockets(("127.0.0.1", "0.0.0.0"), 0)  # refused before any socket is opened
    sockets = bind_sockets(("127.0.0.1",), 0)
    try:
        assert all(s.getsockname()[0] == "127.0.0.1" for s in sockets)
        assert all(s.family == socket.AF_INET for s in sockets)
    finally:
        for s in sockets:
            s.close()
    monkeypatch.setenv("GOULOT_BIND", "0.0.0.0")
    with pytest.raises(BindError):
        load_settings()
    with pytest.raises(SystemExit) as exc:
        cli_main(["start"])
    assert "refusée" in str(exc.value)


def test_host_header_rejected(client: TestClient, ctx: AppContext) -> None:
    for host in ("evil.example", "evil.example:8742", "localhost:9999", "127.0.0.1", "192.168.1.5:8742"):
        for method, path in (("GET", "/healthz"), ("GET", "/api/session"), ("GET", "/auth"), ("POST", "/auth/exchange")):
            r = client.request(method, path, headers={"Host": host, "Origin": ORIGIN}, json={})
            assert r.status_code == 421, (host, path)
            assert r.headers["Content-Security-Policy"] == CSP
    # A page asked on 127.0.0.1 goes to localhost (WebAuthn needs a hostname).
    r = client.get("/", headers={"Host": "127.0.0.1:8742"}, follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == f"{ORIGIN}/"
    assert client.get("/healthz").status_code == 200


def test_csrf_and_origin(client: TestClient, ctx: AppContext) -> None:
    csrf = login(client, ctx)
    cookie = next(c for c in client.cookies.jar if c.name == "goulot_session")
    assert cookie.has_nonstandard_attr("HttpOnly")
    assert not cookie.domain_specified and cookie.path == "/"
    exchange_headers = client.post(
        "/auth/exchange",
        json={"token": create_one_time_token(ctx.db, ctx.clock, "login")},
        headers={"Origin": ORIGIN},
    ).headers
    set_cookie = exchange_headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=strict" in set_cookie and "domain=" not in set_cookie
    csrf = client.get("/api/session").json()["csrf_token"]  # the second exchange opened a new session

    def lock(headers: dict[str, str]) -> int:
        return client.post("/api/vault/lock", json={}, headers=headers).status_code

    assert lock({"Origin": ORIGIN}) == 403  # no CSRF token
    assert lock({"Origin": ORIGIN, "X-CSRF-Token": "0" * 64}) == 403  # wrong CSRF token
    assert lock({"X-CSRF-Token": csrf}) == 403  # no Origin
    assert lock({"Origin": "https://evil.example", "X-CSRF-Token": csrf}) == 403
    assert lock({"Origin": "http://localhost:8743", "X-CSRF-Token": csrf}) == 403  # another local port
    assert lock({"Origin": "http://127.0.0.1:8742", "X-CSRF-Token": csrf}) == 403
    assert lock({"Origin": ORIGIN, "X-CSRF-Token": csrf, "Sec-Fetch-Site": "cross-site"}) == 403
    assert lock({"Origin": ORIGIN, "X-CSRF-Token": csrf, "Sec-Fetch-Site": "same-site"}) == 403
    assert lock(unsafe_headers(csrf)) == 200
    # Exchange from another origin is refused too.
    token = create_one_time_token(ctx.db, ctx.clock, "login")
    assert client.post("/auth/exchange", json={"token": token}, headers={"Origin": "https://evil.example"}).status_code == 403
    # No CORS, ever.
    for r in (
        client.options("/api/session", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"}),
        client.get("/api/session", headers={"Origin": "https://evil.example"}),
        client.get("/healthz", headers={"Origin": "https://evil.example"}),
    ):
        assert not any(h.lower().startswith("access-control-") for h in r.headers)


def _fill(path: str) -> str:
    return fill_path(path, {"ref": "acrylonitrile"})


def test_auth_required_on_all_routes(client: TestClient, ctx: AppContext) -> None:
    routes = [(m, r.path) for r in api_routes(client.app) for m in r.methods]
    api = [(m, p) for m, p in routes if p.startswith("/api/")]
    assert len(api) >= 55  # the routes of the included routers are enumerated too
    assert {(m, p) for m, p in routes if not p.startswith("/api/")} == PUBLIC_ROUTES
    for method, path in api:
        r = client.request(method, _fill(path), headers={"Origin": ORIGIN}, json={})
        assert r.status_code == 401, (method, path)
    # Unknown /api/ paths too, and a forged or expired session.
    assert client.get("/api/does-not-exist").status_code == 401
    client.cookies.set("goulot_session", "forged-token-value", domain="localhost.local")
    assert client.get("/api/session").status_code == 401
    client.cookies.clear()
    login(client, ctx)
    assert client.get("/api/session").status_code == 200
    ctx.clock.advance(31 * 24 * 3600)  # type: ignore[attr-defined]
    assert client.get("/api/session").status_code == 401
    # Documentation endpoints are disabled.
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert "openapi" not in client.get(path).text.lower()


def test_launch_token_single_use(client: TestClient, ctx: AppContext) -> None:
    token = create_one_time_token(ctx.db, ctx.clock, "login")
    assert len(token) >= 43  # 256 bits in base64url
    raw = db_bytes(ctx.settings.db_path)
    assert token.encode() not in raw and digest(token).encode() in raw
    first = client.post("/auth/exchange", json={"token": token}, headers={"Origin": ORIGIN})
    assert first.status_code == 200
    client.cookies.clear()
    second = client.post("/auth/exchange", json={"token": token}, headers={"Origin": ORIGIN})
    assert second.status_code == 401 and "set-cookie" not in second.headers
    expired = create_one_time_token(ctx.db, ctx.clock, "login")
    ctx.clock.advance(61)  # type: ignore[attr-defined]
    assert client.post("/auth/exchange", json={"token": expired}, headers={"Origin": ORIGIN}).status_code == 401
    # An enrollment grant is not a login token.
    grant = create_one_time_token(ctx.db, ctx.clock, "enroll")
    assert client.post("/auth/exchange", json={"token": grant}, headers={"Origin": ORIGIN}).status_code == 401
    # Rate limit on authentication: 10 per minute.
    ctx.clock.advance(61)  # type: ignore[attr-defined]
    codes = [
        client.post("/auth/exchange", json={"token": "x" * 43}, headers={"Origin": ORIGIN}).status_code for _ in range(11)
    ]
    assert codes[:10] == [401] * 10 and codes[10] == 429


def test_security_headers(client: TestClient, ctx: AppContext) -> None:
    expected = {
        "content-security-policy": CSP,
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
        "cross-origin-opener-policy": "same-origin",
        "cross-origin-resource-policy": "same-origin",
        "permissions-policy": "camera=(), microphone=(), geolocation=()",
    }
    assert CSP == (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob: "
        "https://tiles.openfreemap.org; font-src 'self'; connect-src 'self' https://tiles.openfreemap.org; "
        "worker-src 'self' blob:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    csrf = login(client, ctx)
    responses = {
        "healthz": client.get("/healthz"),
        "page": client.get("/"),
        "auth": client.get("/auth"),
        "api": client.get("/api/session"),
        "api_error": client.post("/api/vault/lock", json={}, headers={"Origin": ORIGIN}),
        "private_closed": client.get("/api/private/coverage"),
        "not_found": client.get("/api/nope"),
        "post": client.post("/api/vault/lock", json={}, headers=unsafe_headers(csrf)),
    }
    for name, r in responses.items():
        for header, value in expected.items():
            assert r.headers.get(header) == value, (name, header)
    for name in ("healthz", "auth", "api", "api_error", "private_closed", "post"):
        assert responses[name].headers["cache-control"] == "no-store", name
    assert responses["page"].headers["cache-control"] in ("no-cache", "no-store")  # never an outdated page
    assert responses["healthz"].json().keys() == {"status", "version"}
    page = responses["page"].text
    assert "<script>" not in page and " style=" not in page  # no inline script or style attribute
