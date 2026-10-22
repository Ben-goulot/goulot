"""Trusted release signing keys and the release repository (docs/UPDATES.md, « Clé de signature »).

Only the keys below, shipped in signed releases, are trusted: no file on disk can add one. `goulot dev keygen`
writes the public key here; a rotation ships the old and the new key together, then a later release drops the old
one. Raw Ed25519 public keys, base64.
"""

from __future__ import annotations

REPOSITORY = "Ben-goulot/goulot"

# (key id, raw Ed25519 public key in base64). The key id is the first 16 hex digits of its SHA-256.
TRUSTED_KEYS: tuple[tuple[str, str], ...] = (
    # BEGIN TRUSTED KEYS (written by `goulot dev keygen`)
    # END TRUSTED KEYS
)
