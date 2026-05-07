"""Async token bucket — keeps Lichess polling under their soft limits."""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import Deque, Dict


class TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: float):
        self.rate = float(rate_per_sec)
        self.capacity = float(capacity)
        self._tokens = float(capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, n: float = 1.0) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity,
                    self._tokens + (now - self._updated) * self.rate,
                )
                self._updated = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                wait = (n - self._tokens) / self.rate
            await asyncio.sleep(wait)


# Lichess docs: be polite. We stay well under 1 req/s on average.
LICHESS_BUCKET = TokenBucket(rate_per_sec=1.0, capacity=3)


class SlidingWindow:
    """Per-key sliding-window counter for abuse mitigation.

    In-memory (single-process) — fine for our one-uvicorn-worker setup.
    Old timestamps are pruned on every check so it self-cleans.
    """

    def __init__(self, max_hits: int, window_seconds: float):
        self.max = max_hits
        self.window = window_seconds
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window
        async with self._lock:
            dq = self._hits[key]
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self.max:
                return False
            dq.append(now)
            # Opportunistic GC: drop empty entries to avoid unbounded growth.
            if len(self._hits) > 10_000:
                for k in list(self._hits.keys()):
                    if not self._hits[k]:
                        del self._hits[k]
            return True


# /recover: keep both per-IP and per-email limits.
# Numbers chosen for a 1-person support load: a real user retrying a few times
# is fine; a bot hammering the endpoint is not.
RECOVER_PER_IP    = SlidingWindow(max_hits=8,  window_seconds=3600)   # 8 / hour / IP
RECOVER_PER_EMAIL = SlidingWindow(max_hits=3,  window_seconds=3600)   # 3 / hour / email
CHECKOUT_PER_IP   = SlidingWindow(max_hits=20, window_seconds=3600)   # 20 / hour / IP
