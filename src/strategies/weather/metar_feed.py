"""
Real-time METAR Integration — fetches current weather observations from
aviationweather.gov for all ICAO stations.

METAR (Meteorological Aerodrome Report) provides:
  - Temperature with 0.1°C precision (T-group)
  - Dewpoint
  - Wind speed and direction
  - Cloud cover (SKC/FEW/SCT/BKN/OVC)
  - Visibility
  - Altimeter setting

For T+0 markets: if today's max is already observed, lock the prediction.
Updates every 30 minutes (METAR frequency). Cache with 5-minute TTL.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from src.strategies.weather.market_parser import STATION_METADATA, LOCATION_MAP

logger = structlog.get_logger(__name__)


# =============================================================================
# Data Models
# =============================================================================


@dataclass
class MetarObservation:
    """A single METAR observation for a station."""
    icao: str
    station_name: str = ""
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    temperature_c: float | None = None  # °C, 0.1°C precision from T-group
    temperature_f: float | None = None  # °F derived
    dewpoint_c: float | None = None
    wind_dir_deg: int | None = None
    wind_speed_kt: int | None = None
    wind_gust_kt: int | None = None
    visibility_sm: float | None = None
    cloud_cover_code: str = ""     # SKC/FEW/SCT/BKN/OVC
    cloud_base_ft: int | None = None
    altimeter_inhg: float | None = None
    raw_text: str = ""
    is_stale: bool = False
    seconds_ago: float = 0.0

    @property
    def temperature_f_safe(self) -> float:
        """Temperature in °F with safe fallback."""
        if self.temperature_f is not None:
            return self.temperature_f
        if self.temperature_c is not None:
            return self.temperature_c * 9.0 / 5.0 + 32.0
        return 70.0

    @property
    def cloud_cover_pct(self) -> float:
        """Estimated cloud cover percentage from METAR code."""
        mapping = {
            "SKC": 0.0, "CLR": 0.0, "FEW": 20.0,
            "SCT": 40.0, "BKN": 65.0, "OVC": 95.0,
            "VV": 100.0,
        }
        return mapping.get(self.cloud_cover_code, 50.0)


@dataclass
class MetarCache:
    """Cache entry for a station's METAR."""
    observation: MetarObservation | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ttl_seconds: int = 300  # 5 minutes


# =============================================================================
# METAR Feed Client
# =============================================================================


class MetarFeed:
    """
    Real-time METAR feed from aviationweather.gov.

    Fetches current observations for all configured ICAO stations.
    Provides caching with configurable TTL and async batch fetching.

    API: https://aviationweather.gov/api/data/metar?ids=KJFK,KLGA,...
    Free, no API key required. Rate limit: ~120 req/min.
    """

    BASE_URL: str = "https://aviationweather.gov/api/data/metar"
    DEFAULT_TTL: int = 300  # 5 minutes
    REQUEST_TIMEOUT: float = 15.0
    MAX_STATIONS_PER_REQUEST: int = 20  # Batch limit per API call

    def __init__(
        self,
        cache_ttl: int = 300,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._cache_ttl = cache_ttl
        self._http = http_client
        self._cache: dict[str, MetarCache] = {}
        self._lock = asyncio.Lock()

        # Collect all ICAO codes from STATION_METADATA
        self._station_icaos: dict[str, str] = {}  # location_name → ICAO
        for loc_name, meta in STATION_METADATA.items():
            icao = str(meta.get("icao", ""))
            if icao and icao != "UNKN":
                self._station_icaos[loc_name] = icao

        logger.info(
            "metar_feed_initialized",
            stations=len(self._station_icaos),
            ttl=cache_ttl,
        )

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.REQUEST_TIMEOUT)
        return self._http

    # =========================================================================
    # Main Fetch Methods
    # =========================================================================

    async def fetch_all_stations(self) -> dict[str, MetarObservation]:
        """
        Fetch METAR for all configured stations.

        Returns dict of location_name → MetarObservation.
        Batches requests in groups of MAX_STATIONS_PER_REQUEST.
        """
        if not self._station_icaos:
            return {}

        # Deduplicate ICAOs (multiple location names may map to same ICAO)
        unique_icaos = sorted(set(self._station_icaos.values()))
        if not unique_icaos:
            return {}

        results: dict[str, MetarObservation] = {}

        # Batch into groups of 20
        for i in range(0, len(unique_icaos), self.MAX_STATIONS_PER_REQUEST):
            batch = unique_icaos[i:i + self.MAX_STATIONS_PER_REQUEST]
            batch_results = await self._fetch_batch(batch)
            results.update(batch_results)

        # Map back to location names
        output: dict[str, MetarObservation] = {}
        for loc_name, icao in self._station_icaos.items():
            if icao in results:
                obs = results[icao]
                obs.station_name = loc_name
                output[loc_name] = obs

        return output

    async def fetch_station(self, location_name: str) -> MetarObservation | None:
        """Fetch METAR for a single station by location name."""
        icao = self._station_icaos.get(location_name.lower())
        if icao is None:
            logger.warning("unknown_station", location=location_name)
            return None

        return await self._fetch_single(icao)

    async def _fetch_batch(
        self, icaos: list[str],
    ) -> dict[str, MetarObservation]:
        """Fetch a batch of METARs for multiple ICAOs."""
        icao_str = ",".join(icaos)
        url = f"{self.BASE_URL}?ids={icao_str}&format=json"

        try:
            response = await self.http.get(url)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            logger.error("metar_http_error", icaos=icao_str[:50], error=str(exc))
            return {}
        except Exception as exc:
            logger.error("metar_fetch_error", icaos=icao_str[:50], error=str(exc))
            return {}

        results: dict[str, MetarObservation] = {}
        if not isinstance(data, list):
            return results

        for entry in data:
            if not isinstance(entry, dict):
                continue
            obs = self._parse_metar_json(entry)
            if obs is not None:
                results[obs.icao] = obs
                # Update cache
                self._cache[obs.icao] = MetarCache(observation=obs)

        return results

    async def _fetch_single(self, icao: str) -> MetarObservation | None:
        """Fetch METAR for a single ICAO, using cache if fresh."""
        # Check cache
        cache_entry = self._cache.get(icao)
        if cache_entry is not None and cache_entry.observation is not None:
            age = (datetime.now(timezone.utc) - cache_entry.fetched_at).total_seconds()
            if age < self._cache_ttl:
                obs = cache_entry.observation
                obs.seconds_ago = age
                obs.is_stale = age > 1800  # Stale if > 30 min
                return obs

        batch = await self._fetch_batch([icao])
        return batch.get(icao)

    # =========================================================================
    # METAR Parsing
    # =========================================================================

    def _parse_metar_json(self, entry: dict[str, Any]) -> MetarObservation | None:
        """Parse a METAR JSON entry from aviationweather.gov."""
        icao = str(entry.get("icaoId", "") or entry.get("station_id", "") or "")
        if not icao:
            return None

        # Parse observation time
        obs_time_str = entry.get("reportTime", "") or entry.get("obsTime", "") or ""
        try:
            obs_time = datetime.fromisoformat(obs_time_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            obs_time = datetime.now(timezone.utc)

        # Temperature: prefer T-group precision over whole-degree temp
        temp_c = None
        raw_text = str(entry.get("rawOb", "") or entry.get("raw_text", "") or "")

        # Try parsing T-group from raw METAR text
        # Format: TXXXXYYYY where XXXX = temp * 10, YYYY = dewpoint * 10
        # Sign: first digit 0 = positive, 1 = negative
        t_match = re.search(r"T(\d{4})(\d{4})", raw_text)
        if t_match:
            temp_code = t_match.group(1)
            temp_val = int(temp_code) / 10.0
            if temp_code[0] == "1":
                temp_val = -temp_val
            temp_c = temp_val

        # Fallback: use temp field from JSON
        if temp_c is None:
            temp_val = entry.get("temp", entry.get("temperature"))
            if temp_val is not None and isinstance(temp_val, (int, float)):
                temp_c = float(temp_val)

        # Dewpoint
        dewpoint_c = None
        if t_match:
            dp_code = t_match.group(2)
            dp_val = int(dp_code) / 10.0
            if dp_code[0] == "1":
                dp_val = -dp_val
            dewpoint_c = dp_val
        if dewpoint_c is None:
            dp_val = entry.get("dewp", entry.get("dewpoint"))
            if dp_val is not None and isinstance(dp_val, (int, float)):
                dewpoint_c = float(dp_val)

        # Wind
        wind_dir = entry.get("wdir", entry.get("wind_dir"))
        wind_speed = entry.get("wspd", entry.get("wind_speed"))
        wind_gust = entry.get("wgst", entry.get("wind_gust"))

        # Visibility
        vis = entry.get("visib", entry.get("visibility"))

        # Cloud cover
        cloud_codes = entry.get("skyc1", entry.get("cloud_base1")) or ""
        cloud_cover = ""
        sky_condition = entry.get("skyCondition", {})
        if isinstance(sky_condition, dict):
            cloud_cover = sky_condition.get("cover", "")
        elif isinstance(sky_condition, str):
            cloud_cover = sky_condition
        if not cloud_cover:
            # Parse from raw text
            for code in ["SKC", "CLR", "FEW", "SCT", "BKN", "OVC", "VV"]:
                if code in raw_text:
                    cloud_cover = code
                    break

        cloud_base = entry.get("skyc1_base", entry.get("cloud_base_ft_agl"))

        # Altimeter
        alt = entry.get("altim", entry.get("altimeter"))

        # Temperature in °F
        temp_f = (temp_c * 9.0 / 5.0 + 32.0) if temp_c is not None else None

        return MetarObservation(
            icao=icao,
            station_name="",
            observed_at=obs_time,
            temperature_c=round(temp_c, 1) if temp_c is not None else None,
            temperature_f=round(temp_f, 1) if temp_f is not None else None,
            dewpoint_c=round(dewpoint_c, 1) if dewpoint_c is not None else None,
            wind_dir_deg=int(wind_dir) if wind_dir is not None else None,
            wind_speed_kt=int(wind_speed) if wind_speed is not None else None,
            wind_gust_kt=int(wind_gust) if wind_gust is not None else None,
            visibility_sm=round(float(vis), 1) if vis is not None else None,
            cloud_cover_code=cloud_cover,
            cloud_base_ft=int(cloud_base) if cloud_base is not None else None,
            altimeter_inhg=round(float(alt), 2) if alt is not None else None,
            raw_text=raw_text,
            seconds_ago=(datetime.now(timezone.utc) - obs_time).total_seconds(),
        )

    # =========================================================================
    # T+0 Lock: if today's max already observed
    # =========================================================================

    def check_t0_lock(
        self,
        location_name: str,
        current_max_f: float,
        predicted_max_f: float,
        metar: MetarObservation | None = None,
    ) -> dict[str, Any]:
        """
        For T+0 markets: if today's observed max already exceeds or equals
        a bucket threshold, lock the prediction.

        Returns:
            {"locked": bool, "locked_max_f": float, "reason": str}
        """
        if metar is None:
            cache_entry = self._cache.get(
                self._station_icaos.get(location_name.lower(), "")
            )
            if cache_entry is not None:
                metar = cache_entry.observation

        metar_temp_f = metar.temperature_f_safe if metar is not None else None

        # If we have METAR and the current temp is already high
        locked_max = current_max_f

        if metar_temp_f is not None and metar_temp_f > current_max_f:
            locked_max = metar_temp_f

        reason = "forecast_active"
        if locked_max >= predicted_max_f * 0.95:
            reason = "observed_near_forecast"
        if locked_max >= predicted_max_f:
            reason = "observed_met_or_exceeded"

        return {
            "locked": reason != "forecast_active",
            "locked_max_f": locked_max,
            "metar_temp_f": metar_temp_f,
            "predicted_max_f": predicted_max_f,
            "reason": reason,
        }

    # =========================================================================
    # Cache Management
    # =========================================================================

    def get_cached(self, location_name: str) -> MetarObservation | None:
        """Get cached METAR for a location."""
        icao = self._station_icaos.get(location_name.lower())
        if icao is None:
            return None
        cache_entry = self._cache.get(icao)
        if cache_entry is not None:
            obs = cache_entry.observation
            if obs is not None:
                obs.seconds_ago = (
                    datetime.now(timezone.utc) - cache_entry.fetched_at
                ).total_seconds()
            return obs
        return None

    def get_cache_stats(self) -> dict[str, Any]:
        """Get cache statistics."""
        total = len(self._cache)
        fresh = 0
        for icao, entry in self._cache.items():
            age = (datetime.now(timezone.utc) - entry.fetched_at).total_seconds()
            if age < self._cache_ttl:
                fresh += 1

        return {
            "total_cached": total,
            "fresh": fresh,
            "stale": total - fresh,
            "configured_stations": len(self._station_icaos),
            "ttl_seconds": self._cache_ttl,
        }

    async def close(self) -> None:
        """Close HTTP client."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None
