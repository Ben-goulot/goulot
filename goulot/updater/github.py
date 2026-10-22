"""Latest release of the repository, through the allowlist HTTP client (docs/UPDATES.md, step 1).

Public repository: no token. The archive is downloaded only after the manifest's signature has been checked.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx2

from goulot.net.allowlist import FIXED_HOSTS, Allowlist
from goulot.net.client import build_client, fetch
from goulot.net.transport import AllowlistTransport
from goulot.updater.manifest import MAX_ARCHIVE_BYTES, MAX_MANIFEST_BYTES, UpdateRefused
from goulot.updater.trust import REPOSITORY

API = "https://api.github.com"
GITHUB_HOSTS = frozenset(h for h in FIXED_HOSTS if "github" in h)


class FetchError(Exception):
    """Network trouble: not a fault of the release, the next check tries again."""


@dataclass(frozen=True)
class ReleaseAssets:
    tag: str
    manifest_url: str
    signature_url: str
    archive_url: str
    archive_name: str


def updater_client(transport: httpx2.BaseTransport | None = None) -> httpx2.Client:
    """GitHub hosts only, with room for a release archive."""
    allowlist = Allowlist(GITHUB_HOSTS)
    return build_client(allowlist, transport=transport or AllowlistTransport(allowlist, max_bytes=MAX_ARCHIVE_BYTES))


def _get(client: httpx2.Client, url: str, max_bytes: int, headers: dict[str, str] | None = None) -> bytes:
    try:
        result = fetch(client, url, headers=headers, max_bytes=max_bytes)
    except httpx2.HTTPError as exc:
        raise FetchError(f"{type(exc).__name__}") from exc
    if result.status != 200:
        raise FetchError(f"HTTP {result.status}")
    return result.content


def latest_release(client: httpx2.Client, repository: str = REPOSITORY) -> ReleaseAssets:
    raw = _get(client, f"{API}/repos/{repository}/releases/latest", 1024 * 1024,
               {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        data: Any = json.loads(raw)
        tag = str(data["tag_name"])
        assets = {str(a["name"]): str(a["browser_download_url"]) for a in data["assets"]}
    except (ValueError, KeyError, TypeError) as exc:
        raise FetchError("réponse de GitHub illisible") from exc
    version = tag.removeprefix("v")
    name = f"goulot-{version}.tar.gz"
    needed = ("manifest.json", "manifest.json.sig", name)
    if not all(n in assets for n in needed):
        raise UpdateRefused(f"release {tag[:20]} incomplète")
    for n in needed:
        if not assets[n].startswith(f"https://github.com/{repository}/releases/download/"):
            raise UpdateRefused("adresse de téléchargement inattendue")
    return ReleaseAssets(tag, assets["manifest.json"], assets["manifest.json.sig"], assets[name], name)


def download_manifest(client: httpx2.Client, assets: ReleaseAssets) -> tuple[bytes, bytes]:
    return _get(client, assets.manifest_url, MAX_MANIFEST_BYTES), _get(client, assets.signature_url, 1024)


def download_archive(client: httpx2.Client, assets: ReleaseAssets, size: int) -> bytes:
    return _get(client, assets.archive_url, min(size, MAX_ARCHIVE_BYTES))
