"""`goulot dev keygen` and `goulot dev release` (docs/UPDATES.md, « Clé de signature »). Run by the user in their
own Terminal, outside Claude Code's sandbox.

keygen: a new Ed25519 pair; the private key goes, without ever being shown, to the GitHub environment secret
`GOULOT_SIGNING_KEY` (stdin of `gh secret set`) and to a separate keychain item `goulot-signing`, created with no
trusted application (`-T ""`: every read asks for the macOS password; through the stdin of `security -i`, never on
a command line); the public key is written into `goulot/updater/trust.py`.

Rotation in two steps, so that every release is signed by a key the installed version trusts: `--rotate` prepares
a new key (keychain only) and trusts it beside the current one; once a release shipping both keys is installed,
`--activate` hands the new key to GitHub. A later release drops the old key from trust.py.
"""

from __future__ import annotations

import base64
import re
import subprocess  # nosec B404 - fixed programs, argument lists, no shell
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from goulot.updater.manifest import key_id

SIGNING_SERVICE = "goulot-signing"
SIGNING_ACCOUNT = "release-signing"
NEXT_ACCOUNT = "release-signing-next"
SECRET_NAME = "GOULOT_SIGNING_KEY"  # noqa: S105  # nosec B105 - the name of the GitHub secret, not its value
ENVIRONMENT = "release"
SECURITY = "/usr/bin/security"
GH_CANDIDATES = ("/opt/homebrew/bin/gh", "/usr/local/bin/gh")
BEGIN = "    # BEGIN TRUSTED KEYS (written by `goulot dev keygen`)\n"
END = "    # END TRUSTED KEYS\n"

Runner = Callable[[list[str], str | None], subprocess.CompletedProcess[str]]


class KeygenError(Exception):
    """French message for the user; nothing has been left half-done that a new run cannot redo."""


def run(args: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=120, check=False)  # noqa: S603  # nosec B603


@dataclass(frozen=True)
class KeyPair:
    private_pem: str
    private_raw: bytes
    public_b64: str
    key_id: str


def generate() -> KeyPair:
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode("ascii")
    raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return KeyPair(pem, raw, base64.b64encode(public).decode("ascii"), key_id(public))


def read_trusted(trust_file: Path) -> list[tuple[str, str]]:
    text = trust_file.read_text(encoding="utf-8")
    block = text[text.index(BEGIN) + len(BEGIN):text.index(END)]
    return re.findall(r'\("([0-9a-f]{16})", "([A-Za-z0-9+/=]{44})"\)', block)


def write_trusted(trust_file: Path, keys: list[tuple[str, str]]) -> None:
    text = trust_file.read_text(encoding="utf-8")
    head, tail = text[:text.index(BEGIN) + len(BEGIN)], text[text.index(END):]
    lines = "".join(f'    ("{kid}", "{b64}"),\n' for kid, b64 in keys)
    trust_file.write_text(head + lines + tail, encoding="utf-8")


def keychain_add_command(account: str, raw_private: bytes) -> str:
    """One line for `security -i`: the key as hexadecimal text, no application trusted (-T "")."""
    return (f'add-generic-password -a {account} -s {SIGNING_SERVICE} '
            f'-l "Goulot : cle de signature des mises a jour" -T "" -w {raw_private.hex()}\n')


def _item_exists(runner: Runner, account: str) -> bool:
    return runner([SECURITY, "find-generic-password", "-s", SIGNING_SERVICE, "-a", account], None).returncode == 0


def _delete_item(runner: Runner, account: str) -> None:
    runner([SECURITY, "delete-generic-password", "-s", SIGNING_SERVICE, "-a", account], None)


def _pem(raw_private: bytes) -> str:
    return Ed25519PrivateKey.from_private_bytes(raw_private).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode("ascii")


def _check_github(runner: Runner, gh: str, repository: str) -> None:
    if runner([gh, "auth", "status"], None).returncode != 0:
        raise KeygenError("gh n'est pas connecté à GitHub : lancez « gh auth login »")
    if runner([gh, "api", f"repos/{repository}/environments/{ENVIRONMENT}"], None).returncode != 0:
        raise KeygenError(f"l'environnement « {ENVIRONMENT} » n'existe pas encore sur GitHub : créez-le d'abord")


def _send_secret(runner: Runner, gh: str, repository: str, pem: str) -> bool:
    sent = runner([gh, "secret", "set", SECRET_NAME, "--env", ENVIRONMENT, "--repo", repository], pem)
    return sent.returncode == 0


def keygen(trust_file: Path, repository: str, gh: str, *, runner: Runner = run) -> KeyPair:
    """First key: keychain copy, GitHub secret, public key in trust.py."""
    if read_trusted(trust_file):
        raise KeygenError("une clé de confiance existe déjà : « --rotate » pour en préparer une nouvelle")
    _check_github(runner, gh, repository)
    if _item_exists(runner, SIGNING_ACCOUNT):
        raise KeygenError("un élément « goulot-signing » existe déjà dans le trousseau : supprimez-le dans "
                          "Trousseaux d'accès s'il ne sert plus, puis relancez")
    pair = generate()
    if runner([SECURITY, "-i"], keychain_add_command(SIGNING_ACCOUNT, pair.private_raw)).returncode != 0:
        raise KeygenError("copie de secours dans le trousseau impossible : rien n'a été envoyé à GitHub")
    if not _send_secret(runner, gh, repository, pair.private_pem):
        _delete_item(runner, SIGNING_ACCOUNT)
        raise KeygenError("envoi du secret à GitHub impossible : rien n'a été gardé, relancez la commande")
    write_trusted(trust_file, [(pair.key_id, pair.public_b64)])
    return pair


def prepare_rotation(trust_file: Path, *, runner: Runner = run) -> KeyPair:
    """Rotation, step 1: a new key, kept in the keychain only, trusted beside the current one. The next release is
    still signed by the current key, and ships both."""
    existing = read_trusted(trust_file)
    if not existing:
        raise KeygenError("aucune clé actuelle : lancez « goulot dev keygen » sans option")
    if _item_exists(runner, NEXT_ACCOUNT):
        raise KeygenError("une rotation est déjà préparée : « goulot dev keygen --activate »")
    pair = generate()
    if runner([SECURITY, "-i"], keychain_add_command(NEXT_ACCOUNT, pair.private_raw)).returncode != 0:
        raise KeygenError("écriture dans le trousseau impossible : rien n'a changé")
    write_trusted(trust_file, [*existing, (pair.key_id, pair.public_b64)])
    return pair


def activate_rotation(repository: str, gh: str, *, runner: Runner = run) -> None:
    """Rotation, step 2, once a release trusting both keys is installed: the new key signs from now on. Reading it
    from the keychain asks for the macOS password."""
    _check_github(runner, gh, repository)
    out = runner([SECURITY, "find-generic-password", "-s", SIGNING_SERVICE, "-a", NEXT_ACCOUNT, "-w"], None)
    if out.returncode != 0 or not re.fullmatch(r"[0-9a-f]{64}", out.stdout.strip()):
        raise KeygenError("nouvelle clé introuvable dans le trousseau : « goulot dev keygen --rotate » d'abord")
    raw = bytes.fromhex(out.stdout.strip())
    if not _send_secret(runner, gh, repository, _pem(raw)):
        raise KeygenError("envoi du secret à GitHub impossible : rien n'a changé, relancez la commande")
    _delete_item(runner, SIGNING_ACCOUNT)
    if runner([SECURITY, "-i"], keychain_add_command(SIGNING_ACCOUNT, raw)).returncode == 0:
        _delete_item(runner, NEXT_ACCOUNT)


def find_gh() -> str | None:
    for candidate in GH_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None
