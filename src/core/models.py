"""Minimal stubs — weather monitor doesn't use these types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass
class OrderBookL1:
    """Stub for order book level-1 snapshot."""
    pass


class OrderSide(str, Enum):
    """Stub for buy/sell side enum."""
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """Stub for order type enum."""
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


@dataclass
class DirectionEntry:
    """Stub for directional trade entry."""
    pass


@dataclass
class Signal:
    """Stub for strategy signal."""
    pass
