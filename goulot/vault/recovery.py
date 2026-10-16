"""Recovery key: 128 random bits in Crockford base 32 with a check symbol, shown in groups of 4.

Shown once in the Terminal by `goulot setup`, never stored in clear, never typed anywhere but the Terminal.
"""

from __future__ import annotations

import secrets

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CHECK_SYMBOLS = ALPHABET + "*~$=U"
RAW_BYTES = 16
DATA_CHARS = 26  # 130 bits: the 128 key bits followed by two zero bits
GROUP = 4


class RecoveryKeyError(ValueError):
    pass


def generate() -> bytes:
    return secrets.token_bytes(RAW_BYTES)


def encode(raw: bytes) -> str:
    if len(raw) != RAW_BYTES:
        raise ValueError("recovery key must be 128 bits")
    n = int.from_bytes(raw, "big")
    padded = n << 2
    chars = [ALPHABET[(padded >> (5 * i)) & 31] for i in reversed(range(DATA_CHARS))]
    text = "".join(chars) + CHECK_SYMBOLS[n % 37]
    return "-".join(text[i : i + GROUP] for i in range(0, len(text), GROUP))


def decode(text: str) -> bytes:
    compact = "".join(text.split()).replace("-", "").upper()
    if len(compact) != DATA_CHARS + 1:
        raise RecoveryKeyError("La clé de secours compte 27 caractères (sans les tirets).")
    data = compact[:DATA_CHARS].replace("O", "0").replace("I", "1").replace("L", "1")
    check = compact[DATA_CHARS]
    padded = 0
    for ch in data:
        idx = ALPHABET.find(ch)
        if idx < 0:
            raise RecoveryKeyError(f"Caractère inattendu : {ch}")
        padded = (padded << 5) | idx
    if padded & 3:
        raise RecoveryKeyError("Clé de secours invalide.")
    n = padded >> 2
    if CHECK_SYMBOLS[n % 37] != check:
        raise RecoveryKeyError("Le caractère de contrôle ne correspond pas : vérifiez la saisie.")
    return n.to_bytes(RAW_BYTES, "big")
