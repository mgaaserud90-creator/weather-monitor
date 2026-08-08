"""
Open-Meteo weather API async client — wraps forecast, ensemble, and archive endpoints.

Open-Meteo is FREE (no API key) for non-commercial use.
Rate limit: 10,000 calls/day by default.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import httpx
import structlog
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
)

from src.config.loader import get_config

logger = structlog.get_logger(__name__)

# =============================================================================
# Data Models
# =============================================================================


@dataclass
class DailyForecast:
    """A single day's forecast data."""
    date: date
    temp_max_c: float
    temp_min_c: float
    temp_mean_c: float = 0.0
    cloud_cover_max: float | None = None  # 0-100 %
    cloud_cover_min: float | None = None  # 0-100 %

    def __post_init__(self) -> None:
        if self.temp_mean_c == 0.0:
            self.temp_mean_c = (self.temp_max_c + self.temp_min_c) / 2.0

    @property
    def temp_max_f(self) -> float:
        return self.c_to_f(self.temp_max_c)

    @property
    def temp_min_f(self) -> float:
        return self.c_to_f(self.temp_min_c)

    @property
    def temp_mean_f(self) -> float:
        return self.c_to_f(self.temp_mean_c)

    @property
    def cloud_cover(self) -> float:
        """Mean daily cloud cover percentage (0-100).

        Returns the average of ``cloud_cover_max``/``cloud_cover_min`` when
        available, falling back to whichever is present, and finally to a
        neutral 50.0 %% when cloud-cover data was not requested/available.
        """
        if self.cloud_cover_max is not None and self.cloud_cover_min is not None:
            return (self.cloud_cover_max + self.cloud_cover_min) / 2.0
        if self.cloud_cover_max is not None:
            return self.cloud_cover_max
        if self.cloud_cover_min is not None:
            return self.cloud_cover_min
        return 50.0

    @staticmethod
    def c_to_f(celsius: float) -> float:
        return celsius * 9.0 / 5.0 + 32.0

    @staticmethod
    def f_to_c(fahrenheit: float) -> float:
        return (fahrenheit - 32.0) * 5.0 / 9.0


@dataclass
class WeatherForecast:
    """Forecast response with multiple days of daily data."""
    latitude: float
    longitude: float
    timezone: str
    elevation: float
    daily: list[DailyForecast] = field(default_factory=list)
    generated_at: datetime = field(default_factory=datetime.utcnow)

    def get_day(self, target: date) -> DailyForecast | None:
        """Get forecast for a specific date."""
        for day in self.daily:
            if day.date == target:
                return day
        return None


@dataclass
class EnsembleForecast:
    """
    Multi-model ensemble forecast for uncertainty estimation.

    When the Open-Meteo ensemble API is unavailable, we construct a
    pseudo-ensemble by querying multiple deterministic models and
    computing statistics from the cross-model spread.
    """
    latitude: float
    longitude: float
    timezone: str
    daily: list[EnsembleDaily] = field(default_factory=list)
    num_models: int = 0
    generated_at: datetime = field(default_factory=datetime.utcnow)

    def get_day(self, target: date) -> "EnsembleDaily | None":
        for day in self.daily:
            if day.date == target:
                return day
        return None


@dataclass
class EnsembleDaily:
    """Ensemble statistics for a single day's max temperature."""
    date: date
    mean_max_c: float
    std_max_c: float
    mean_min_c: float = 0.0
    std_min_c: float = 0.0
    percentile_10_c: float = 0.0
    percentile_25_c: float = 0.0
    percentile_75_c: float = 0.0
    percentile_90_c: float = 0.0
    member_count: int = 0
    confidence: float = 0.0  # 0.0–1.0: how reliable is this estimate?

    @property
    def mean_max_f(self) -> float:
        return DailyForecast.c_to_f(self.mean_max_c)

    @property
    def std_max_f(self) -> float:
        return self.std_max_c * 9.0 / 5.0  # Only scale, no offset for std dev

    @property
    def mean_min_f(self) -> float:
        return DailyForecast.c_to_f(self.mean_min_c)


@dataclass
class HistoricalWeather:
    """Historical weather observations for calibration."""
    latitude: float
    longitude: float
    timezone: str
    daily: list[DailyForecast] = field(default_factory=list)


# =============================================================================
# Open-Meteo Client
# =============================================================================


class OpenMeteoClient:
    """
    Async HTTP client wrapper for Open-Meteo weather APIs.

    Endpoints:
      - Forecast:  https://api.open-meteo.com/v1/forecast
      - Ensemble:  https://ensemble-api.open-meteo.com/v1/ensemble
      - Archive:   https://archive-api.open-meteo.com/v1/archive

    All temperatures are returned in Celsius; conversion to Fahrenheit
    is handled by the data model properties.
    """

    FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
    ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
    ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

    # Models used for pseudo-ensemble when true ensemble is unavailable
    PSEUDO_ENSEMBLE_MODELS = [
        "gfs_seamless",                   # GFS global
        "dwd_icon",                        # DWD ICON global
        "gem_global",                      # CMC GEM global
        "jma_seamless",                    # JMA global
        "ecmwf_ifs025",                    # ECMWF IFS
        "ukmo_global_deterministic_10km",  # UK Met Office
        "ncep_hrrr_conus",                 # HRRR CONUS (US only)
        "ecmwf_aifs025_single",            # ECMWF AI/ML
    ]

    # Alternative single-model ensemble via Open-Meteo's model selection
    # These are passed as query params to the standard forecast endpoint
    SINGLE_MODEL_OPTIONS = [
        "gfs_seamless",
        "dwd_icon",
        "gem_global",
        "jma_seamless",
        "ecmwf_ifs025",
        "ukmo_global_deterministic_10km",
        "ncep_hrrr_conus",
        "ecmwf_aifs025_single",
    ]

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._timeout = httpx.Timeout(30.0)

    async def initialize(self) -> None:
        """Initialize the HTTP client."""
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            headers={
                "Accept": "application/json",
                "User-Agent": "PolymarketArbBot-Weather/1.0",
            },
        )
        logger.info("openmeteo_client_initialized")

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("OpenMeteoClient not initialized. Call initialize() first.")
        return self._client

    # =========================================================================
    # Standard Forecast
    # =========================================================================

    async def get_forecast(
        self,
        lat: float,
        lon: float,
        days: int = 7,
        timezone: str = "auto",
        model: str | None = None,
        include_cloud_cover: bool = False,
    ) -> WeatherForecast:
        """
        Fetch daily max/min temperature forecast.

        Args:
            lat: Latitude
            lon: Longitude
            days: Forecast days (1–16)
            timezone: Timezone string or "auto"
            model: Optional model override (e.g., "gfs_seamless")
            include_cloud_cover: When True, also request daily
                ``cloud_cover_max``/``cloud_cover_min`` so the returned
                ``DailyForecast`` objects carry real cloud-cover data.

        Returns:
            WeatherForecast with daily max/min temperatures in Celsius.
        """
        daily_vars = "temperature_2m_max,temperature_2m_min"
        if include_cloud_cover:
            daily_vars += ",cloud_cover_max,cloud_cover_min"

        params: dict[str, Any] = {
            "latitude": lat,
            "longitude": lon,
            "daily": daily_vars,
            "timezone": timezone,
            "forecast_days": min(days, 16),
        }
        if model:
            params["models"] = model

        data = await self._get(self.FORECAST_URL, params)
        return self._parse_forecast(data)

    async def get_multi_model_forecast(
        self,
        lat: float,
        lon: float,
        days: int = 7,
        models: list[str] | None = None,
        timezone: str = "auto",
    ) -> list[tuple[str, WeatherForecast]]:
        """
        Fetch forecasts from multiple models for pseudo-ensemble.

        Returns a list of (model_name, forecast) tuples.

        Models are fetched sequentially with a 0.3 s inter-request gap to
        stay within Open-Meteo's rate limits.
        """
        models = models or self.PSEUDO_ENSEMBLE_MODELS[:3]
        results: list[tuple[str, WeatherForecast]] = []

        for i, model in enumerate(models):
            if i > 0:
                await asyncio.sleep(0.3)
            try:
                forecast = await self.get_forecast(
                    lat=lat, lon=lon, days=days, timezone=timezone, model=model,
                )
                results.append((model, forecast))
            except Exception as exc:
                logger.warning(
                    "model_forecast_failed", model=model, error=str(exc),
                )

        return results

    # =========================================================================
    # Ensemble Forecast
    # =========================================================================

    async def get_ensemble(
        self,
        lat: float,
        lon: float,
        days: int = 7,
        timezone: str = "auto",
    ) -> EnsembleForecast:
        """
        Get ensemble temperature forecast.

        Strategy:
        1. Try the true Open-Meteo ensemble endpoint first.
        2. If it returns null data (common for temperature variables),
           fall back to a pseudo-ensemble built from multiple single-model
           forecasts. This gives us uncertainty via cross-model spread.

        Returns:
            EnsembleForecast with mean, std, and percentiles per day.
        """
        # Attempt 1: True ensemble endpoint
        ensemble = await self._try_native_ensemble(lat, lon, days, timezone)
        if ensemble is not None and len(ensemble.daily) > 0:
            first_day = ensemble.daily[0]
            if first_day.mean_max_c != 0.0 and first_day.member_count > 0:
                logger.info("using_native_ensemble", members=first_day.member_count)
                return ensemble

        # Attempt 2: Pseudo-ensemble from multiple single-model forecasts
        logger.info("falling_back_to_pseudo_ensemble")
        return await self._build_pseudo_ensemble(lat, lon, days, timezone)

    async def _try_native_ensemble(
        self,
        lat: float,
        lon: float,
        days: int,
        timezone: str,
    ) -> EnsembleForecast | None:
        """Try the native Open-Meteo ensemble endpoint."""
        try:
            params: dict[str, Any] = {
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max,temperature_2m_min",
                "forecast_days": min(days, 16),
                "timezone": timezone,
                "models": "ecmwf_ifs04",
            }
            data = await self._get(self.ENSEMBLE_URL, params)
            return self._parse_ensemble(data)
        except Exception as exc:
            logger.debug("native_ensemble_failed", error=str(exc))
            return None

    async def _build_pseudo_ensemble(
        self,
        lat: float,
        lon: float,
        days: int,
        timezone: str,
    ) -> EnsembleForecast:
        """
        Build a pseudo-ensemble from multiple deterministic model runs.

        Computes mean, std, and percentiles across models for each day.
        """
        model_forecasts = await self.get_multi_model_forecast(
            lat=lat, lon=lon, days=days, timezone=timezone,
        )

        if not model_forecasts:
            # Fall back to single GFS forecast with synthetic uncertainty
            forecast = await self.get_forecast(lat, lon, days, timezone)
            return self._single_model_ensemble(forecast)

        return self._compute_pseudo_ensemble(model_forecasts, lat, lon, timezone)

    def _single_model_ensemble(self, forecast: WeatherForecast) -> EnsembleForecast:
        """
        Create ensemble from a single forecast with estimated uncertainty.

        Uses rule-of-thumb: std ≈ 0.5°C per day of lead time.
        """
        daily: list[EnsembleDaily] = []
        for i, day in enumerate(forecast.daily):
            lead_days = i + 1
            # Typical NWP error grows ~0.3-0.5°C per day of lead for max temp
            estimated_std = 0.4 + 0.3 * min(lead_days, 10)
            daily.append(EnsembleDaily(
                date=day.date,
                mean_max_c=day.temp_max_c,
                std_max_c=estimated_std,
                mean_min_c=day.temp_min_c,
                std_min_c=estimated_std * 0.8,
                percentile_10_c=day.temp_max_c - 1.28 * estimated_std,
                percentile_25_c=day.temp_max_c - 0.67 * estimated_std,
                percentile_75_c=day.temp_max_c + 0.67 * estimated_std,
                percentile_90_c=day.temp_max_c + 1.28 * estimated_std,
                member_count=1,
                confidence=max(0.3, 1.0 - 0.05 * lead_days),
            ))

        return EnsembleForecast(
            latitude=forecast.latitude,
            longitude=forecast.longitude,
            timezone=forecast.timezone,
            daily=daily,
            num_models=1,
        )

    def _compute_pseudo_ensemble(
        self,
        model_forecasts: list[tuple[str, WeatherForecast]],
        lat: float,
        lon: float,
        timezone: str,
    ) -> EnsembleForecast:
        """Compute ensemble statistics from multiple model forecasts."""
        import statistics

        # Collect all dates that appear in any forecast
        all_dates: dict[date, list[tuple[float, float]]] = {}
        for _model_name, forecast in model_forecasts:
            for day in forecast.daily:
                if day.date not in all_dates:
                    all_dates[day.date] = []
                all_dates[day.date].append((day.temp_max_c, day.temp_min_c))

        daily: list[EnsembleDaily] = []
        for dt, temps in sorted(all_dates.items()):
            max_temps = [t[0] for t in temps]
            min_temps = [t[1] for t in temps]
            n = len(max_temps)

            if n >= 2:
                mean_max = statistics.mean(max_temps)
                std_max = statistics.stdev(max_temps) if n >= 2 else 0.5
                mean_min = statistics.mean(min_temps)
                std_min = statistics.stdev(min_temps) if n >= 2 else 0.4
            else:
                mean_max = max_temps[0]
                std_max = 0.5
                mean_min = min_temps[0]
                std_min = 0.4

            sorted_max = sorted(max_temps)
            p10 = sorted_max[int(n * 0.1)] if n >= 3 else mean_max - 1.28 * std_max
            p25 = sorted_max[int(n * 0.25)] if n >= 4 else mean_max - 0.67 * std_max
            p75 = sorted_max[int(n * 0.75)] if n >= 4 else mean_max + 0.67 * std_max
            p90 = sorted_max[int(n * 0.9)] if n >= 3 else mean_max + 1.28 * std_max

            # Confidence: higher with more models, decays with lead time
            model_factor = min(1.0, n / 3.0)
            confidence = model_factor

            daily.append(EnsembleDaily(
                date=dt,
                mean_max_c=mean_max,
                std_max_c=std_max,
                mean_min_c=mean_min,
                std_min_c=std_min,
                percentile_10_c=p10,
                percentile_25_c=p25,
                percentile_75_c=p75,
                percentile_90_c=p90,
                member_count=n,
                confidence=confidence,
            ))

        return EnsembleForecast(
            latitude=lat,
            longitude=lon,
            timezone=timezone,
            daily=daily,
            num_models=len(model_forecasts),
        )

    # =========================================================================
    # Historical / Archive
    # =========================================================================

    async def get_historical(
        self,
        lat: float,
        lon: float,
        start: date,
        end: date,
        timezone: str = "auto",
    ) -> HistoricalWeather:
        """
        Fetch historical daily temperature observations.

        Used for bias calibration: compare past forecasts vs actuals.
        """
        params: dict[str, Any] = {
            "latitude": lat,
            "longitude": lon,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "daily": "temperature_2m_max,temperature_2m_min",
            "timezone": timezone,
        }
        data = await self._get(self.ARCHIVE_URL, params)
        return self._parse_historical(data)

    # =========================================================================
    # HTTP helpers
    # =========================================================================

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    async def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """Make a GET request with retry logic."""
        response = await self.client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    def _parse_forecast(self, data: dict[str, Any]) -> WeatherForecast:
        """Parse raw forecast API response into WeatherForecast."""
        daily_data = data.get("daily", {})
        times = daily_data.get("time", [])
        max_temps = daily_data.get("temperature_2m_max", [])
        min_temps = daily_data.get("temperature_2m_min", [])
        cc_max = daily_data.get("cloud_cover_max", [])
        cc_min = daily_data.get("cloud_cover_min", [])

        daily: list[DailyForecast] = []
        for i, time_str in enumerate(times):
            dt = date.fromisoformat(time_str)
            tmax = float(max_temps[i]) if i < len(max_temps) else 0.0
            tmin = float(min_temps[i]) if i < len(min_temps) else 0.0
            cc_max_val = (
                float(cc_max[i])
                if i < len(cc_max) and cc_max[i] is not None
                else None
            )
            cc_min_val = (
                float(cc_min[i])
                if i < len(cc_min) and cc_min[i] is not None
                else None
            )
            daily.append(DailyForecast(
                date=dt, temp_max_c=tmax, temp_min_c=tmin,
                cloud_cover_max=cc_max_val, cloud_cover_min=cc_min_val,
            ))

        return WeatherForecast(
            latitude=float(data.get("latitude", 0)),
            longitude=float(data.get("longitude", 0)),
            timezone=str(data.get("timezone", "UTC")),
            elevation=float(data.get("elevation", 0)),
            daily=daily,
        )

    def _parse_ensemble(self, data: dict[str, Any]) -> EnsembleForecast:
        """
        Parse ensemble API response.

        The Open-Meteo ensemble response structure is similar to forecast
        but with additional statistics. When individual member data is
        not available, we look for mean/spread in the response.
        """
        daily_data = data.get("daily", {})
        times = daily_data.get("time", [])
        max_temps = daily_data.get("temperature_2m_max", [])
        min_temps = daily_data.get("temperature_2m_min", [])

        daily: list[EnsembleDaily] = []
        for i, time_str in enumerate(times):
            dt = date.fromisoformat(time_str)
            tmax = float(max_temps[i]) if i < len(max_temps) and max_temps[i] is not None else None
            tmin = float(min_temps[i]) if i < len(min_temps) and min_temps[i] is not None else None

            if tmax is None or tmin is None:
                # Ensemble returned null — skip this day
                continue

            # Without spread data from the API, use lead-time heuristic
            estimated_std = 0.4 + 0.3 * min(i + 1, 10)
            daily.append(EnsembleDaily(
                date=dt,
                mean_max_c=tmax,
                std_max_c=estimated_std,
                mean_min_c=tmin,
                std_min_c=estimated_std * 0.8,
                percentile_10_c=tmax - 1.28 * estimated_std,
                percentile_25_c=tmax - 0.67 * estimated_std,
                percentile_75_c=tmax + 0.67 * estimated_std,
                percentile_90_c=tmax + 1.28 * estimated_std,
                member_count=51,  # ECMWF ensemble has 51 members
                confidence=0.8,
            ))

        return EnsembleForecast(
            latitude=float(data.get("latitude", 0)),
            longitude=float(data.get("longitude", 0)),
            timezone=str(data.get("timezone", "UTC")),
            daily=daily,
            num_models=1,
        )

    def _parse_historical(self, data: dict[str, Any]) -> HistoricalWeather:
        """Parse historical/archive API response."""
        daily_data = data.get("daily", {})
        times = daily_data.get("time", [])
        max_temps = daily_data.get("temperature_2m_max", [])
        min_temps = daily_data.get("temperature_2m_min", [])

        daily: list[DailyForecast] = []
        for i, time_str in enumerate(times):
            dt = date.fromisoformat(time_str)
            tmax = float(max_temps[i]) if i < len(max_temps) else 0.0
            tmin = float(min_temps[i]) if i < len(min_temps) else 0.0
            daily.append(DailyForecast(date=dt, temp_max_c=tmax, temp_min_c=tmin))

        return HistoricalWeather(
            latitude=float(data.get("latitude", 0)),
            longitude=float(data.get("longitude", 0)),
            timezone=str(data.get("timezone", "UTC")),
            daily=daily,
        )


# =============================================================================
# Singleton
# =============================================================================

_openmeteo_client: OpenMeteoClient | None = None


def get_openmeteo_client() -> OpenMeteoClient:
    """Get the singleton Open-Meteo client."""
    global _openmeteo_client
    if _openmeteo_client is None:
        _openmeteo_client = OpenMeteoClient()
    return _openmeteo_client
