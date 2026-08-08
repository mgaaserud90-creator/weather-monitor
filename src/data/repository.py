"""
Repository pattern implementation for database access.
Provides async CRUD operations for all entities.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.data.database import get_session
from src.data.models import (
    Category,
    ConfigRecord,
    Event,
    FillRecord,
    LogicalDependency,
    Market,
    OrderRecord,
    PnLLedgerEntry,
    PositionRecord,
    RateLimitState,
    RiskEventRecord,
    SignalRecord,
    Strategy,
    Token,
)


# =============================================================================
# Market & Reference Data
# =============================================================================


async def upsert_categories(categories: list[dict]) -> None:
    """Upsert categories from Gamma API."""
    async with await get_session() as session:
        for cat in categories:
            await session.merge(Category(
                id=cat["id"],
                name=cat["name"],
                parent_id=cat.get("parent_id"),
                updated_at=datetime.now(timezone.utc),
            ))
        await session.commit()


async def upsert_events(events: list[dict]) -> None:
    """Upsert events from Gamma API."""
    async with await get_session() as session:
        for evt in events:
            await session.merge(Event(
                id=evt["id"],
                title=evt["title"],
                description=evt.get("description"),
                category_id=evt.get("category_id"),
                start_date=evt.get("start_date"),
                end_date=evt.get("end_date"),
                resolution_date=evt.get("resolution_date"),
                status=evt["status"],
                updated_at=datetime.now(timezone.utc),
            ))
        await session.commit()


async def upsert_markets(markets: list[dict]) -> None:
    """Upsert markets from Gamma API."""
    async with await get_session() as session:
        for m in markets:
            await session.merge(Market(
                id=m["id"],
                event_id=m["event_id"],
                question=m["question"],
                description=m.get("description"),
                market_type=m["market_type"],
                outcomes=m.get("outcomes", []),
                token_ids=m.get("token_ids", []),
                volume_24h=Decimal(str(m.get("volume_24h", 0))),
                liquidity=Decimal(str(m.get("liquidity", 0))),
                resolution_status=m.get("resolution_status"),
                resolution_outcome=m.get("resolution_outcome"),
                updated_at=datetime.now(timezone.utc),
            ))
        await session.commit()


async def upsert_tokens(tokens: list[dict]) -> None:
    """Upsert tokens from Gamma API."""
    async with await get_session() as session:
        for t in tokens:
            await session.merge(Token(
                id=t["id"],
                market_id=t["market_id"],
                outcome_label=t["outcome_label"],
                outcome_index=t.get("outcome_index", 0),
                is_winner=t.get("is_winner"),
            ))
        await session.commit()


async def get_active_markets() -> Sequence[Market]:
    """Get all markets that are still actively trading."""
    async with await get_session() as session:
        result = await session.execute(
            select(Market).where(Market.resolution_status == "open")
        )
        return result.scalars().all()


async def get_binary_markets() -> Sequence[Market]:
    """Get all active binary markets."""
    async with await get_session() as session:
        result = await session.execute(
            select(Market).where(
                Market.market_type == "binary",
                Market.resolution_status == "open",
            )
        )
        return result.scalars().all()


async def get_market_tokens(market_id: str) -> Sequence[Token]:
    """Get all tokens for a market."""
    async with await get_session() as session:
        result = await session.execute(
            select(Token).where(Token.market_id == market_id)
        )
        return result.scalars().all()


async def get_multi_outcome_markets() -> Sequence[Market]:
    """Get all active multi-outcome markets."""
    async with await get_session() as session:
        result = await session.execute(
            select(Market).where(
                Market.market_type == "multi_outcome",
                Market.resolution_status == "open",
            )
        )
        return result.scalars().all()


# =============================================================================
# Signal Operations
# =============================================================================


async def save_signal(signal_data: dict) -> int:
    """Save a detected signal to the database. Returns the signal ID."""
    async with await get_session() as session:
        record = SignalRecord(**signal_data)
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record.id


async def update_signal_status(signal_id: int, status: str, rejection_reason: str | None = None) -> None:
    """Update the status of a signal."""
    async with await get_session() as session:
        stmt = (
            update(SignalRecord)
            .where(SignalRecord.id == signal_id)
            .values(status=status, rejection_reason=rejection_reason)
        )
        await session.execute(stmt)
        await session.commit()


async def get_recent_signals(limit: int = 100) -> Sequence[SignalRecord]:
    """Get the most recent signals."""
    async with await get_session() as session:
        result = await session.execute(
            select(SignalRecord).order_by(SignalRecord.detected_at.desc()).limit(limit)
        )
        return result.scalars().all()


# =============================================================================
# Order Operations
# =============================================================================


async def save_order(order_data: dict) -> None:
    """Save an order to the database."""
    async with await get_session() as session:
        record = OrderRecord(**order_data)
        session.add(record)
        await session.commit()


async def update_order_status(order_id: str, status: str, size_filled: Decimal | None = None) -> None:
    """Update an order's status and filled size."""
    async with await get_session() as session:
        values = {"status": status, "last_updated_at": datetime.now(timezone.utc)}
        if size_filled is not None:
            values["size_filled"] = size_filled
        stmt = update(OrderRecord).where(OrderRecord.id == order_id).values(**values)
        await session.execute(stmt)
        await session.commit()


async def get_orders_by_signal(signal_id: int) -> Sequence[OrderRecord]:
    """Get all orders associated with a signal."""
    async with await get_session() as session:
        result = await session.execute(
            select(OrderRecord).where(OrderRecord.signal_id == signal_id)
        )
        return result.scalars().all()


# =============================================================================
# Fill Operations
# =============================================================================


async def save_fill(fill_data: dict) -> None:
    """Save a fill record."""
    async with await get_session() as session:
        record = FillRecord(**fill_data)
        session.add(record)
        await session.commit()


# =============================================================================
# Position Operations
# =============================================================================


async def get_positions(strategy_id: int | None = None, market_id: str | None = None) -> Sequence[PositionRecord]:
    """Get positions, optionally filtered by strategy and/or market."""
    async with await get_session() as session:
        stmt = select(PositionRecord).where(PositionRecord.is_settled == False)  # noqa: E712
        if strategy_id is not None:
            stmt = stmt.where(PositionRecord.strategy_id == strategy_id)
        if market_id is not None:
            stmt = stmt.where(PositionRecord.market_id == market_id)
        result = await session.execute(stmt)
        return result.scalars().all()


async def get_total_exposure() -> Decimal:
    """Get total USDC exposure across all active positions."""
    async with await get_session() as session:
        result = await session.execute(
            select(PositionRecord).where(PositionRecord.is_settled == False)  # noqa: E712
        )
        positions = result.scalars().all()
        return sum((p.cost_basis or Decimal("0")) for p in positions)


async def update_position(token_id: str, strategy_id: int, quantity: Decimal, avg_price: Decimal, cost_basis: Decimal) -> None:
    """Update or create a position."""
    async with await get_session() as session:
        stmt = select(PositionRecord).where(
            PositionRecord.token_id == token_id,
            PositionRecord.strategy_id == strategy_id,
            PositionRecord.is_settled == False,  # noqa: E712
        )
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            existing.quantity = quantity
            existing.avg_entry_price = avg_price
            existing.cost_basis = cost_basis
            existing.last_updated = datetime.now(timezone.utc)
        else:
            session.add(PositionRecord(
                token_id=token_id,
                market_id="",  # Should be set from context
                strategy_id=strategy_id,
                quantity=quantity,
                avg_entry_price=avg_price,
                cost_basis=cost_basis,
            ))
        await session.commit()


# =============================================================================
# P&L Operations
# =============================================================================


async def save_pnl_entry(entry: dict) -> None:
    """Save a P&L ledger entry."""
    async with await get_session() as session:
        record = PnLLedgerEntry(**entry)
        session.add(record)
        await session.commit()


async def get_daily_pnl(date: datetime) -> Decimal:
    """Get total P&L for a given date."""
    async with await get_session() as session:
        result = await session.execute(
            select(PnLLedgerEntry).where(PnLLedgerEntry.date == date.date())
        )
        entries = result.scalars().all()
        return sum((e.net_pnl or Decimal("0")) for e in entries)


# =============================================================================
# Risk Event Operations
# =============================================================================


async def save_risk_event(event_type: str, severity: str, details: dict) -> None:
    """Save a risk event."""
    async with await get_session() as session:
        record = RiskEventRecord(
            event_type=event_type,
            severity=severity,
            details=details,
        )
        session.add(record)
        await session.commit()


# =============================================================================
# Strategy Operations
# =============================================================================


async def get_active_strategies() -> Sequence[Strategy]:
    """Get all active strategies."""
    async with await get_session() as session:
        result = await session.execute(
            select(Strategy).where(Strategy.is_active == True)  # noqa: E712
        )
        return result.scalars().all()


# =============================================================================
# Config Operations
# =============================================================================


async def get_config_value(key: str) -> dict | None:
    """Get a configuration value from the database."""
    async with await get_session() as session:
        result = await session.execute(
            select(ConfigRecord).where(ConfigRecord.key == key)
        )
        record = result.scalar_one_or_none()
        return record.value if record else None
