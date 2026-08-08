"""
Configuration management for the Polymarket Arbitrage Trading Bot.

Uses Pydantic Settings for type-safe, environment-variable-driven configuration.
All values can be overridden via environment variables or .env file.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PostgresConfig(BaseSettings):
    """PostgreSQL connection configuration."""

    model_config = SettingsConfigDict(env_prefix="POSTGRES_")

    host: str = Field(default="localhost")
    port: int = Field(default=5432)
    db: str = Field(default="polymarket_arb")
    user: str = Field(default="arb_bot")
    password: str = Field(default="changeme")
    min_connections: int = Field(default=5)
    max_connections: int = Field(default=20)

    @property
    def database_url(self) -> str:
        return f"postgresql+asyncpg://{self.user}:{self.password}@{self.host}:{self.port}/{self.db}"

    @property
    def sync_database_url(self) -> str:
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.db}"


class RedisConfig(BaseSettings):
    """Redis connection configuration."""

    model_config = SettingsConfigDict(env_prefix="REDIS_")

    host: str = Field(default="localhost")
    port: int = Field(default=6379)
    password: str = Field(default="")
    db: int = Field(default=0)
    max_connections: int = Field(default=50)

    @property
    def url(self) -> str:
        if self.password:
            return f"redis://:{self.password}@{self.host}:{self.port}/{self.db}"
        return f"redis://{self.host}:{self.port}/{self.db}"


class PolymarketConfig(BaseSettings):
    """Polymarket API credentials and endpoints."""

    model_config = SettingsConfigDict(
        env_prefix="POLYMARKET_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    private_key: str = Field(default="")
    api_key: str = Field(default="")
    api_secret: str = Field(default="")
    passphrase: str = Field(default="")
    funder_address: str = Field(default="")


class ClobConfig(BaseSettings):
    """CLOB API configuration."""

    model_config = SettingsConfigDict(env_prefix="CLOB_")

    api_url: str = Field(default="https://clob.polymarket.com")
    ws_url: str = Field(default="wss://ws-subscriptions-clob.polymarket.com/ws/market")
    ws_user_url: str = Field(default="wss://ws-subscriptions-clob.polymarket.com/ws/user")
    chain_id: int = Field(default=137)
    signature_type: int = Field(default=2)
    snapshot_interval_sec: float = Field(default=1.0)
    validation_tolerance_bps: int = Field(default=10)


class GammaConfig(BaseSettings):
    """Gamma API configuration."""

    model_config = SettingsConfigDict(env_prefix="GAMMA_")

    api_url: str = Field(default="https://gamma-api.polymarket.com")
    poll_interval_sec: float = Field(default=60.0)
    request_timeout_sec: float = Field(default=30.0)
    max_retries: int = Field(default=3)
    backoff_base_sec: float = Field(default=2.0)


class WebSocketConfig(BaseSettings):
    """WebSocket connection configuration."""

    model_config = SettingsConfigDict(env_prefix="WS_")

    reconnect_max_delay_sec: float = Field(default=60.0)
    ping_interval_sec: float = Field(default=10.0)
    ping_timeout_sec: float = Field(default=5.0)
    max_message_backlog: int = Field(default=10000)


class SignalAggregatorConfig(BaseSettings):
    """Signal aggregator configuration."""

    model_config = SettingsConfigDict(env_prefix="SIGNAL_")

    max_queue_depth: int = Field(default=1000)
    ttl_ms: int = Field(default=500)
    dedup_window_ms: int = Field(default=200)
    min_priority_score: float = Field(default=0.0)


class StrategyYesNoConfig(BaseSettings):
    """YES+NO Complement strategy configuration."""

    model_config = SettingsConfigDict(env_prefix="YES_NO_")

    enabled: bool = Field(default=True)
    min_profit_bps: int = Field(default=250)
    max_position_usdc: float = Field(default=5000.0)
    execution_timeout_ms: int = Field(default=500)
    max_slippage_bps: int = Field(default=50)
    min_depth_per_side_usdc: float = Field(default=500.0)
    taker_fee_rate: float = Field(default=0.0001)
    winner_fee_rate: float = Field(default=0.02)
    atomic_mode: Literal["all_or_nothing", "best_effort"] = Field(default="all_or_nothing")
    cooldown_after_trade_ms: int = Field(default=5000)


class StrategyMultiOutcomeConfig(BaseSettings):
    """Multi-Outcome Bundle strategy configuration."""

    model_config = SettingsConfigDict(env_prefix="MULTI_")

    enabled: bool = Field(default=True)
    min_profit_bps: int = Field(default=300)
    max_position_usdc: float = Field(default=10000.0)
    execution_timeout_ms: int = Field(default=800)
    max_slippage_bps: int = Field(default=50)
    min_depth_per_outcome_usdc: float = Field(default=300.0)
    atomic_mode: Literal["all_or_nothing", "best_effort"] = Field(default="all_or_nothing")
    partial_ok: bool = Field(default=False)
    max_outcomes: int = Field(default=10)


class StrategyLogicalArbConfig(BaseSettings):
    """Logical Arbitrage strategy configuration."""

    model_config = SettingsConfigDict(env_prefix="LOGICAL_")

    enabled: bool = Field(default=False)
    min_profit_usd: float = Field(default=50.0)
    max_position_usdc: float = Field(default=5000.0)
    max_total_budget_usdc: float = Field(default=25000.0)
    ip_solver_timeout_sec: float = Field(default=5.0)
    llm_model: str = Field(default="gpt-4o")
    llm_max_tokens: int = Field(default=4000)
    dependency_refresh_minutes: int = Field(default=15)


class StrategyCrossPlatformConfig(BaseSettings):
    """Cross-Platform Arbitrage strategy configuration."""

    model_config = SettingsConfigDict(env_prefix="CROSS_PLATFORM_")

    enabled: bool = Field(default=False)
    min_profit_bps: int = Field(default=400)
    max_position_usdc: float = Field(default=5000.0)


class StrategyWeatherConfig(BaseSettings):
    """Weather Calibration strategy configuration."""

    model_config = SettingsConfigDict(env_prefix="WEATHER_")

    enabled: bool = Field(default=True)
    min_edge_threshold: float = Field(default=0.05)
    max_position_per_market: float = Field(default=100.0)
    ensemble_confidence_floor: float = Field(default=0.7)
    kelly_fraction: float = Field(default=0.25)
    scan_interval_seconds: float = Field(default=300.0)

    # BMA ensemble
    bma_window_days: int = Field(default=40, description="Rolling window for BMA weight training")
    bma_enabled: bool = Field(default=True, description="Use BMA multi-model ensemble")

    # METAR real-time observations
    metar_enabled: bool = Field(default=True, description="Fetch real-time METAR observations")
    metar_update_sec: int = Field(default=1800, description="METAR fetch interval in seconds")
    metar_cache_ttl_sec: int = Field(default=300, description="METAR cache TTL in seconds")

    # Microclimate
    microclimate_enabled: bool = Field(default=True, description="Apply microclimate corrections")

    # Satellite cloud cover
    satellite_enabled: bool = Field(default=True, description="Apply satellite cloud cover corrections")

    # Market discovery — liquidity filtering & pagination
    min_liquidity: float = Field(default=1000.0, description="Minimum liquidityNum for weather market discovery")
    scan_max_markets: int = Field(default=1000, description="Max markets to scan across paginated pages")
    scan_page_size: int = Field(default=100, description="Markets per Gamma API page during discovery")

    # Limits
    max_cities: int = Field(default=30, description="Max cities to fetch forecasts for")

    # Conformal prediction
    conformal_enabled: bool = Field(default=True, description="Use conformal prediction for calibrated intervals")
    conformal_significance: float = Field(default=0.1, description="Conformal prediction significance level")


class StrategyCryptoUpDownConfig(BaseSettings):
    """Crypto Up/Down Resolution Arbitrage strategy configuration."""

    model_config = SettingsConfigDict(env_prefix="CRYPTO_UPDOWN_")

    enabled: bool = Field(default=False)
    min_edge_threshold: float = Field(default=0.05)
    max_position_per_market: float = Field(default=100.0)
    scan_interval_seconds: int = Field(default=120)
    kelly_fraction: float = Field(default=0.25)
    expiry_buffer_minutes: int = Field(default=5)
    supported_symbols: list[str] = Field(default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])


class RiskConfig(BaseSettings):
    """Risk management configuration."""

    model_config = SettingsConfigDict(env_prefix="")

    max_position_per_market_usdc: float = Field(default=5000.0)
    max_position_per_strategy_usdc: float = Field(default=15000.0)
    max_global_position_usdc: float = Field(default=50000.0)
    max_position_per_outcome_usdc: float = Field(default=2500.0)
    max_concurrent_markets: int = Field(default=10)

    # Circuit Breakers
    cb_consecutive_losses_threshold: int = Field(default=5)
    cb_consecutive_losses_cooldown_sec: int = Field(default=300)
    cb_daily_drawdown_threshold_pct: float = Field(default=5.0)
    cb_daily_drawdown_cooldown_sec: int = Field(default=3600)
    cb_rate_limit_trips_threshold: int = Field(default=3)
    cb_rate_limit_trips_window_sec: int = Field(default=60)
    cb_slippage_violations_threshold: int = Field(default=10)
    cb_slippage_violations_window_sec: int = Field(default=300)

    # Capital Allocation
    total_bankroll_usdc: float = Field(default=50000.0)
    allocation_method: Literal["equal_weight", "kelly"] = Field(default="equal_weight")
    kelly_fraction: float = Field(default=0.25)
    min_cash_reserve_usdc: float = Field(default=5000.0)


class ExecutionConfig(BaseSettings):
    """Execution engine configuration."""

    model_config = SettingsConfigDict(env_prefix="")

    execution_timeout_ms: int = Field(default=2000)
    max_slippage_bps: int = Field(default=50)
    atomic_mode: Literal["all_or_nothing", "best_effort"] = Field(default="all_or_nothing")
    cancel_orphans_after_ms: int = Field(default=5000)
    fill_reconciliation_interval_sec: float = Field(default=1.0)
    fill_stale_timeout_sec: float = Field(default=30.0)


class UMAConfig(BaseSettings):
    """UMA Oracle monitoring configuration."""

    model_config = SettingsConfigDict(env_prefix="UMA_")

    challenge_window_hours: int = Field(default=2)
    poll_interval_sec: float = Field(default=30.0)


class MonitoringConfig(BaseSettings):
    """Monitoring and metrics configuration."""

    model_config = SettingsConfigDict(env_prefix="")

    metrics_port: int = Field(default=9090)
    heartbeat_interval_sec: float = Field(default=15.0)


class AgentConfigModel(BaseSettings):
    """Multi-Agent: Configuration for a single trading agent."""

    model_config = SettingsConfigDict(env_prefix="AGENT_")

    agent_id: str = Field(default="agent_1")
    agent_type: str = Field(default="weather")
    enabled: bool = Field(default=True)
    capital_allocation_usdc: float = Field(default=5000.0, ge=10.0)
    max_position_usdc: float = Field(default=1000.0, ge=1.0)
    max_daily_loss_pct: float = Field(default=8.0, ge=0.1, le=100.0)
    cooldown_seconds: int = Field(default=60, ge=0)
    heartbeat_interval_seconds: int = Field(default=5, ge=1, le=60)


class MultiAgentConfigModel(BaseSettings):
    """Multi-Agent: Top-level multi-agent system configuration."""

    model_config = SettingsConfigDict(env_prefix="AGENT_")

    max_concurrent_agents: int = Field(default=8, ge=1, le=20)
    heartbeat_interval_seconds: int = Field(default=5, ge=1, le=60)
    health_check_timeout_seconds: int = Field(default=30, ge=5, le=300)
    agent_restart_cooldown_seconds: int = Field(default=10, ge=1, le=120)
    duplicate_detection_enabled: bool = Field(default=True)
    multi_agent_enabled: bool = Field(default=True)


class AppConfig(BaseSettings):
    """Top-level application configuration aggregating all sub-configs."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    env: Literal["dev", "paper", "production"] = Field(default="dev")
    dry_run: bool = Field(default=True)
    log_level: Literal["DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"] = Field(default="INFO")

    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    polymarket: PolymarketConfig = Field(default_factory=PolymarketConfig)
    clob: ClobConfig = Field(default_factory=ClobConfig)
    gamma: GammaConfig = Field(default_factory=GammaConfig)
    websocket: WebSocketConfig = Field(default_factory=WebSocketConfig)

    signal_aggregator: SignalAggregatorConfig = Field(default_factory=SignalAggregatorConfig)
    strategy_yes_no: StrategyYesNoConfig = Field(default_factory=StrategyYesNoConfig)
    strategy_multi: StrategyMultiOutcomeConfig = Field(default_factory=StrategyMultiOutcomeConfig)
    strategy_logical: StrategyLogicalArbConfig = Field(default_factory=StrategyLogicalArbConfig)
    strategy_cross: StrategyCrossPlatformConfig = Field(default_factory=StrategyCrossPlatformConfig)
    strategy_weather: StrategyWeatherConfig = Field(default_factory=StrategyWeatherConfig)
    strategy_crypto_updown: StrategyCryptoUpDownConfig = Field(default_factory=StrategyCryptoUpDownConfig)

    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    uma: UMAConfig = Field(default_factory=UMAConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    agent: MultiAgentConfigModel = Field(default_factory=MultiAgentConfigModel)

    # External API keys
    openai_api_key: str = Field(default="")
    kalshi_api_key: str = Field(default="")
    kalshi_api_secret: str = Field(default="")
    kalshi_base_url: str = Field(default="https://trading-api.kalshi.com/trade-api/v2")

    # Polymarket Relayer (gass-lose transaksjoner)
    relayer_api_key: str = Field(default="")
    relayer_api_url: str = Field(default="https://relayer-v2.polymarket.com")

    # Telegram
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")

    # Web3
    polygon_rpc_url: str = Field(default="https://polygon-rpc.com")
