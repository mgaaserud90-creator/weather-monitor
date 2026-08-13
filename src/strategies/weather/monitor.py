"""
Weather Market Monitor — continuously scans for weather markets and
re-evaluates positions as ensemble forecasts update.

Ensemble updates happen at 00z, 06z, 12z, 18z (every 6 hours).
The monitor re-evaluates all known markets at these times.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import structlog

from src.clients.gamma_client import GammaClient, get_gamma_client
from src.config.constants import (
    WEATHER_EVENTS_MAX_PAGES,
    WEATHER_EVENTS_PAGE_SIZE,
    WEATHER_MARKET_PAGE_SIZE,
    WEATHER_MARKET_SCAN_MAX,
    WEATHER_MIN_LIQUIDITY,
    WEATHER_TAG_SLUG,
)
from src.config.loader import get_config
from src.event_bus import get_event_bus
from src.strategies.weather.discovery import (
    is_today_highest_temperature_event,
    local_today,
)
from src.strategies.weather.market_parser import (
    TemperatureBucket,
    WeatherMarket,
    WeatherMarketParser,
)
from src.strategies.weather.strategy import WeatherCalibrationStrategy

logger = structlog.get_logger(__name__)


class WeatherMarketMonitor:
    """
    Continuous monitor for Polymarket weather markets.

    Functionality:
      - Scans Gamma API every N seconds for new weather markets
      - Re-evaluates existing markets at ensemble update times (00/06/12/18z)
      - Tracks resolution status and P&L
      - Emits signals via event bus when edge > threshold
    """

    # Weather-related search keywords for Gamma API filtering
    WEATHER_KEYWORDS: list[str] = [
        # Temperatur
        "temperature", "temp", "hottest", "coldest", "warmest",
        "°c", "°f", "degrees", "fahrenheit", "celsius",
        "record high", "record low", "heatwave", "freeze", "heat wave",
        # Værfenomen
        "weather", "hurricane", "tornado", "storm", "cyclone", "typhoon",
        "rain", "snow", "precipitation", "humidity", "wind",
        "lightning", "thunder", "hail", "flood", "drought",
        # Klima
        "climate", "global warming", "el niño", "la niña",
        "hottest year", "warmest year", "record temperature",
        "heat index", "wind chill",
        # Sesong
        "summer", "winter", "heat season", "cold season",
        "monsoon", "hurricane season",
    ]

    def __init__(
        self,
        strategy: WeatherCalibrationStrategy | None = None,
        gamma_client: GammaClient | None = None,
        scan_interval: float = 300.0,
    ) -> None:
        self._strategy = strategy
        self._gamma = gamma_client
        self._parser = WeatherMarketParser()
        self._event_bus = get_event_bus()
        self._scan_interval = scan_interval
        self._running = False
        self._task: asyncio.Task[Any] | None = None

        # Track known markets
        self._known_markets: dict[str, dict[str, Any]] = {}
        self._resolved_markets: set[str] = set()

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def initialize(self) -> None:
        """Initialize sub-components."""
        if self._gamma is None:
            self._gamma = get_gamma_client()
            await self._gamma.initialize()

        if self._strategy is None:
            self._strategy = WeatherCalibrationStrategy()
            await self._strategy.initialize()

        logger.info("weather_monitor_initialized")

    async def start(self) -> None:
        """Start the monitoring loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._scan_loop())
        logger.info("weather_monitor_started", scan_interval=self._scan_interval)

    async def stop(self) -> None:
        """Stop monitoring."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        if self._strategy is not None:
            await self._strategy.shutdown()

        if self._gamma is not None:
            await self._gamma.close()

        logger.info("weather_monitor_stopped")

    # =========================================================================
    # Main loop
    # =========================================================================

    async def _scan_loop(self) -> None:
        """Main monitoring loop."""
        while self._running:
            try:
                await self._scan_for_new_markets()
                await self._re_evaluate_if_ensemble_update()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("monitor_loop_error", error=str(exc))

            await asyncio.sleep(self._scan_interval)

    async def _scan_for_new_markets(self) -> list[WeatherMarket]:
        """
        Discover today's "Highest temperature" weather markets.

        Primary path (tag-slug events):
        1. Fetch ALL events with ``tag_slug=daily-temperature`` (fully
           paginated) from the Gamma ``/events`` endpoint.
        2. Filter to today's "Highest temperature" events by matching the
           event slug/title with ``highest-temperature`` AND today's date
           token (e.g. ``on-august-12-2026``).
        3. Flatten each event's nested ``markets[]`` (11 markets/event) into
           ``WeatherMarket`` objects.

        Fallback (only when the primary path finds nothing): the legacy
        liquidity-ordered ``/markets`` scan. Synthetic test data is only used
        in dev/paper mode when nothing is found.
        """
        markets: list[WeatherMarket] = []

        if self._gamma is None:
            logger.info("no_gamma_client_skipping_api_scan")
            return self._generate_test_markets()

        app_config = get_config()
        weather_cfg = app_config.strategy_weather
        min_liquidity = float(getattr(weather_cfg, "min_liquidity", WEATHER_MIN_LIQUIDITY))
        scan_max = int(getattr(weather_cfg, "scan_max_markets", WEATHER_MARKET_SCAN_MAX))
        page_size = int(getattr(weather_cfg, "scan_page_size", WEATHER_MARKET_PAGE_SIZE))

        today = local_today()

        # =====================================================================
        # Primary: /events?tag_slug=daily-temperature (nested markets)
        # =====================================================================
        try:
            events = await self._gamma.get_all_events_by_tag_slug(
                tag_slug=WEATHER_TAG_SLUG,
                page_size=WEATHER_EVENTS_PAGE_SIZE,
                max_pages=WEATHER_EVENTS_MAX_PAGES,
            )
            logger.info(
                "gamma_daily_temperature_events_fetched",
                count=len(events),
                tag_slug=WEATHER_TAG_SLUG,
            )

            today_events = [
                event
                for event in events
                if is_today_highest_temperature_event(
                    event.get("slug", "") or "",
                    event.get("title", "") or "",
                    today=today,
                )
            ]

            event_market_counts: dict[str, int] = {}
            for event in today_events:
                event_id = str(event.get("id", ""))
                title = event.get("title", "") or ""
                nested = event.get("markets", [])
                event_market_counts[title] = len(nested)

                for market_data in nested:
                    market_id = str(market_data.get("id", ""))
                    question = market_data.get("question", "") or title

                    if market_id in self._known_markets or market_id in self._resolved_markets:
                        continue

                    parsed = self._parse_market_data(
                        event_id=event_id,
                        market_id=market_id,
                        question=question,
                        market_data=market_data,
                    )
                    if parsed is None:
                        logger.debug("weather_parse_skipped", question=question[:80])
                        continue

                    logger.info(
                        "new_weather_market_via_tag_events",
                        event_id=event_id,
                        market_id=market_id,
                        question=question[:100],
                    )

                    markets.append(parsed)
                    await self._register_and_evaluate(
                        event_id=event_id,
                        market_id=market_id,
                        question=question,
                        market_data=market_data,
                        parsed=parsed,
                    )

            logger.info(
                "daily_temperature_discovery_complete",
                highest_temperature_events=len(today_events),
                individual_markets=sum(event_market_counts.values()),
                new_markets=len(markets),
                event_titles=list(event_market_counts.keys()),
                event_market_counts=event_market_counts,
            )

        except Exception as exc:
            logger.error("scan_daily_temperature_events_failed", error=str(exc))

        # =====================================================================
        # Fallback: /markets endpoint — paginate ordered by liquidityNum desc
        # (only used if the tag-slug events path found nothing)
        # =====================================================================
        if not markets:
            try:
                offset = 0
                total_scanned = 0
                while total_scanned < scan_max:
                    batch = await self._gamma.get_markets(
                        limit=page_size,
                        offset=offset,
                        active=True,
                        order="liquidityNum",
                        ascending=False,
                    )
                    if not batch:
                        break

                    logger.info(
                        "gamma_markets_fetched",
                        count=len(batch),
                        offset=offset,
                    )

                    for m in batch:
                        question = (m.get("question", "") or "")
                        description = (m.get("description", "") or "")
                        combined = (question + " " + description).lower()

                        if not self._is_weather_event(combined):
                            continue

                        market_id = str(m.get("id", ""))
                        if market_id in self._known_markets or market_id in self._resolved_markets:
                            continue

                        # Liquidity filter (liquidityNum is numeric on /markets)
                        liquidity = self._safe_float(m.get("liquidityNum"))
                        if liquidity is not None and liquidity < min_liquidity:
                            logger.debug(
                                "weather_market_below_min_liquidity",
                                market_id=market_id,
                                liquidity=liquidity,
                                min_liquidity=min_liquidity,
                            )
                            continue

                        logger.info(
                            "new_weather_market_via_markets",
                            market_id=market_id,
                            question=question[:100],
                            liquidity=liquidity,
                        )

                        parsed = self._parse_market_data(
                            event_id="",
                            market_id=market_id,
                            question=question,
                            market_data=m,
                        )
                        if parsed is None:
                            logger.debug("weather_parse_skipped", question=question[:80])
                            continue

                        markets.append(parsed)
                        await self._register_and_evaluate(
                            event_id="",
                            market_id=market_id,
                            question=question,
                            market_data=m,
                            parsed=parsed,
                        )

                    total_scanned += len(batch)
                    if len(batch) < page_size:
                        break
                    offset += page_size

                logger.info(
                    "gamma_markets_scan_complete",
                    total_scanned=total_scanned,
                    weather_found=len(markets),
                )

            except Exception as exc:
                logger.error("scan_markets_failed", error=str(exc))

        # =====================================================================
        # Fallback: synthetic test data ONLY in dev/paper mode
        # =====================================================================
        if not markets:
            env = str(getattr(app_config, "env", "dev")).lower()
            if env in ("dev", "paper"):
                logger.warning("no_weather_markets_found_using_test_data", env=env)
                markets = self._generate_test_markets()
            else:
                logger.warning(
                    "no_weather_markets_found_no_synthetic_fallback",
                    env=env,
                    hint="Polymarket may currently have zero active temperature markets",
                )

        return markets

    def _parse_market_data(
        self,
        *,
        event_id: str,
        market_id: str,
        question: str,
        market_data: dict[str, Any],
    ) -> WeatherMarket | None:
        """Normalize a raw Gamma market dict into a WeatherMarket.

        ``market_data`` may come from the nested ``event.markets[]`` array or
        from the top-level ``/markets`` endpoint. Both return ``outcomes`` and
        ``clobTokenIds`` as JSON-encoded strings.
        """
        outcomes = self._parse_json_array(market_data.get("outcomes"))
        parsed = self._parser.parse_question(question, outcomes=outcomes)
        if parsed is None:
            return None

        parsed.event_id = event_id
        parsed.market_id = market_id

        clob_ids = self._parse_json_array(market_data.get("clobTokenIds"))
        parsed.token_ids = [str(t) for t in clob_ids]
        for i, bucket in enumerate(parsed.buckets):
            if i < len(clob_ids):
                bucket.token_id = str(clob_ids[i])

        return parsed

    async def _register_and_evaluate(
        self,
        *,
        event_id: str,
        market_id: str,
        question: str,
        market_data: dict[str, Any],
        parsed: WeatherMarket,
    ) -> None:
        """Register a discovered market and kick off its first evaluation."""
        self._known_markets[market_id] = {
            "event_id": event_id,
            "question": question,
            "market_data": market_data,
            "liquidity": self._safe_float(market_data.get("liquidityNum")),
            "best_bid": self._safe_float(market_data.get("bestBid")),
            "best_ask": self._safe_float(market_data.get("bestAsk")),
            "last_trade_price": self._safe_float(market_data.get("lastTradePrice")),
            "volume_24h": self._safe_float(market_data.get("volume24hr")),
            "discovered_at": datetime.now(timezone.utc),
            "parsed_market": parsed,
        }

        await self._event_bus.emit_gamma_market_new({
            "event_id": event_id,
            "market_id": market_id,
            "question": question,
            "category": "weather",
        })

        await self._evaluate_market(market_id)

    def _generate_test_markets(self) -> list[WeatherMarket]:
        """Generate synthetic weather markets for testing when Polymarket has none."""
        tomorrow = date.today() + timedelta(days=1)

        test_markets = [
            WeatherMarket(
                question=f"Temperature in New York City on {tomorrow.strftime('%B %d')}? <85°F or 85-90°F or 90-95°F or >95°F",
                location="new york city",
                lat=40.7128,
                lon=-74.0060,
                target_date=tomorrow,
                buckets=[
                    TemperatureBucket(-100, 85, "<85°F"),
                    TemperatureBucket(85, 90, "85-90°F"),
                    TemperatureBucket(90, 95, "90-95°F"),
                    TemperatureBucket(95, 200, ">95°F", is_open_upper=True),
                ],
                event_id="test_nyc_temp",
                market_id="test_nyc_temp_0",
            ),
            WeatherMarket(
                question=f"Will London Heathrow record a temperature above 28°C on {tomorrow.strftime('%B %d')}?",
                location="london heathrow",
                lat=51.4700,
                lon=-0.4543,
                target_date=tomorrow,
                buckets=[
                    TemperatureBucket(82.4, 200, "Above 28°C", is_open_upper=True),
                    TemperatureBucket(-100, 82.4, "Below 28°C"),
                ],
                event_id="test_lhr_temp",
                market_id="test_lhr_temp_0",
            ),
        ]

        for tm in test_markets:
            market_id = tm.market_id
            self._known_markets[market_id] = {
                "event_id": tm.event_id,
                "question": tm.question,
                "market_data": {},
                "discovered_at": datetime.now(timezone.utc),
                "parsed_market": tm,
            }

            logger.info(
                "test_market_created",
                market_id=market_id,
                question=tm.question[:80],
            )

        return test_markets

    async def _re_evaluate_if_ensemble_update(self) -> None:
        """
        Re-evaluate all known markets at ensemble update times.

        Major NWP ensemble updates: 00z, 06z, 12z, 18z
        We re-evaluate within 5 minutes of these times.
        """
        now = datetime.now(timezone.utc)
        ensemble_hours = {0, 6, 12, 18}

        if now.hour in ensemble_hours and now.minute < 5:
            logger.info("ensemble_update_window", hour=now.hour)
            await self._re_evaluate_all()

    async def _re_evaluate_all(self) -> None:
        """Re-evaluate all known active markets."""
        for market_id in list(self._known_markets.keys()):
            if market_id in self._resolved_markets:
                continue
            await self._evaluate_market(market_id)

    async def _evaluate_market(self, market_id: str) -> None:
        """Evaluate a single market and generate signals."""
        if self._strategy is None:
            return

        market_info = self._known_markets.get(market_id)
        if market_info is None:
            return

        question = market_info.get("question", "")
        event_id = market_info.get("event_id", "")

        try:
            signal = await self._strategy.evaluate_question(
                question=question,
                event_id=event_id,
                market_id=market_id,
            )

            if signal is not None:
                logger.info(
                    "weather_signal_generated",
                    market_id=market_id,
                    edge=signal.metadata.get("edge"),
                    prob=signal.metadata.get("model_prob"),
                    bucket=signal.metadata.get("bucket_label"),
                )
                await self._event_bus.emit_signal_arb(signal.model_dump(mode="json"))

        except Exception as exc:
            logger.error(
                "market_evaluation_failed",
                market_id=market_id,
                error=str(exc),
            )

    # =========================================================================
    # Helpers
    # =========================================================================

    @staticmethod
    def _is_weather_event(text: str) -> bool:
        """Check if event text (title + description) is weather-related.

        Uses two tiers of keywords to avoid false positives:
          * **Strong** keywords (temperature-specific) — matched as substrings;
            they are specific enough to never hit sports/entertainment markets.
          * **Ambiguous** keywords (weather phenomena) — matched as whole words
            (``\\b``) so "May**weather**", "Uk**rain**ian", "**Winding** River"
            are NOT matched. Singular team names ("Thunder"/"Heat" NBA,
            "Hurricanes" NHL) are suppressed via a sports-context check.
        """
        text_lower = text.lower()

        # --- Strong / specific weather keywords (safe substring match) ---
        strong_keywords = [
            "temperature", "°c", "°f", "degrees", "fahrenheit", "celsius",
            "hottest", "coldest", "warmest", "heatwave", "heat wave",
            "heat index", "wind chill", "record high", "record low",
            "record temperature", "precipitation", "snowfall", "rainfall",
            "humidity", "tornado", "cyclone", "typhoon", "blizzard",
            "drought", "frost", "monsoon", "meteorological", "barometric",
            "noaa", "national weather", "el niño", "la niña",
            "climate", "global warming", "freeze warning",
            "hottest year", "warmest year",
        ]
        if any(kw in text_lower for kw in strong_keywords):
            return True

        # --- Ambiguous keywords (whole-word match to avoid false positives) ---
        ambiguous_keywords = [
            "weather", "hurricane", "thunder", "storm", "heat", "rain",
            "snow", "wind", "flood", "hail", "lightning", "freeze",
            "summer", "winter",
        ]
        has_ambiguous = any(
            re.search(rf"\b{re.escape(kw)}\b", text_lower)
            for kw in ambiguous_keywords
        )
        if not has_ambiguous:
            return False

        # Suppress ambiguous matches in obvious sports contexts (NHL/NBA/NFL
        # team names like "Hurricanes", "Thunder", "Heat") — a strong weather
        # signal would already have returned True above.
        sports_indicators = [
            "nhl", "nba", "nfl", "mlb", "mls", "premier league",
            "beat the", "matchup", "playoff", "playoffs", "vs ", "vs.",
            "goals", "score", "fifa", "uefa", "nhl:", "nba:", "nfl:",
            "win game", "season record",
        ]
        if any(ind in text_lower for ind in sports_indicators):
            return False

        return True

    @staticmethod
    def _parse_json_array(value: Any) -> list[Any]:
        """Parse a Gamma API JSON-encoded string array into a list.

        The ``/markets`` endpoint returns ``clobTokenIds``, ``outcomes`` and
        ``outcomePrices`` as JSON-encoded *strings* (e.g. ``'["123","456"]'``)
        rather than native arrays. This helper accepts a string, a list, or
        ``None`` and always returns a native list.
        """
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            try:
                parsed = json.loads(stripped)
                return parsed if isinstance(parsed, list) else []
            except (json.JSONDecodeError, ValueError):
                return []
        return []

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        """Safely convert a value to float, returning None on failure."""
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def get_state(self) -> dict[str, Any]:
        """Get current monitor state."""
        return {
            "running": self._running,
            "known_markets": len(self._known_markets),
            "resolved_markets": len(self._resolved_markets),
            "scan_interval": self._scan_interval,
            "active_market_ids": [
                mid for mid in self._known_markets
                if mid not in self._resolved_markets
            ],
        }
