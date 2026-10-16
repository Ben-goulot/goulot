"""Secrets in the macOS keychain through `keyring`, service `goulot` (CLAUDE.md, rule 2).

The API never returns a secret value: only whether it is configured.
The vault master key lives in a separate item (`goulot-vault`), handled by goulot.vault.keys.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

import keyring
import keyring.errors

from goulot.security.redact import register_secret

SERVICE = "goulot"


@dataclass(frozen=True)
class SecretSpec:
    name: str
    label: str
    optional: bool


SECRETS: tuple[SecretSpec, ...] = (
    SecretSpec("anthropic", "Clé API Anthropic (espace de travail « goulot »)", False),
    SecretSpec("telegram_bot", "Jeton du bot Telegram", True),
    SecretSpec("smtp_password", "Mot de passe SMTP", True),
    SecretSpec("nasa_firms", "Clé NASA FIRMS", True),
    SecretSpec("eia", "Clé EIA", True),
    SecretSpec("comtrade", "Clé UN Comtrade", True),
    SecretSpec("bls", "Clé BLS", True),
    SecretSpec("census", "Clé des douanes américaines (Census)", True),
    SecretSpec("fred", "Clé FRED (facultative)", True),
    SecretSpec("github_token", "Jeton GitHub en lecture seule (dépôt privé seulement)", True),
)
SECRET_NAMES = frozenset(s.name for s in SECRETS)


class UnknownSecretError(KeyError):
    pass


def _check(name: str) -> None:
    if name not in SECRET_NAMES:
        raise UnknownSecretError(name)


def get_secret(name: str) -> str | None:
    _check(name)
    value = keyring.get_password(SERVICE, name)
    if value:
        register_secret(value)
    return value


def set_secret(name: str, value: str) -> None:
    _check(name)
    value = value.strip()
    if not value:
        raise ValueError("Valeur vide.")
    register_secret(value)
    keyring.set_password(SERVICE, name, value)


def delete_secret(name: str) -> None:
    _check(name)
    with contextlib.suppress(keyring.errors.PasswordDeleteError):
        keyring.delete_password(SERVICE, name)


def has_secret(name: str) -> bool:
    return bool(get_secret(name))


def secrets_status() -> dict[str, str]:
    """Public view of the secrets: « configuré » or « absent », never a value."""
    return {s.name: ("configuré" if has_secret(s.name) else "absent") for s in SECRETS}


def masked(value: str) -> str:
    """At most the last 4 characters (`goulot setup` confirmation)."""
    return "…" + value[-4:] if len(value) > 8 else "…"
