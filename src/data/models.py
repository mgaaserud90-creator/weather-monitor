"""
SQLAlchemy ORM models for all database entities.

Mirrors the PostgreSQL schema defined in docker/init-db.sql.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# =============================================================================
# Reference Data
# =============================================================================


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    parent_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("categories.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now())


class Event(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(512))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("categories.id"), nullable=True)
    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32))  # active, closed, resolved, disputed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now())

    markets: Mapped[list["Market"]] = relationship(back_populates="event", lazy="selectin")


class Market(Base):
    __tablename__ = "markets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), ForeignKey("events.id"))
    question: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    market_type: Mapped[str] = mapped_column(String(32))  # binary, multi_outcome, multi_scalar
    outcomes: Mapped[dict] = mapped_column(JSONB)
    token_ids: Mapped[list[str]] = mapped_column(ARRAY(Text))
    volume_24h: Mapped[Decimal] = mapped_column(Numeric(24, 6), default=Decimal("0"))
    liquidity: Mapped[Decimal] = mapped_column(Numeric(24, 6), default=Decimal("0"))
    resolution_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolution_outcome: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now())

    event: Mapped[Event] = relationship(back_populates="markets")
    tokens: Mapped[list["Token"]] = relationship(back_populates="market", lazy="selectin")


class Token(Base):
    __tablename__ = "tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    market_id: Mapped[str] = mapped_column(String(64), ForeignKey("markets.id"))
    outcome_label: Mapped[str] = mapped_column(String(128))
    outcome_index: Mapped[int] = mapped_column(Integer)
    is_winner: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())

    market: Mapped[Market] = relationship(back_populates="tokens")


# =============================================================================
# Strategies
# =============================================================================


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    version: Mapped[str] = mapped_column(String(16), default="1.0")
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now())


# =============================================================================
# Signals
# =============================================================================


class SignalRecord(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    strategy_id: Mapped[int] = mapped_column(Integer, ForeignKey("strategies.id"))
    market_ids: Mapped[list[str]] = mapped_column(ARRAY(Text))
    token_ids: Mapped[list[str]] = mapped_column(ARRAY(Text))
    signal_type: Mapped[str] = mapped_column(String(64))
    direction: Mapped[dict] = mapped_column(JSONB)
    expected_profit: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    profit_bps: Mapped[int] = mapped_column(Integer)
    spread_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    gross_revenue: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    total_cost: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    fees_estimated: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    timestamp_ws: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="detected")
    rejection_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    extra_data: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())

    __table_args__ = (
        Index("idx_signals_status", "status"),
        Index("idx_signals_strategy", "strategy_id", "detected_at"),
    )


# =============================================================================
# Orders & Fills
# =============================================================================


class OrderRecord(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("signals.id"), nullable=True)
    strategy_id: Mapped[int] = mapped_column(Integer, ForeignKey("strategies.id"))
    token_id: Mapped[str] = mapped_column(String(64), ForeignKey("tokens.id"))
    market_id: Mapped[str] = mapped_column(String(64), ForeignKey("markets.id"))
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(16))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    size: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    size_filled: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(32))
    clob_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    last_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now())
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_orphan: Mapped[bool | None] = mapped_column(Boolean, default=False)

    __table_args__ = (
        Index("idx_orders_signal", "signal_id"),
        Index("idx_orders_status", "status"),
        Index("idx_orders_token", "token_id", "status"),
    )


class FillRecord(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(64), ForeignKey("orders.id"))
    fill_amount: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    fill_price: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    fee_paid: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())

    __table_args__ = (
        Index("idx_fills_order", "order_id"),
    )


# =============================================================================
# Positions & P&L
# =============================================================================


class PositionRecord(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_id: Mapped[str] = mapped_column(String(64), ForeignKey("tokens.id"))
    market_id: Mapped[str] = mapped_column(String(64), ForeignKey("markets.id"))
    strategy_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("strategies.id"), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("0"))
    avg_entry_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    cost_basis: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), default=Decimal("0"))
    is_settled: Mapped[bool | None] = mapped_column(Boolean, default=False)
    settlement_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    dispute_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("token_id", "strategy_id", name="idx_positions_token_strat"),
    )


class PnLLedgerEntry(Base):
    __tablename__ = "pnl_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    date: Mapped[datetime] = mapped_column(Date)
    strategy_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("strategies.id"), nullable=True)
    signal_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("signals.id"), nullable=True)
    gross_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("0"))
    fees_paid: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("0"))
    winner_fee: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("0"))
    gas_cost: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("0"))
    net_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())

    __table_args__ = (
        Index("idx_pnl_date", "date"),
        Index("idx_pnl_strategy", "strategy_id", "date"),
    )


# =============================================================================
# Risk & Configuration
# =============================================================================


class RiskEventRecord(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))
    details: Mapped[dict] = mapped_column(JSONB)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())


class ConfigRecord(Base):
    __tablename__ = "config"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now())
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class RateLimitState(Base):
    __tablename__ = "rate_limit_state"

    endpoint: Mapped[str] = mapped_column(String(128), primary_key=True)
    limit_total: Mapped[int] = mapped_column(Integer)
    remaining: Mapped[int] = mapped_column(Integer)
    reset_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now())


# =============================================================================
# Logical Dependencies (Strategy 3)
# =============================================================================


class LogicalDependency(Base):
    __tablename__ = "logical_dependencies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    implicant_market_id: Mapped[str] = mapped_column(String(64), ForeignKey("markets.id"))
    implicant_outcome: Mapped[str] = mapped_column(String(64))
    implied_market_id: Mapped[str] = mapped_column(String(64), ForeignKey("markets.id"))
    implied_outcome: Mapped[str] = mapped_column(String(64))
    strictness: Mapped[str] = mapped_column(String(32), default="PROBABILISTIC")
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool | None] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_deps_implicant", "implicant_market_id"),
        Index("idx_deps_implied", "implied_market_id"),
    )


# =============================================================================
# Cross-Platform Mappings (Strategy 4)
# =============================================================================


class CrossPlatformMapping(Base):
    __tablename__ = "cross_platform_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pm_market_id: Mapped[str] = mapped_column(String(64), ForeignKey("markets.id"))
    pm_token_id: Mapped[str] = mapped_column(String(64), ForeignKey("tokens.id"))
    kalshi_market_id: Mapped[str] = mapped_column(String(128))
    kalshi_ticker: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_active: Mapped[bool | None] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now())
