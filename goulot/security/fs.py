"""Private directories (0700) and files (0600) (docs/SECURITY.md, threat 17)."""

from __future__ import annotations

import os
from pathlib import Path

DIR_MODE = 0o700
FILE_MODE = 0o600


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    os.chmod(path, DIR_MODE)
    return path


def make_private(path: Path) -> None:
    if path.exists():
        os.chmod(path, FILE_MODE)


def touch_private(path: Path) -> None:
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, FILE_MODE)
    os.close(fd)
    os.chmod(path, FILE_MODE)
