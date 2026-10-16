"""Wall and monotonic clocks, injectable in tests.

On macOS, `time.monotonic()` stops while the Mac sleeps: a gap between the two clocks reveals a sleep,
which closes the vault (docs/SECURITY.md, vault token).
"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def time(self) -> float: ...

    def monotonic(self) -> float: ...


class SystemClock:
    def time(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()


class ManualClock:
    """Test clock. `advance` moves both clocks; `sleep_jump` moves only the wall clock."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self._wall = start
        self._mono = 1000.0

    def time(self) -> float:
        return self._wall

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._wall += seconds
        self._mono += seconds

    def sleep_jump(self, seconds: float) -> None:
        self._wall += seconds
