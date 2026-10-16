"""Outbound host allowlist (docs/SECURITY.md, outbound network).

Hosts of the enabled sources of `config/sources.yaml` (url and extra_hosts), plus the fixed hosts below.
Exact host match: a listed domain does not open its subdomains. A new domain enters only with the user's
consent (Settings > Sources, phase 3).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit

import yaml

FIXED_HOSTS: frozenset[str] = frozenset(
    {
        "api.anthropic.com",
        "api.telegram.org",
        # Updater (phase 5): GitHub API, release pages and release asset hosts.
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)


def normalize_host(host: str) -> str:
    return host.strip().rstrip(".").lower()


class Allowlist:
    def __init__(self, hosts: Iterable[str]) -> None:
        self._hosts = frozenset(normalize_host(h) for h in hosts if h.strip())

    @property
    def hosts(self) -> frozenset[str]:
        return self._hosts

    def allows(self, host: str) -> bool:
        return normalize_host(host) in self._hosts

    def with_hosts(self, extra: Iterable[str]) -> Allowlist:
        return Allowlist(set(self._hosts) | {normalize_host(h) for h in extra})


def source_hosts(sources_file: Path) -> set[str]:
    data = yaml.safe_load(sources_file.read_text(encoding="utf-8"))
    hosts: set[str] = set()
    for source in data.get("sources", []):
        if not source.get("enabled", False):
            continue
        url = urlsplit(str(source["url"]))
        if url.scheme != "https" or not url.hostname:
            raise ValueError(f"source {source.get('id')}: HTTPS required")
        hosts.add(url.hostname)
        hosts.update(str(h) for h in source.get("extra_hosts", []) or [])
    return hosts


def load_allowlist(config_dir: Path, extra_hosts: Iterable[str] = ()) -> Allowlist:
    """`extra_hosts`: the SMTP server chosen by the user and domains approved in Settings (phase 3)."""
    return Allowlist(FIXED_HOSTS | source_hosts(config_dir / "sources.yaml") | set(extra_hosts))
