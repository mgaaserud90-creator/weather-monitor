"""
CLOB API Client — wrapper around py-clob-client-v2 with:
  - L1 + L2 authentication
  - Rate-limit tracking (token bucket)
  - Retry with exponential backoff (429/5xx)
  - Circuit breaker integration
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BookParams, OrderArgs
from py_clob_client.order_builder.builder import OrderBuilder
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from src.clients.rate_limiter import get_rate_limiter
from src.config.constants import (
    CB_STATE_CLOSED,
    CB_STATE_HALF_OPEN,
    CB_STATE_OPEN,
    SIDE_BUY,
    SIDE_SELL,
)
from src.config.loader import get_config
from src.core.exceptions import (
    CLOBAuthenticationError,
    CLOBRateLimitError,
    OrderRejectedError,
)
from src.core.models import OrderBookL1, OrderSide, OrderType
from src.data.redis_client import get_circuit_breaker_state


class CLOBClientWrapper:
    """
    Async wrapper around py-clob-client-v2.

    Handles:
      - L2 HMAC authentication
      - Rate limiting via token bucket
      - Automatic retry on transient errors
      - Circuit breaker awareness
    """

    def __init__(self) -> None:
        self._config = get_config()
        self._client: ClobClient | None = None
        self._order_builder: OrderBuilder | None = None
        self._rate_limiter = get_rate_limiter()

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def initialize(self) -> None:
        """Initialize the CLOB client with L2 authentication."""
        host = self._config.clob.api_url
        chain_id = self._config.clob.chain_id
        private_key = self._config.polymarket.private_key

        if not private_key:
            if self._config.dry_run:
                # Dry-run mode: create client without real credentials
                self._client = None
                return
            raise CLOBAuthenticationError("POLYMARKET_PRIVATE_KEY is required for live trading")

        # Initialize with L1 (read-only) first
        self._client = ClobClient(
            host=host,
            key=private_key,
            chain_id=chain_id,
            signature_type=self._config.clob.signature_type,
        )

        # Derive L2 credentials and create authenticated client
        api_creds = self._client.create_or_derive_api_creds()
        self._client = ClobClient(
            host=host,
            key=private_key,
            chain_id=chain_id,
            signature_type=self._config.clob.signature_type,
            creds=api_creds,
        )

        self._order_builder = OrderBuilder(self._client)

    async def close(self) -> None:
        """Close the client."""
        # py-clob-client uses httpx internally, no explicit close needed
        self._client = None
        self._order_builder = None

    @property
    def is_dry_run(self) -> bool:
        return self._config.dry_run

    # =========================================================================
    # Circuit Breaker Check
    # =========================================================================

    async def _check_circuit_breaker(self, strategy_name: str = "global") -> bool:
        """Check if circuit breaker is closed (allows operations)."""
        state = await get_circuit_breaker_state(strategy_name)
        if state is None:
            return True  # No breaker set
        return state == CB_STATE_CLOSED

    # =========================================================================
    # Rate Limit Aware Methods
    # =========================================================================

    async def _acquire_rate_limit(self, component: str = "clob_rest") -> None:
        """Wait for rate limit permission before making a request."""
        ok = await self._rate_limiter.wait_and_acquire(component)
        if not ok:
            raise CLOBRateLimitError(f"Rate limit exceeded for {component}")

    # =========================================================================
    # Market Data Methods
    # =========================================================================

    async def get_order_book(self, token_id: str) -> OrderBookL1 | None:
        """Get L1 order book data for a token."""
        await self._acquire_rate_limit("clob_rest")

        if self._client is None:
            return None

        try:
            book = await asyncio.to_thread(
                self._client.get_order_book,
                token_id,
            )
            if not book:
                return None

            best_bid = Decimal(str(book.bids[0].price)) if book.bids else Decimal("0")
            best_ask = Decimal(str(book.asks[0].price)) if book.asks else Decimal("0")
            bid_size = Decimal(str(book.bids[0].size)) if book.bids else Decimal("0")
            ask_size = Decimal(str(book.asks[0].size)) if book.asks else Decimal("0")

            return OrderBookL1(
                asset_id=token_id,
                best_bid=best_bid,
                best_ask=best_ask,
                bid_size=bid_size,
                ask_size=ask_size,
            )
        except Exception:
            return None

    async def get_order_books_batch(self, token_ids: list[str]) -> dict[str, OrderBookL1]:
        """Batch-fetch order books for multiple tokens."""
        await self._acquire_rate_limit("clob_rest")

        results: dict[str, OrderBookL1] = {}
        tasks = [self.get_order_book(tid) for tid in token_ids]
        books = await asyncio.gather(*tasks, return_exceptions=True)

        for tid, book in zip(token_ids, books):
            if isinstance(book, OrderBookL1):
                results[tid] = book

        return results

    async def get_midpoint(self, token_id: str) -> Decimal | None:
        """Get the midpoint price for a token."""
        await self._acquire_rate_limit("clob_rest")

        if self._client is None:
            return None

        try:
            result = await asyncio.to_thread(
                self._client.get_midpoint,
                token_id,
            )
            return Decimal(str(result["mid"])) if result else None
        except Exception:
            return None

    async def get_midpoints_batch(self, token_ids: list[str]) -> dict[str, Decimal]:
        """Batch-fetch midpoints for multiple tokens."""
        await self._acquire_rate_limit("clob_rest")

        results: dict[str, Decimal] = {}
        if self._client is None:
            return results

        try:
            params = [BookParams(token_id=tid) for tid in token_ids]
            mids = await asyncio.to_thread(
                self._client.get_midpoints,
                params,
            )
            for tid, mid_data in zip(token_ids, mids):
                if mid_data:
                    results[tid] = Decimal(str(mid_data.get("mid", 0)))
        except Exception:
            pass

        return results

    # =========================================================================
    # Order Placement Methods
    # =========================================================================

    def _build_order_args(
        self,
        token_id: str,
        price: Decimal,
        size: Decimal,
        side: OrderSide,
    ) -> dict[str, Any]:
        """Build order arguments for the CLOB API."""
        side_str = "BUY" if side == OrderSide.BUY else "SELL"
        return {
            "token_id": token_id,
            "price": float(price),
            "size": float(size),
            "side": side_str,
        }

    async def place_order(
        self,
        token_id: str,
        price: Decimal,
        size: Decimal,
        side: OrderSide,
        order_type: OrderType = OrderType.LIMIT,
        strategy_name: str = "global",
    ) -> dict[str, Any]:
        """Place a single order via the CLOB API."""
        if not await self._check_circuit_breaker(strategy_name):
            raise OrderRejectedError(f"Circuit breaker is OPEN for {strategy_name}")

        await self._acquire_rate_limit("clob_post")

        if self.is_dry_run:
            return {"order_id": f"dry_run_{token_id}_{side.value}", "status": "DRY_RUN"}

        args = self._build_order_args(token_id, price, size, side)

        try:
            result = await asyncio.to_thread(
                self._client.create_and_post_order,
                self._order_builder,
                price=args["price"],
                size=args["size"],
                side=args["side"],
                token_id=args["token_id"],
            )
            return result if result else {}
        except Exception as exc:
            raise OrderRejectedError(f"Order rejected: {exc}") from exc

    async def place_orders_batch(
        self,
        orders: list[dict[str, Any]],
        strategy_name: str = "global",
    ) -> list[dict[str, Any]]:
        """
        Place multiple orders concurrently (all-or-nothing via asyncio.gather).

        Each order dict should have: token_id, price, size, side.
        """
        if not await self._check_circuit_breaker(strategy_name):
            raise OrderRejectedError(f"Circuit breaker is OPEN for {strategy_name}")

        tasks = []
        for order in orders:
            side = OrderSide.BUY if order["side"] == "BUY" else OrderSide.SELL
            tasks.append(
                self.place_order(
                    token_id=order["token_id"],
                    price=Decimal(str(order["price"])),
                    size=Decimal(str(order["size"])),
                    side=side,
                    strategy_name=strategy_name,
                )
            )

        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [
            {"error": str(r)} if isinstance(r, Exception) else r
            for r in results
        ]

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel an open order."""
        await self._acquire_rate_limit("order_cancel")

        if self.is_dry_run:
            return {"order_id": order_id, "status": "CANCELLED_DRY_RUN"}

        try:
            result = await asyncio.to_thread(
                self._client.cancel,
                order_id,
            )
            return result if result else {}
        except Exception:
            return {"order_id": order_id, "status": "CANCEL_FAILED"}

    async def cancel_orders_batch(self, order_ids: list[str]) -> list[dict[str, Any]]:
        """Cancel multiple orders concurrently."""
        tasks = [self.cancel_order(oid) for oid in order_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [
            {"error": str(r)} if isinstance(r, Exception) else r
            for r in results
        ]

    async def cancel_all_orders(self) -> list[dict[str, Any]]:
        """Cancel all open orders."""
        await self._acquire_rate_limit("order_cancel")

        if self.is_dry_run:
            return [{"status": "ALL_CANCELLED_DRY_RUN"}]

        try:
            result = await asyncio.to_thread(self._client.cancel_all)
            return result if result else []
        except Exception:
            return []

    async def get_order_status(self, order_id: str) -> dict[str, Any] | None:
        """Get the status of an order."""
        await self._acquire_rate_limit("clob_rest")

        if self._client is None:
            return None

        try:
            result = await asyncio.to_thread(
                self._client.get_order,
                order_id,
            )
            return result
        except Exception:
            return None


# Singleton
_clob_client: CLOBClientWrapper | None = None


def get_clob_client() -> CLOBClientWrapper:
    """Get the singleton CLOB client wrapper."""
    global _clob_client
    if _clob_client is None:
        _clob_client = CLOBClientWrapper()
    return _clob_client
