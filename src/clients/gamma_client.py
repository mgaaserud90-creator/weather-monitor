"""
Gamma API async client — fetches market metadata, events, tokens, and categories.

Rate-limit aware and uses httpx for async HTTP with retry logic.
"""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
)

from src.clients.rate_limiter import get_rate_limiter
from src.config.loader import get_config
from src.core.exceptions import GammaRateLimitError


class GammaClient:
    """Async HTTP client for the Polymarket Gamma API."""

    def __init__(self) -> None:
        config = get_config()
        self._base_url = config.gamma.api_url.rstrip("/")
        self._timeout = httpx.Timeout(config.gamma.request_timeout_sec)
        self._rate_limiter = get_rate_limiter()
        self._client: httpx.AsyncClient | None = None

    async def initialize(self) -> None:
        """Initialize the HTTP client."""
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers={
                "Accept": "application/json",
                "User-Agent": "PolymarketArbBot/1.0",
            },
        )

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("GammaClient not initialized. Call initialize() first.")
        return self._client

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make a rate-limited GET request."""
        ok = await self._rate_limiter.wait_and_acquire("gamma", timeout=30.0)
        if not ok:
            raise GammaRateLimitError("Gamma API rate limit exceeded")

        response = await self.client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    # =========================================================================
    # Market Discovery Endpoints
    # =========================================================================

    async def get_events(
        self,
        limit: int = 500,
        offset: int = 0,
        active: bool = True,
    ) -> list[dict[str, Any]]:
        """Fetch events from Gamma API."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if active:
            params["active"] = "true"
        return await self._get("/events", params)

    async def get_event(self, event_id: str) -> dict[str, Any]:
        """Fetch a single event by ID."""
        return await self._get(f"/events/{event_id}")

    async def get_events_by_slug(self, slug: str) -> list[dict[str, Any]]:
        """Fetch events by slug. Returns list of matching events."""
        return await self._get("/events", {"slug": slug})

    async def get_markets(
        self,
        limit: int = 500,
        offset: int = 0,
        active: bool = True,
        closed: bool = False,
        order: str | None = None,
        ascending: bool | None = None,
        liquidity_min: float | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch markets from Gamma API.

        Args:
            limit: Page size.
            offset: Pagination offset.
            active: Only active markets.
            closed: Include closed markets.
            order: Field to order by (e.g. ``liquidityNum``).
            ascending: Sort direction (False = descending).
            liquidity_min: Minimum ``liquidityNum`` filter
                (sent as ``liquidity_num_min``).
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if active:
            params["active"] = "true"
        if closed:
            params["closed"] = "true"
        if order:
            params["order"] = order
        if ascending is not None:
            params["ascending"] = "true" if ascending else "false"
        if liquidity_min is not None:
            params["liquidity_num_min"] = liquidity_min
        return await self._get("/markets", params)

    async def get_markets_by_event(self, event_id: str) -> list[dict[str, Any]]:
        """Fetch all markets for a given event."""
        return await self._get(f"/events/{event_id}/markets")

    async def get_all_markets_paginated(self, page_size: int = 500) -> list[dict[str, Any]]:
        """Fetch all active markets using pagination."""
        all_markets: list[dict[str, Any]] = []
        offset = 0
        while True:
            batch = await self.get_markets(limit=page_size, offset=offset, active=True)
            if not batch:
                break
            all_markets.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size
        return all_markets

    async def get_categories(self) -> list[dict[str, Any]]:
        """Fetch all categories."""
        return await self._get("/categories")

    async def get_tokens(
        self,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Fetch tokens from Gamma API."""
        return await self._get("/tokens", {"limit": limit, "offset": offset})

    # =========================================================================
    # Resolution & Settlement
    # =========================================================================

    async def get_market_resolution(self, market_id: str) -> dict[str, Any] | None:
        """Get resolution status for a market."""
        try:
            return await self._get(f"/markets/{market_id}/resolution")
        except Exception:
            return None

    async def get_resolutions_batch(self, market_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Get resolutions for multiple markets."""
        results: dict[str, dict[str, Any]] = {}
        for mid in market_ids:
            resolution = await self.get_market_resolution(mid)
            if resolution:
                results[mid] = resolution
        return results


# Singleton
_gamma_client: GammaClient | None = None


def get_gamma_client() -> GammaClient:
    """Get the singleton Gamma API client."""
    global _gamma_client
    if _gamma_client is None:
        _gamma_client = GammaClient()
    return _gamma_client
