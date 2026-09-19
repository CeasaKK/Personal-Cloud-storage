"""Token-bucket rate limiter for background I/O (scrub, rebuild)."""

from __future__ import annotations

import threading
import time


class TokenBucket:
    def __init__(self, rate_bytes_per_s: float, burst: float | None = None) -> None:
        self.rate = float(rate_bytes_per_s)
        self.capacity = burst if burst is not None else max(self.rate, 1.0)
        self.tokens = self.capacity
        self.last = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, n: int) -> None:
        """Block until n tokens are available (n may exceed the burst size)."""
        if self.rate <= 0:
            return
        with self._lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
            self.last = now
            self.tokens -= n
            deficit = -self.tokens
        if deficit > 0:
            time.sleep(deficit / self.rate)
