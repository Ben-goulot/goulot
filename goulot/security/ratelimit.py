"""Sliding-window rate limits: 10/min on authentication, 300/min on the API."""

from __future__ import annotations

import threading
from collections import deque

from goulot.security.clock import Clock


class RateLimiter:
    def __init__(self, clock: Clock, limits: dict[str, int], window: float = 60.0) -> None:
        self.clock = clock
        self.limits = limits
        self.window = window
        self._hits: dict[str, deque[float]] = {name: deque() for name in limits}
        self._lock = threading.Lock()

    def allow(self, bucket: str) -> bool:
        now = self.clock.monotonic()
        with self._lock:
            hits = self._hits[bucket]
            while hits and hits[0] <= now - self.window:
                hits.popleft()
            if len(hits) >= self.limits[bucket]:
                return False
            hits.append(now)
            return True
