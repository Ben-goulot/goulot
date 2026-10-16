"""The single outbound transport, used by the sources and by the Anthropic SDK (CLAUDE.md, rule 6).

- HTTPS only, port 443, certificates verified, no proxy from the environment.
- Host on the allowlist, checked on every request, redirects included (each hop goes through here).
- DNS resolved here; private, loopback, link-local, multicast and reserved addresses refused; the connection
  goes to the address that was checked, so a second resolution cannot swap it (DNS rebinding).
- Responses limited to 10 MB on the wire.
"""

from __future__ import annotations

import ipaddress
import socket
import typing
from collections.abc import Callable, Iterator

import httpcore2
import httpx2
from httpcore2._backends.base import SOCKET_OPTION, NetworkBackend, NetworkStream
from httpcore2._backends.sync import SyncBackend

from goulot.net.allowlist import Allowlist

MAX_RESPONSE_BYTES = 10 * 1024 * 1024

Resolver = Callable[[str, int], list[str]]


class OutboundBlocked(httpx2.TransportError):
    """A request refused by the outbound policy (host, scheme, port or address)."""


class ResponseTooLarge(httpx2.TransportError):
    pass


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def is_public_address(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%", 1)[0])
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return bool(
        addr.is_global
        and not addr.is_multicast
        and not addr.is_reserved
        and not addr.is_loopback
        and not addr.is_link_local
        and not addr.is_private
        and not addr.is_unspecified
    )


class GuardedBackend(NetworkBackend):
    """Resolves, checks every address, then connects to the checked address."""

    def __init__(self, allowlist: Allowlist, resolver: Resolver, inner: NetworkBackend | None = None) -> None:
        self.allowlist = allowlist
        self.resolver = resolver
        self.inner = inner or SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[SOCKET_OPTION] | None = None,
    ) -> NetworkStream:
        if not self.allowlist.allows(host):
            raise httpcore2.ConnectError(f"host not allowed: {host}")
        try:
            addresses = self.resolver(host, port)
        except OSError as exc:
            raise httpcore2.ConnectError(f"DNS failure for {host}") from exc
        if not addresses:
            raise httpcore2.ConnectError(f"no address for {host}")
        for ip in addresses:
            if not is_public_address(ip):
                raise httpcore2.ConnectError(f"non-public address refused for {host}")
        return self.inner.connect_tcp(addresses[0], port, timeout, local_address, socket_options)

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: typing.Iterable[SOCKET_OPTION] | None = None,
    ) -> NetworkStream:
        raise httpcore2.ConnectError("unix sockets refused")

    def sleep(self, seconds: float) -> None:
        self.inner.sleep(seconds)


class _LimitedStream(httpx2.SyncByteStream):
    def __init__(self, inner: httpx2.SyncByteStream, limit: int) -> None:
        self.inner = inner
        self.limit = limit

    def __iter__(self) -> Iterator[bytes]:
        total = 0
        for chunk in self.inner:
            total += len(chunk)
            if total > self.limit:
                self.inner.close()
                raise ResponseTooLarge("response larger than the limit")
            yield chunk

    def close(self) -> None:
        self.inner.close()


class AllowlistTransport(httpx2.HTTPTransport):
    def __init__(
        self,
        allowlist: Allowlist,
        *,
        resolver: Resolver = system_resolver,
        inner_backend: NetworkBackend | None = None,
        max_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        super().__init__(verify=True, trust_env=False, http2=False)
        self.allowlist = allowlist
        self.max_bytes = max_bytes
        self._pool.close()
        self._pool = httpcore2.ConnectionPool(
            ssl_context=httpx2.create_ssl_context(verify=True, trust_env=False),
            max_connections=10,
            http1=True,
            http2=False,
            network_backend=GuardedBackend(allowlist, resolver, inner_backend),
        )

    def check(self, request: httpx2.Request) -> None:
        url = request.url
        if url.scheme != "https":
            raise OutboundBlocked("HTTPS required", request=request)
        if url.port not in (None, 443):
            raise OutboundBlocked("port not allowed", request=request)
        if not url.host or not self.allowlist.allows(url.host):
            raise OutboundBlocked(f"host not allowed: {url.host}", request=request)
        try:
            ipaddress.ip_address(url.host)
        except ValueError:
            return
        raise OutboundBlocked("IP literals refused", request=request)

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        self.check(request)
        response = super().handle_request(request)
        length = response.headers.get("content-length")
        if length is not None and length.isdigit() and int(length) > self.max_bytes:
            response.close()
            raise ResponseTooLarge("response larger than the limit", request=request)
        if not isinstance(response.stream, httpx2.SyncByteStream):
            raise httpx2.TransportError("unexpected response stream", request=request)
        return httpx2.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_LimitedStream(response.stream, self.max_bytes),
            extensions=response.extensions,
        )
