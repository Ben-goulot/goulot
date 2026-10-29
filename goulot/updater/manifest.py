"""Release manifest: strict parsing, Ed25519 signature, version rules (docs/UPDATES.md, step 2)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from goulot.updater import UPDATER_PROTOCOL

MAX_MANIFEST_BYTES = 64 * 1024
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
FIELDS = {"name", "version", "commit", "created_at", "sha256", "size", "min_updater_version", "migrations",
          "notes_fr"}
_VERSION = re.compile(r"^(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,4})$")


class UpdateRefused(Exception):
    """A release that must not be installed. The message is in French: it reaches the user."""


Version = tuple[int, int, int]


def parse_version(text: str) -> Version:
    m = _VERSION.match(text)
    if not m:
        raise UpdateRefused(f"numéro de version invalide : {text[:20]!r}")
    return int(m[1]), int(m[2]), int(m[3])


def key_id(raw_public_key: bytes) -> str:
    return hashlib.sha256(raw_public_key).hexdigest()[:16]


@dataclass(frozen=True)
class Manifest:
    name: str
    version: str
    commit: str
    created_at: str
    sha256: str
    size: int
    min_updater_version: int
    migrations: tuple[str, ...]
    notes_fr: str

    @property
    def archive_name(self) -> str:
        return f"goulot-{self.version}.tar.gz"


def parse_manifest(raw: bytes) -> Manifest:
    if len(raw) > MAX_MANIFEST_BYTES:
        raise UpdateRefused("manifeste trop volumineux")
    try:
        data: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateRefused("manifeste illisible") from exc
    if not isinstance(data, dict) or set(data) != FIELDS:
        raise UpdateRefused("manifeste : champs inattendus")
    ok = (
        data["name"] == "goulot"
        and isinstance(data["version"], str)
        and isinstance(data["commit"], str) and re.fullmatch(r"[0-9a-f]{40}", data["commit"]) is not None
        and isinstance(data["created_at"], str) and len(data["created_at"]) <= 40
        and isinstance(data["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", data["sha256"]) is not None
        and type(data["size"]) is int and 0 < data["size"] <= MAX_ARCHIVE_BYTES
        and type(data["min_updater_version"]) is int and data["min_updater_version"] >= 1
        and isinstance(data["migrations"], list)
        and all(isinstance(m, str) and re.fullmatch(r"\d{4}_[a-z0-9_]+\.sql", m) for m in data["migrations"])
        and isinstance(data["notes_fr"], str) and len(data["notes_fr"]) <= 20_000
    )
    if not ok:
        raise UpdateRefused("manifeste : valeur invalide")
    parse_version(data["version"])
    return Manifest(data["name"], data["version"], data["commit"], data["created_at"], data["sha256"],
                    data["size"], data["min_updater_version"], tuple(data["migrations"]), data["notes_fr"])


def verify_signature(raw_manifest: bytes, signature: bytes, keys: Iterable[tuple[str, str]]) -> str:
    """Return the id of the trusted key that signed the manifest, or refuse."""
    if len(signature) != 64:
        raise UpdateRefused("signature absente ou mal formée")
    for kid, b64 in keys:
        try:
            public = Ed25519PublicKey.from_public_bytes(base64.b64decode(b64, validate=True))
        except (ValueError, binascii.Error):
            continue
        try:
            public.verify(signature, raw_manifest)
        except InvalidSignature:
            continue
        return kid
    raise UpdateRefused("signature invalide ou clé inconnue")


def check_installable(manifest: Manifest, *, highest_installed: str | None, rejected: Iterable[str]) -> None:
    """Strictly above the highest version ever installed, never a rejected one, a compatible updater."""
    version = parse_version(manifest.version)
    if manifest.version in set(rejected):
        raise UpdateRefused(f"la version {manifest.version} a déjà été écartée (retour arrière ou test de santé "
                            "raté) : seule une version plus récente s'installera")
    if highest_installed is not None and version <= parse_version(highest_installed):
        raise UpdateRefused(f"la version {manifest.version} n'est pas plus récente que {highest_installed}")
    if manifest.min_updater_version > UPDATER_PROTOCOL:
        raise UpdateRefused("cette version demande une réinstallation : « goulot dev bootstrap »")


def check_archive(manifest: Manifest, archive: bytes) -> None:
    if len(archive) != manifest.size:
        raise UpdateRefused("taille de l'archive différente du manifeste")
    if hashlib.sha256(archive).hexdigest() != manifest.sha256:
        raise UpdateRefused("empreinte de l'archive différente du manifeste")
