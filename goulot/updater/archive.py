"""Safe extraction of a release archive (docs/UPDATES.md, step 3).

Only regular files and directories, under relative paths without `..`: absolute paths, links (symbolic or hard)
and special files are refused before anything is written. Limits on the number of members and on the extracted
size. Directories 0700, files 0600, created exclusively (never through an existing link).
"""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path, PurePosixPath

from goulot.security.fs import DIR_MODE, FILE_MODE
from goulot.updater.manifest import UpdateRefused

MAX_MEMBERS = 20_000
MAX_EXTRACTED_BYTES = 600 * 1024 * 1024


def _safe_parts(name: str) -> tuple[str, ...]:
    if not name or "\x00" in name or "\\" in name or name.startswith("/"):
        raise UpdateRefused(f"archive : chemin refusé ({name[:80]!r})")
    parts = tuple(p for p in PurePosixPath(name).parts if p not in ("", "."))
    if not parts or any(p == ".." for p in parts):
        raise UpdateRefused(f"archive : chemin refusé ({name[:80]!r})")
    return parts


def check_members(members: list[tarfile.TarInfo]) -> list[tuple[tuple[str, ...], tarfile.TarInfo]]:
    if len(members) > MAX_MEMBERS:
        raise UpdateRefused("archive : trop de fichiers")
    seen: set[tuple[str, ...]] = set()
    total = 0
    checked = []
    for m in members:
        parts = _safe_parts(m.name)
        if not (m.isreg() or m.isdir()):
            raise UpdateRefused(f"archive : lien ou fichier spécial refusé ({m.name[:80]!r})")
        if parts in seen:
            raise UpdateRefused(f"archive : chemin en double ({m.name[:80]!r})")
        seen.add(parts)
        total += m.size
        if total > MAX_EXTRACTED_BYTES:
            raise UpdateRefused("archive : contenu trop volumineux")
        checked.append((parts, m))
    return checked


def extract(archive: bytes, dest: Path) -> None:
    """Extract into `dest`, which must not exist yet."""
    try:
        tar = tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz")  # noqa: SIM115 - closed by the with below
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise UpdateRefused("archive illisible") from exc
    with tar:
        try:
            members = check_members(tar.getmembers())
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise UpdateRefused("archive illisible") from exc
        dest.mkdir(mode=DIR_MODE)
        os.chmod(dest, DIR_MODE)
        for parts, member in members:
            target = dest.joinpath(*parts)
            for i in range(1, len(parts)):
                parent = dest.joinpath(*parts[:i])
                if not parent.exists():
                    parent.mkdir(mode=DIR_MODE)
                elif parent.is_symlink() or not parent.is_dir():
                    raise UpdateRefused("archive : chemin refusé")
            if member.isdir():
                if not target.exists():
                    target.mkdir(mode=DIR_MODE)
                continue
            source = tar.extractfile(member)
            if source is None:
                raise UpdateRefused("archive illisible")
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE)
            with os.fdopen(fd, "wb") as out:
                remaining = member.size
                while remaining:
                    chunk = source.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        raise UpdateRefused("archive tronquée")
                    out.write(chunk)
                    remaining -= len(chunk)
