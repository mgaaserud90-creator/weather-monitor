"""
Satellite Cloud Cover Correction — uses GOES-16/18 (Americas) and Meteosat
(Europe/Africa) satellite imagery to estimate cloud cover percentage for each
station location and apply temperature corrections.

Mechanism:
  - Clear sky (0-20% cloud) → +1 to +4°F warming boost
  - Overcast (80-100%) → -1 to -3°F cooling dampening
  - Partial (20-80%) → proportional correction

Only applied for T+0 to T+2 markets (satellite has short predictive horizon).

Data sources:
  - GOES-16/18: NOAA CLASS / OpenDAP (Americas)
  - Meteosat: EUMETSAT (Europe, Africa)
  - Himawari: JMA (Asia-Pacific) — future enhancement

For simplicity, we use Open-Meteo's cloud cover forecast for satellite
estimation (since real satellite data requires heavy image processing).
The cloud cover correction is then applied as a temperature delta.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from src.clients.openmeteo_client import OpenMeteoClient, get_openmeteo_client
from src.strategies.weather.market_parser import STATION_METADATA

logger = structlog.get_logger(__name__)


# =============================================================================
# Data Models
# =============================================================================


@dataclass
class CloudCoverEstimate:
    """Cloud cover estimate for a station location."""
    location: str
    icao: str = ""
    cloud_cover_pct: float = 0.0  # 0-100
    low_cloud_pct: float = 0.0
    mid_cloud_pct: float = 0.0
    high_cloud_pct: float = 0.0
    source: str = "open-meteo"  # "goes", "meteosat", "open-meteo"
    valid_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    lead_hours: int = 0  # 0 = now, 1-48 = forecast
    confidence: float = 0.7  # 0-1, lower for longer leads


@dataclass
class CloudCorrection:
    """Temperature correction from cloud cover."""
    location: str
    cloud_cover_pct: float
    temperature_correction_f: float  # Positive = warmer, negative = cooler
    correction_type: str  # "warming", "cooling", "neutral"
    confidence: float


# =============================================================================
# Cloud Cover → Temperature Correction Map
# =============================================================================

# Cloud cover percentage ranges and their temperature effects
# Based on empirical studies of radiational heating/cooling
CLOUD_EFFECT_TABLE: list[dict[str, Any]] = [
    # (min_pct, max_pct, day_correction_f, night_correction_f, label)
    {"min": 0, "max": 10, "day_f": 4.0, "night_f": -3.0, "label": "Clear"},
    {"min": 10, "max": 30, "day_f": 2.5, "night_f": -1.5, "label": "Mostly clear"},
    {"min": 30, "max": 50, "day_f": 1.0, "night_f": 0.0, "label": "Partly cloudy"},
    {"min": 50, "max": 70, "day_f": -0.5, "night_f": 1.0, "label": "Mostly cloudy"},
    {"min": 70, "max": 90, "day_f": -1.5, "night_f": 1.5, "label": "Cloudy"},
    {"min": 90, "max": 100, "day_f": -3.0, "night_f": 2.0, "label": "Overcast"},
]


# =============================================================================
# Satellite Cloud Cover Corrector
# =============================================================================


class SatelliteCorrector:
    """
    Estimates cloud cover from satellite data and applies temperature corrections.

    For now, uses Open-Meteo's cloud cover forecast as a proxy for satellite
    imagery. Future versions can integrate GOES-16/18 and Meteosat direct
    satellite data feeds.

    Only applies corrections for T+0, T+1, T+2 markets (lead ≤ 48 hours).
    """

    # Maximum lead hours for satellite correction (48h = T+2)
    MAX_LEAD_HOURS: int = 48

    # Open-Meteo cloud cover endpoint uses the same URL as forecast
    CLOUD_VARIABLES: list[str] = [
        "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
    ]

    def __init__(
        self,
        openmeteo: OpenMeteoClient | None = None,
        use_real_satellite: bool = False,
    ) -> None:
        self._openmeteo = openmeteo
        self._use_real_satellite = use_real_satellite
        self._cloud_cache: dict[str, CloudCoverEstimate] = {}

        logger.info(
            "satellite_corrector_initialized",
            max_lead_hours=self.MAX_LEAD_HOURS,
            real_satellite=use_real_satellite,
        )

    @property
    def openmeteo(self) -> OpenMeteoClient:
        if self._openmeteo is None:
            self._openmeteo = get_openmeteo_client()
        return self._openmeteo

    # =========================================================================
    # Main correction methods
    # =========================================================================

    async def fetch_cloud_cover(
        self,
        lat: float,
        lon: float,
        location: str,
        lead_days: int = 0,
    ) -> CloudCoverEstimate:
        """
        Estimate cloud cover for a station at given lead time.

        Args:
            lat, lon: Station coordinates
            location: Location name
            lead_days: Days ahead (0 = today)

        Returns:
            CloudCoverEstimate with percentages and confidence.
        """
        lead_hours = lead_days * 24
        cache_key = f"{location}:{lead_days}"

        # Check cache (valid for 30 min)
        if cache_key in self._cloud_cache:
            cached = self._cloud_cache[cache_key]
            age = (datetime.now(timezone.utc) - cached.valid_at).total_seconds()
            if age < 1800:
                return cached

        # Beyond max lead → return zero correction
        if lead_hours > self.MAX_LEAD_HOURS:
            return CloudCoverEstimate(
                location=location,
                cloud_cover_pct=50.0,  # Neutral
                source="none",
                lead_hours=lead_hours,
                confidence=0.1,
            )

        try:
            # Use Open-Meteo for cloud cover (request cloud-cover daily vars).
            # NOTE: get_forecast() has no ``variables`` kwarg; cloud cover is
            # requested via ``include_cloud_cover=True`` which appends
            # cloud_cover_max/cloud_cover_min to the daily variables.
            forecast = await self.openmeteo.get_forecast(
                lat, lon,
                days=max(1, lead_days + 1),
                include_cloud_cover=True,
            )

            if forecast is None or not forecast.daily:
                return self._empty_estimate(location, lead_hours)

            idx = min(lead_days, len(forecast.daily) - 1)
            day = forecast.daily[idx]

            # Extract cloud cover (daily mean). The DailyForecast.cloud_cover
            # property returns the mean of cloud_cover_max/min, defaulting to a
            # neutral 50%% when cloud-cover data is absent (does NOT crash).
            total_cloud = float(getattr(day, "cloud_cover", 50.0))
            low_cloud = 0.0
            mid_cloud = 0.0
            high_cloud = 0.0

            if (
                getattr(day, "cloud_cover_max", None) is None
                and getattr(day, "cloud_cover_min", None) is None
            ):
                logger.warning(
                    "cloud_cover_unavailable_using_neutral",
                    location=location,
                    lead_days=lead_days,
                )

            # Confidence decays with lead time
            confidence = max(0.3, 0.9 - lead_days * 0.15)

            estimate = CloudCoverEstimate(
                location=location,
                cloud_cover_pct=total_cloud,
                low_cloud_pct=low_cloud,
                mid_cloud_pct=mid_cloud,
                high_cloud_pct=high_cloud,
                source="open-meteo",
                lead_hours=lead_hours,
                confidence=confidence,
            )

            self._cloud_cache[cache_key] = estimate
            return estimate

        except Exception as exc:
            logger.warning("cloud_cover_fetch_failed", location=location, error=str(exc))
            return self._empty_estimate(location, lead_hours)

    async def calculate_correction(
        self,
        lat: float,
        lon: float,
        location: str,
        lead_days: int = 0,
        is_daytime: bool = True,
    ) -> CloudCorrection:
        """
        Calculate temperature correction from cloud cover.

        Args:
            lat, lon: Station coordinates
            location: Location name
            lead_days: Days ahead
            is_daytime: True for daytime max temp, False for nighttime min

        Returns:
            CloudCorrection with temperature delta in °F.
        """
        cloud = await self.fetch_cloud_cover(lat, lon, location, lead_days)

        # Find matching row in effect table
        correction_f = 0.0
        correction_type = "neutral"

        for row in CLOUD_EFFECT_TABLE:
            if row["min"] <= cloud.cloud_cover_pct <= row["max"]:
                correction_f = row["day_f"] if is_daytime else row["night_f"]
                if correction_f > 0.5:
                    correction_type = "warming"
                elif correction_f < -0.5:
                    correction_type = "cooling"
                else:
                    correction_type = "neutral"
                break

        # Scale by confidence
        correction_f *= cloud.confidence

        # Marine influence: stations near water have less cloud effect
        # (more moderate, clouds matter less for max temps)
        station_meta = STATION_METADATA.get(location.lower(), {})
        if station_meta.get("marine_influence", False):
            correction_f *= 0.6

        return CloudCorrection(
            location=location,
            cloud_cover_pct=cloud.cloud_cover_pct,
            temperature_correction_f=round(correction_f, 2),
            correction_type=correction_type,
            confidence=round(cloud.confidence, 3),
        )

    # =========================================================================
    # Batch operations
    # =========================================================================

    async def fetch_all_locations(
        self,
        locations: list[tuple[str, float, float]],  # [(name, lat, lon), ...]
        lead_days: int = 0,
    ) -> dict[str, CloudCoverEstimate]:
        """
        Fetch cloud cover for multiple locations in parallel.

        Args:
            locations: List of (location_name, lat, lon) tuples
            lead_days: Days ahead

        Returns:
            Dict of location → CloudCoverEstimate
        """
        import asyncio

        tasks = [
            self.fetch_cloud_cover(lat, lon, name, lead_days)
            for name, lat, lon in locations
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        output: dict[str, CloudCoverEstimate] = {}
        for (name, _, _), result in zip(locations, results):
            if isinstance(result, Exception):
                logger.warning("batch_cloud_fetch_failed", location=name, error=str(result))
                output[name] = self._empty_estimate(name, lead_days * 24)
            elif result is not None and not isinstance(result, BaseException):
                output[name] = result
            else:
                output[name] = self._empty_estimate(name, lead_days * 24)

        return output

    # =========================================================================
    # Helpers
    # =========================================================================

    def _empty_estimate(self, location: str, lead_hours: int) -> CloudCoverEstimate:
        """Return a neutral cloud cover estimate."""
        return CloudCoverEstimate(
            location=location,
            cloud_cover_pct=50.0,
            source="none",
            lead_hours=lead_hours,
            confidence=0.1,
        )

    def get_cached_estimate(self, location: str, lead_days: int = 0) -> CloudCoverEstimate | None:
        """Get cached cloud cover estimate."""
        return self._cloud_cache.get(f"{location}:{lead_days}")
