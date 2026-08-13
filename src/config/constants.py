"""
Immutable constants used across the arbitrage bot.
"""

from decimal import Decimal

# =============================================================================
# Polymarket fee constants
# =============================================================================
TAKER_FEE_RATE: Decimal = Decimal("0.0001")  # 0.01%
WINNER_FEE_RATE: Decimal = Decimal("0.02")    # 2%

# =============================================================================
# CLOB token IDs for standard outcomes
# =============================================================================
# These are the canonical token IDs for YES/NO outcomes
# Binary market token pairs are determined by Gamma API
YES_OUTCOME_LABEL = "Yes"
NO_OUTCOME_LABEL = "No"

# =============================================================================
# Market types
# =============================================================================
MARKET_TYPE_BINARY = "binary"
MARKET_TYPE_MULTI_OUTCOME = "multi_outcome"
MARKET_TYPE_MULTI_SCALAR = "multi_scalar"

# =============================================================================
# Order sides
# =============================================================================
SIDE_BUY = "BUY"
SIDE_SELL = "SELL"

# =============================================================================
# Order types
# =============================================================================
ORDER_TYPE_LIMIT = "LIMIT"
ORDER_TYPE_MARKET = "MARKET"

# =============================================================================
# Signal statuses
# =============================================================================
SIGNAL_STATUS_DETECTED = "detected"
SIGNAL_STATUS_APPROVED = "approved"
SIGNAL_STATUS_REJECTED = "rejected"
SIGNAL_STATUS_EXECUTED = "executed"
SIGNAL_STATUS_EXPIRED = "expired"

# =============================================================================
# Order statuses
# =============================================================================
ORDER_STATUS_PENDING = "pending"
ORDER_STATUS_LIVE = "live"
ORDER_STATUS_FILLED = "filled"
ORDER_STATUS_PARTIAL = "partial"
ORDER_STATUS_CANCELLED = "cancelled"
ORDER_STATUS_EXPIRED = "expired"

# =============================================================================
# Circuit breaker states
# =============================================================================
CB_STATE_CLOSED = "CLOSED"
CB_STATE_OPEN = "OPEN"
CB_STATE_HALF_OPEN = "HALF_OPEN"

# =============================================================================
# Redis key patterns
# =============================================================================
REDIS_KEY_ORDERBOOK_L1 = "orderbook:l1:{asset_id}"
REDIS_KEY_ORDERBOOK_FULL = "orderbook:full:{asset_id}"
REDIS_KEY_RATELIMIT_CLOB = "ratelimit:clob:{window}"
REDIS_KEY_RATELIMIT_POST = "ratelimit:post_order:{window}"
REDIS_KEY_CIRCUIT_BREAKER = "circuit_breaker:{strategy_name}"
REDIS_KEY_POSITION = "position:{token_id}:{strategy_id}"
REDIS_KEY_LOCK_EXECUTION = "lock:execution:{market_id}"
REDIS_KEY_SESSION_WS_MARKET = "session:ws:market"
REDIS_KEY_SESSION_WS_USER = "session:ws:user"
REDIS_KEY_KILL_SWITCH = "global:kill_switch"

# =============================================================================
# Redis stream names
# =============================================================================
STREAM_MARKET_BOOK_L1 = "stream:market.book.L1"
STREAM_MARKET_BOOK_L2 = "stream:market.book.L2"
STREAM_MARKET_TRADE = "stream:market.trade"
STREAM_SIGNAL_ARB = "stream:signal.arb"
STREAM_EXECUTION_APPROVED = "stream:execution.approved"
STREAM_EXECUTION_REJECTED = "stream:risk.rejected"
STREAM_EXECUTION_FILL = "stream:execution.fill"
STREAM_ORDER_STATUS = "stream:order.status"
STREAM_USER_ORDER_STATUS = "stream:user.order.status"
STREAM_USER_FILL = "stream:user.fill"
STREAM_RISK_POSITION = "stream:risk.position"
STREAM_SYSTEM_HEARTBEAT = "stream:system.heartbeat"
STREAM_SYSTEM_WS_DISCONNECTED = "stream:system.ws.disconnected"
STREAM_SYSTEM_WS_RECONNECTED = "stream:system.ws.reconnected"
STREAM_GAMMA_MARKET_NEW = "stream:gamma.market.new"
STREAM_GAMMA_MARKET_CLOSED = "stream:gamma.market.closed"

# =============================================================================
# Strategy names
# =============================================================================
STRATEGY_YES_NO_COMPLEMENT = "yes_no_complement"
STRATEGY_MULTI_OUTCOME = "multi_outcome"
STRATEGY_LOGICAL_ARBITRAGE = "logical_arbitrage"
STRATEGY_CROSS_PLATFORM = "cross_platform"
STRATEGY_WEATHER_CALIBRATION = "weather_calibration"
STRATEGY_CRYPTO_UPDOWN = "crypto_updown"

# =============================================================================
# Weather market discovery (Gamma API)
# =============================================================================
# The Gamma /markets endpoint supports order=liquidityNum&ascending=false and
# liquidity_num_min filtering. tag=/search= are NOT honored for weather.
# The correct, reliable discovery path is the Gamma /events endpoint with the
# ``daily-temperature`` tag slug (nested ``markets[]`` hold the real markets).
WEATHER_MIN_LIQUIDITY: float = 1000.0       # Minimum liquidityNum to consider
WEATHER_MARKET_SCAN_MAX: int = 1000         # Max markets to scan across pages
WEATHER_MARKET_PAGE_SIZE: int = 100         # Markets per Gamma API page

# Daily-temperature events discovery (tag-slug /events endpoint)
WEATHER_TAG_SLUG: str = "daily-temperature"  # Gamma events tag slug
WEATHER_EVENTS_PAGE_SIZE: int = 100          # Events per Gamma API page
WEATHER_EVENTS_MAX_PAGES: int = 2            # Max pages (100 + 44 = 144 events)

# =============================================================================
# Rate limit windows
# =============================================================================
RATELIMIT_WINDOW_10S = "10s"
RATELIMIT_WINDOW_1S = "1s"

# =============================================================================
# WebSocket heartbeat intervals (seconds)
# =============================================================================
WS_MARKET_HEARTBEAT_SEC = 10
WS_USER_HEARTBEAT_SEC = 5
