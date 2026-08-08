"""
Generic rate-limit-aware HTTP client using token bucket algorithm.

Tracks rate limits via Redis counters and enforces per-component budgets.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict
from typing import Any

from src.config.loader import get_config
from src.data.redis_client import check_post_rate_limit, check_rate_limit


class TokenBucket:
    """In-memory token bucket for rate limiting."""

    def __init__(self, rate: float, burst: int) -> None:
        self.rate = rate  # tokens per second
        self.burst = burst
        self.tokens = float(burst)
        self.last_update = time.monotonic()

    async def acquire(self) -> bool:
        """Try to acquire one token. Returns True if successful."""
        now = time.monotonic()
        elapsed = now - self.last_update
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
        self.last_update = now

        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True

        # Calculate wait time
        wait = (1.0 - self.tokens) / self.rate
        if wait < 0.01:  # Don't sleep for tiny amounts
            await asyncio.sleep(wait)
            self.tokens = 0.0
            self.last_update = time.monotonic()
            return True

        return False

    async def wait_and_acquire(self, timeout: float = 10.0) -> bool:
        """Wait until a token is available, with timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self.acquire():
                return True
            await asyncio.sleep(0.05)
        return False


class RateLimiter:
    """
    Rate limiter that enforces both global and per-endpoint limits.

    Uses:
      - Redis counters for global rate limit tracking
      - In-memory token buckets for local throttling
      - Jitter to avoid thundering herd
    """

    def __init__(self) -> None:
        config = get_config()

        # Token buckets per component
        self._buckets: dict[str, TokenBucket] = {
            "clob_rest": TokenBucket(rate=50, burst=100),   # ~500/10s
            "clob_post": TokenBucket(rate=20, burst=35),     # ~200/10s
            "gamma": TokenBucket(rate=1, burst=3),            # ~10/10s
            "order_cancel": TokenBucket(rate=10, burst=20),   # ~100/10s
        }

        # Redis-based rate limit windows
        self._redis_windows: dict[str, int] = {
            "10s": 15000,   # Global: 15,000/10s
            "post_1s": 350,  # POST burst: ~350/s
        }

    async def acquire(self, component: str) -> bool:
        """
        Acquire rate limit permission for a component.

        Checks both Redis global counter and local token bucket.
        """
        # Check Redis global limit
        if component in ("clob_rest", "clob_post", "gamma", "order_cancel"):
            redis_ok = await check_rate_limit("10s", self._redis_windows["10s"])
            if not redis_ok:
                return False

        # If posting, also check POST burst limit
        if component == "clob_post":
            post_ok = await check_post_rate_limit("1s", self._redis_windows["post_1s"])
            if not post_ok:
                return False

        # Check local token bucket
        bucket = self._buckets.get(component)
        if bucket is not None:
            return await bucket.acquire()

        return True

    async def wait_and_acquire(self, component: str, timeout: float = 10.0) -> bool:
        """Wait for rate limit permission with timeout and jitter."""
        bucket = self._buckets.get(component)
        if bucket is not None:
            return await bucket.wait_and_acquire(timeout)
        return await self.acquire(component)

    def add_jitter(self, base_delay: float, max_jitter: float = 1.0) -> float:
        """Add random jitter to a delay to avoid thundering herd."""
        return base_delay + random.uniform(0, max_jitter)


# Singleton
_rate_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    """Get the singleton rate limiter."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter()
    return _rate_limiter
