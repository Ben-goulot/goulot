"""Layout of the installation and the updater state (docs/UPDATES.md, « Emplacements »).

`state.json`: highest version ever installed, rejected versions, consecutive failures and pause, the switches
(from which version to which, with the switch backup), the last check and its result. Written atomically, 0600.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from goulot.security.fs import ensure_private_dir, touch_private
from goulot.updater.manifest import parse_version

MAX_FAILURES = 2
VERSIONS_KEPT = 3


@dataclass(frozen=True)
class Layout:
    home: Path

    @property
    def releases(self) -> Path:
        return self.home / "releases"

    @property
    def current(self) -> Path:
        return self.home / "current"

    @property
    def state_file(self) -> Path:
        return self.home / "state.json"

    @property
    def lock_file(self) -> Path:
        return self.home / "updater.lock"

    @property
    def work(self) -> Path:
        return self.home / "update-work"

    def release_dir(self, version: str) -> Path:
        parse_version(version)
        return self.releases / version

    def current_version(self) -> str | None:
        if not self.current.is_symlink():
            return None
        return Path(os.readlink(self.current)).name

    def installed(self) -> list[str]:
        if not self.releases.exists():
            return []
        found = []
        for p in self.releases.iterdir():
            try:
                parse_version(p.name)
            except Exception:  # noqa: S112  # nosec B112 - a stray folder (partial extraction) is not a version
                continue
            if p.is_dir() and not p.is_symlink():
                found.append(p.name)
        return sorted(found, key=parse_version)

    def point_current(self, version: str) -> None:
        """Replace the `current` link atomically."""
        tmp = self.home / "current.new"
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(Path("releases") / version, tmp)
        os.replace(tmp, self.current)


@dataclass
class Switch:
    from_version: str
    to_version: str
    backup: str
    at: str


@dataclass
class State:
    highest_installed: str | None = None
    rejected: list[str] = field(default_factory=list)
    consecutive_failures: int = 0
    paused: bool = False
    switches: list[Switch] = field(default_factory=list)
    last_check: str | None = None
    last_result: str = ""
    last_install: dict[str, str] = field(default_factory=dict)
    available: str | None = None
    uv_path: str | None = None

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= MAX_FAILURES:
            self.paused = True

    def reject(self, version: str) -> None:
        if version not in self.rejected:
            self.rejected.append(version)

    def raise_highest(self, version: str) -> None:
        if self.highest_installed is None or parse_version(version) > parse_version(self.highest_installed):
            self.highest_installed = version

    def switch_into(self, version: str) -> Switch | None:
        for s in reversed(self.switches):
            if s.to_version == version:
                return s
        return None


def load_state(layout: Layout) -> State:
    if not layout.state_file.exists():
        return State()
    data: dict[str, Any] = json.loads(layout.state_file.read_text(encoding="utf-8"))
    switches = [Switch(**s) for s in data.pop("switches", [])]
    known = {f for f in State.__dataclass_fields__ if f != "switches"}
    return State(switches=switches, **{k: v for k, v in data.items() if k in known})


def save_state(layout: Layout, state: State) -> None:
    ensure_private_dir(layout.home)
    tmp = layout.home / "state.json.new"
    touch_private(tmp)
    tmp.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, layout.state_file)
