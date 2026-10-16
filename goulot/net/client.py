"""Outbound HTTP client built on the allowlist transport: 10 s to connect, 30 s to read, 5 redirects at most,
10 MB at most (also after decompression), explicit User-Agent, no environment proxy."""

from __future__ import annotations

from dataclasses import dataclass

import httpx2

from goulot import __version__
from goulot.net.allowlist import Allowlist
from goulot.net.transport import MAX_RESPONSE_BYTES, AllowlistTransport, ResponseTooLarge

TIMEOUT = httpx2.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
MAX_REDIRECTS = 5


def user_agent(contact: str | None = None) -> str:
    suffix = f"; contact: {contact}" if contact else ""
    return f"Goulot/{__version__} (veille personnelle{suffix})"


def build_client(
    allowlist: Allowlist,
    *,
    transport: httpx2.BaseTransport | None = None,
    contact: str | None = None,
) -> httpx2.Client:
    return httpx2.Client(
        transport=transport or AllowlistTransport(allowlist),
        timeout=TIMEOUT,
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
        trust_env=False,
        headers={"User-Agent": user_agent(contact)},
    )


@dataclass(frozen=True)
class FetchResult:
    status: int
    content_type: str
    content: bytes
    final_url: str


def fetch(
    client: httpx2.Client,
    url: str,
    *,
    params: dict[str, str] | None = None,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> FetchResult:
    """GET with a limit on the decoded body as well (protection against compression bombs)."""
    with client.stream("GET", url, params=params) as response:
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise ResponseTooLarge("decoded response larger than the limit")
            chunks.append(chunk)
        return FetchResult(
            status=response.status_code,
            content_type=response.headers.get("content-type", ""),
            content=b"".join(chunks),
            final_url=str(response.url),
        )
