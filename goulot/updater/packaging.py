"""Release build, run by the `build` job of `.github/workflows/release.yml` (no secret there).

`python -m goulot.updater.packaging version --bump patch` : next version number.
`python -m goulot.updater.packaging archive --version X.Y.Z --out dist` : deterministic `goulot-X.Y.Z.tar.gz`
(tracked code, lock files, prompts, schemas, config, LaunchAgent templates, the built frontend), and `meta.json`.
`python -m goulot.updater.packaging notes --since vX.Y.Z` : `notes_fr`, written by script, without AI: commit
titles since the last release and the libraries changed in the lock files.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import subprocess  # nosec B404 - git only, argument lists
import sys
import tarfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from goulot.updater import UPDATER_PROTOCOL
from goulot.updater.manifest import Version, parse_version

ROOT = Path(__file__).resolve().parent.parent.parent
TRACKED_PREFIXES = ("goulot/", "prompts/", "schemas/", "config/", "ops/launchd/")
TRACKED_FILES = ("pyproject.toml", "uv.lock", ".python-version")
BUILT_DIRS = ("frontend/dist",)
VERSION_LINE = re.compile(r'^__version__ = "[^"]*"$', re.MULTILINE)


def _git(*args: str, root: Path = ROOT) -> str:
    out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)  # noqa: S603, S607  # nosec B603 B607
    return out.stdout


def code_version(root: Path = ROOT) -> str:
    m = re.search(r'^__version__ = "([^"]+)"$', (root / "goulot" / "__init__.py").read_text(), re.MULTILINE)
    if not m:
        raise SystemExit("__version__ introuvable")
    return m[1]


def release_tags(root: Path = ROOT) -> list[str]:
    tags = []
    for tag in _git("tag", "--list", "v*", root=root).split():
        try:
            parse_version(tag[1:])
        except Exception:  # noqa: S112  # nosec B112 - not a release tag
            continue
        tags.append(tag)
    return sorted(tags, key=lambda t: parse_version(t[1:]))


def bump(version: Version, kind: str) -> Version:
    major, minor, patch = version
    if kind == "major":
        return major + 1, 0, 0
    if kind == "minor":
        return major, minor + 1, 0
    return major, minor, patch + 1


def next_version(last_tag: str | None, kind: str, code: str) -> str:
    """The code's own version for the first release, or when it was raised by hand; otherwise the bump."""
    if last_tag is None:
        return code
    last = parse_version(last_tag.removeprefix("v"))
    candidate = bump(last, kind)
    wanted = parse_version(code)
    chosen = wanted if wanted > last and wanted > candidate else candidate
    return ".".join(map(str, chosen))


def archive_members(root: Path = ROOT) -> list[str]:
    tracked = [p for p in _git("ls-files", "-z", root=root).split("\0") if p]
    files = [p for p in tracked if p.startswith(TRACKED_PREFIXES) or p in TRACKED_FILES]
    for folder in BUILT_DIRS:
        base = root / folder
        if not (base / "index.html").exists():
            raise SystemExit(f"{folder} absent : construisez le frontend d'abord")
        files += [str(p.relative_to(root)) for p in base.rglob("*") if p.is_file()]
    return sorted(set(files))


def build_archive(version: str, out_dir: Path, root: Path = ROOT, mtime: int | None = None) -> dict[str, Any]:
    parse_version(version)
    commit = _git("rev-parse", "HEAD", root=root).strip()
    stamp = mtime if mtime is not None else int(_git("show", "-s", "--format=%ct", "HEAD", root=root).strip())
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w",
                                                                               format=tarfile.PAX_FORMAT) as tar:
        for name in archive_members(root):
            data = (root / name).read_bytes()
            if name == "goulot/__init__.py":
                data = VERSION_LINE.sub(f'__version__ = "{version}"', data.decode("utf-8"), count=1).encode()
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), stamp, 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    blob = buffer.getvalue()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"goulot-{version}.tar.gz").write_bytes(blob)
    meta = {
        "version": version,
        "commit": commit,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "size": len(blob),
        "min_updater_version": UPDATER_PROTOCOL,
        "migrations": sorted(p.name for p in (root / "goulot" / "db" / "migrations").glob("*.sql")),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def _uv_packages(text: str) -> dict[str, str]:
    data = tomllib.loads(text) if text else {}
    return {p["name"]: p.get("version", "") for p in data.get("package", []) if "version" in p}


def _npm_packages(text: str) -> dict[str, str]:
    data = json.loads(text) if text else {}
    return {k.removeprefix("node_modules/"): v.get("version", "") for k, v in data.get("packages", {}).items()
            if k.startswith("node_modules/") and "/node_modules/" not in k}


def _changes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    lines = []
    for name in sorted(before.keys() | after.keys()):
        old, new = before.get(name), after.get(name)
        if old == new:
            continue
        if old is None:
            lines.append(f"- {name} {new} (ajoutée)")
        elif new is None:
            lines.append(f"- {name} {old} (retirée)")
        else:
            lines.append(f"- {name} {old} → {new}")
    return lines


def _show(ref: str, path: str, root: Path) -> str:
    try:
        return _git("show", f"{ref}:{path}", root=root)
    except subprocess.CalledProcessError:
        return ""


def release_notes(since: str | None, root: Path = ROOT) -> str:
    span = f"{since}..HEAD" if since else "HEAD"
    titles = [t for t in _git("log", "--no-merges", "--format=%s", span, root=root).splitlines() if t.strip()]
    if not since:
        titles = titles[:30]
    lines = ["Changements :", *(f"- {t[:200]}" for t in titles[:100])]
    if since:
        libs = _changes(_uv_packages(_show(since, "uv.lock", root)), _uv_packages(_show("HEAD", "uv.lock", root)))
        libs += _changes(_npm_packages(_show(since, "frontend/package-lock.json", root)),
                         _npm_packages(_show("HEAD", "frontend/package-lock.json", root)))
        lines += ["", "Bibliothèques changées :", *(libs or ["- aucune"])]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="packaging")
    sub = parser.add_subparsers(dest="action", required=True)
    v = sub.add_parser("version")
    v.add_argument("--bump", choices=("patch", "minor", "major"), default="patch")
    a = sub.add_parser("archive")
    a.add_argument("--version", required=True)
    a.add_argument("--out", required=True)
    n = sub.add_parser("notes")
    n.add_argument("--since", default="")
    args = parser.parse_args(argv)
    if args.action == "version":
        tags = release_tags()
        print(next_version(tags[-1] if tags else None, args.bump, code_version()))
    elif args.action == "archive":
        print(json.dumps(build_archive(args.version, Path(args.out))))
    else:
        sys.stdout.write(release_notes(args.since or None))


if __name__ == "__main__":
    main()
