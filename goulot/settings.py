"""Application settings: paths, network binding and timing constants.

Nothing secret lives here. Paths can be redirected with GOULOT_HOME (tests, `vault recover` on a copy).
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"

DEFAULT_PORT = 8742
LOOPBACK_V4 = "127.0.0.1"
LOOPBACK_V6 = "::1"

# Session and token lifetimes, in seconds (docs/SECURITY.md).
LAUNCH_TOKEN_TTL = 60
ENROLL_GRANT_TTL = 10 * 60
SESSION_SLIDING_TTL = 7 * 24 * 3600
SESSION_ABSOLUTE_TTL = 30 * 24 * 3600
VAULT_TOKEN_SLIDING_DEFAULT = 15 * 60
VAULT_TOKEN_SLIDING_MIN = 5 * 60
VAULT_TOKEN_SLIDING_MAX = 60 * 60
VAULT_TOKEN_ABSOLUTE_TTL = 8 * 3600
WEBAUTHN_CHALLENGE_TTL = 2 * 60
FRESH_ASSERTION_TTL = 2 * 60
CLOCK_JUMP_THRESHOLD = 2 * 60
UNLOCK_MAX_FAILURES = 5
UNLOCK_FAILURE_WINDOW = 10 * 60
UNLOCK_BLOCK_DURATION = 15 * 60

AUTH_RATE_PER_MINUTE = 10
API_RATE_PER_MINUTE = 300
MAX_REQUEST_BODY = 1024 * 1024
BACKUPS_KEPT = 7


class BindError(ValueError):
    """Raised when a non-loopback address is configured."""


def _default_home() -> Path:
    return Path.home() / "Library" / "Application Support" / "Goulot"


@dataclass(frozen=True)
class Settings:
    home: Path = field(default_factory=lambda: Path(os.environ.get("GOULOT_HOME") or _default_home()))
    log_dir_override: Path | None = None
    port: int = DEFAULT_PORT
    bind_hosts: tuple[str, ...] = (LOOPBACK_V4,)
    config_dir: Path = CONFIG_DIR
    frontend_dist: Path = FRONTEND_DIST

    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "goulot.sqlite"

    @property
    def backup_dir(self) -> Path:
        return self.home / "backups"

    @property
    def log_dir(self) -> Path:
        if self.log_dir_override is not None:
            return self.log_dir_override
        if os.environ.get("GOULOT_HOME"):
            return self.home / "logs"
        return Path.home() / "Library" / "Logs" / "Goulot"

    @property
    def origin(self) -> str:
        return f"http://localhost:{self.port}"

    @property
    def allowed_hosts(self) -> frozenset[str]:
        hosts = {f"localhost:{self.port}", f"127.0.0.1:{self.port}"}
        if LOOPBACK_V6 in self.bind_hosts:
            hosts.add(f"[::1]:{self.port}")
        return frozenset(hosts)


def validate_bind_hosts(hosts: tuple[str, ...]) -> None:
    """Refuse anything but the loopback interface (docs/SECURITY.md, threat 1)."""
    if not hosts:
        raise BindError("Aucune adresse d'écoute configurée.")
    for host in hosts:
        if host not in (LOOPBACK_V4, LOOPBACK_V6):
            raise BindError(f"Adresse d'écoute refusée : {host}. Goulot n'écoute que 127.0.0.1 (et ::1).")
        if not ipaddress.ip_address(host).is_loopback:
            raise BindError(f"Adresse d'écoute refusée : {host}.")


def load_settings() -> Settings:
    port = int(os.environ.get("GOULOT_PORT", DEFAULT_PORT))
    hosts = tuple(h.strip() for h in os.environ.get("GOULOT_BIND", LOOPBACK_V4).split(",") if h.strip())
    validate_bind_hosts(hosts)
    return Settings(port=port, bind_hosts=hosts)
