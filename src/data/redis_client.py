"""
Redis client wrapper with all key patterns from the architecture.
Provides typed access to Redis data structures: order books, rate limiting, circuit breakers,
locks, sessions, and event streams.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import orjson
from redis.asyncio import Redis

from src.config.constants import (
    REDIS_KEY_CIRCUIT_BREAKER,
    REDIS_KEY_KILL_SWITCH,
    REDIS_KEY_LOCK_EXECUTION,
    REDIS_KEY_ORDERBOOK_FULL,
    REDIS_KEY_ORDERBOOK_L1,
    REDIS_KEY_POSITION,
    REDIS_KEY_RATELIMIT_CLOB,
    REDIS_KEY_RATELIMIT_POST,
    REDIS_KEY_SESSION_WS_MARKET,
    REDIS_KEY_SESSION_WS_USER,
)
from src.config.loader import get_config
from src.core.models import OrderBookL1


_redis: Redis | None = None


async def init_redis() -> None:
    """Initialize the async Redis connection pool."""
    global _redis
    config = get_config()
    _redis = Redis.from_url(
        config.redis.url,
        db=config.redis.db,
        max_connections=config.redis.max_connections,
        decode_responses=True,
    )


async def get_redis() -> Redis:
    """Get the Redis client instance."""
    if _redis is None:
        raise RuntimeError("Redis not initialized. Call init_redis() first.")
    return _redis


async def close_redis() -> None:
    """Close the Redis connection pool."""
    global _redis
    if _redis is not None:
        await _redis.close()
        _redis = None


# =============================================================================
# Order Book Operations
# =============================================================================


async def set_orderbook_l1(l1: OrderBookL1) -> None:
    """Store L1 order book data in Redis."""
    r = await get_redis()
    key = REDIS_KEY_ORDERBOOK_L1.format(asset_id=l1.asset_id)
    await r.hset(
        key,
        mapping={
            "best_bid": str(l1.best_bid),
            "best_ask": str(l1.best_ask),
            "bid_size": str(l1.bid_size),
            "ask_size": str(l1.ask_size),
            "timestamp": l1.timestamp.isoformat(),
        },
    )


async def get_orderbook_l1(asset_id: str) -> OrderBookL1 | None:
    """Get L1 order book data from Redis."""
    r = await get_redis()
    key = REDIS_KEY_ORDERBOOK_L1.format(asset_id=asset_id)
    data = await r.hgetall(key)
    if not data:
        return None
    return OrderBookL1(
        asset_id=asset_id,
        best_bid=Decimal(data.get("best_bid", "0")),
        best_ask=Decimal(data.get("best_ask", "0")),
        bid_size=Decimal(data.get("bid_size", "0")),
        ask_size=Decimal(data.get("ask_size", "0")),
        timestamp=datetime.fromisoformat(data.get("timestamp", datetime.now(timezone.utc).isoformat())),
    )


async def set_orderbook_full(asset_id: str, orders: dict[str, str]) -> None:
    """Store full order book (price → size mapping) as a sorted set."""
    r = await get_redis()
    key = REDIS_KEY_ORDERBOOK_FULL.format(asset_id=asset_id)
    pipe = r.pipeline()
    pipe.delete(key)
    for price, size in orders.items():
        pipe.zadd(key, {size: float(price)})
    pipe.expire(key, 5)  # 5s TTL
    await pipe.execute()


async def get_orderbook_full(asset_id: str) -> dict[Decimal, Decimal]:
    """Get full order book from Redis."""
    r = await get_redis()
    key = REDIS_KEY_ORDERBOOK_FULL.format(asset_id=asset_id)
    data = await r.zrange(key, 0, -1, withscores=True)
    return {Decimal(str(score)): Decimal(member) for member, score in data}


# =============================================================================
# Rate Limit Operations
# =============================================================================


async def check_rate_limit(window: str, max_requests: int) -> bool:
    """Check if we're within rate limit for a window. Returns True if allowed."""
    r = await get_redis()
    key = REDIS_KEY_RATELIMIT_CLOB.format(window=window)
    current = await r.incr(key)
    if current == 1:
        await r.expire(key, 10)  # 10s window
    return current <= max_requests


async def check_post_rate_limit(window: str, max_requests: int) -> bool:
    """Check POST-specific rate limit."""
    r = await get_redis()
    key = REDIS_KEY_RATELIMIT_POST.format(window=window)
    current = await r.incr(key)
    if current == 1:
        await r.expire(key, 1)  # 1s window
    return current <= max_requests


# =============================================================================
# Circuit Breaker Operations
# =============================================================================


async def get_circuit_breaker_state(strategy_name: str) -> str | None:
    """Get the circuit breaker state for a strategy."""
    r = await get_redis()
    key = REDIS_KEY_CIRCUIT_BREAKER.format(strategy_name=strategy_name)
    return await r.get(key)


async def set_circuit_breaker_state(strategy_name: str, state: str) -> None:
    """Set the circuit breaker state for a strategy."""
    r = await get_redis()
    key = REDIS_KEY_CIRCUIT_BREAKER.format(strategy_name=strategy_name)
    await r.set(key, state)


# =============================================================================
# Position Cache Operations
# =============================================================================


async def update_position_cache(token_id: str, strategy_id: int, quantity: Decimal, avg_price: Decimal, cost_basis: Decimal) -> None:
    """Update position data in Redis cache."""
    r = await get_redis()
    key = REDIS_KEY_POSITION.format(token_id=token_id, strategy_id=strategy_id)
    await r.hset(
        key,
        mapping={
            "quantity": str(quantity),
            "avg_price": str(avg_price),
            "cost_basis": str(cost_basis),
        },
    )


async def get_position_cache(token_id: str, strategy_id: int) -> dict[str, str] | None:
    """Get position data from Redis cache."""
    r = await get_redis()
    key = REDIS_KEY_POSITION.format(token_id=token_id, strategy_id=strategy_id)
    data = await r.hgetall(key)
    return data if data else None


# =============================================================================
# Distributed Lock Operations
# =============================================================================


async def acquire_execution_lock(market_id: str, ttl: int = 5) -> bool:
    """Acquire a distributed lock for atomic execution on a market."""
    r = await get_redis()
    key = REDIS_KEY_LOCK_EXECUTION.format(market_id=market_id)
    return await r.set(key, "1", nx=True, ex=ttl) or False


async def release_execution_lock(market_id: str) -> None:
    """Release the execution lock for a market."""
    r = await get_redis()
    key = REDIS_KEY_LOCK_EXECUTION.format(market_id=market_id)
    await r.delete(key)


# =============================================================================
# Session State Operations
# =============================================================================


async def update_ws_session(session_type: str, state: str, last_seq: int = 0, reconnect_count: int = 0) -> None:
    """Update WebSocket session state."""
    r = await get_redis()
    key = REDIS_KEY_SESSION_WS_MARKET if session_type == "market" else REDIS_KEY_SESSION_WS_USER
    await r.hset(
        key,
        mapping={
            "state": state,
            "last_seq": str(last_seq),
            "connected_at": datetime.now(timezone.utc).isoformat(),
            "reconnect_count": str(reconnect_count),
        },
    )


async def get_ws_session(session_type: str) -> dict[str, str] | None:
    """Get WebSocket session state."""
    r = await get_redis()
    key = REDIS_KEY_SESSION_WS_MARKET if session_type == "market" else REDIS_KEY_SESSION_WS_USER
    data = await r.hgetall(key)
    return data if data else None


# =============================================================================
# Kill Switch Operations
# =============================================================================


async def is_kill_switch_active() -> bool:
    """Check if the global kill switch is active."""
    r = await get_redis()
    return await r.exists(REDIS_KEY_KILL_SWITCH) > 0


async def activate_kill_switch() -> None:
    """Activate the global kill switch."""
    r = await get_redis()
    await r.set(REDIS_KEY_KILL_SWITCH, "1")


async def deactivate_kill_switch() -> None:
    """Deactivate the global kill switch."""
    r = await get_redis()
    await r.delete(REDIS_KEY_KILL_SWITCH)


# =============================================================================
# Redis Stream / PubSub Operations
# =============================================================================


async def publish_event(channel: str, data: dict[str, Any]) -> None:
    """Publish an event to a Redis PubSub channel."""
    r = await get_redis()
    payload = orjson.dumps(data).decode("utf-8")
    await r.publish(channel, payload)


async def add_to_stream(stream: str, data: dict[str, Any], maxlen: int = 100000) -> str:
    """Add an event to a Redis Stream with max length."""
    r = await get_redis()
    payload = orjson.dumps(data).decode("utf-8")
    return await r.xadd(stream, {"data": payload}, maxlen=maxlen, approximate=True)
