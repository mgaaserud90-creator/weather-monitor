"""
Microclimate Corrections — adjusts ensemble temperature forecasts for
local effects around each weather station.

Key corrections:
  1. Urban Heat Island (UHI): cities retain more heat, esp. at night / weak wind
  2. Marine Layer: coastal stations (LAX, SFO) have moderated temps by season
  3. Elevation Lapse Rate: standard -6.5°C/km above sea level
  4. Snow Albedo: fresh snow reflects solar radiation → cooler max temps

These are applied AFTER the BMA ensemble mean to produce the final
station-specific temperature distribution.

References:
  - Oke (1982) "The energetic basis of the urban heat island"
  - NOAA UHI intensity vs. wind speed relationship
  - Standard ICAO lapse rate: -6.5°C/km
  - Baker et al. (1999) "The influence of snow cover on surface temp"
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

from src.strategies.weather.market_parser import STATION_METADATA

logger = structlog.get_logger(__name__)


# =============================================================================
# Data Models
# =============================================================================


@dataclass
class MicroclimateCorrection:
    """Aggregated microclimate correction for a station."""
    location: str
    icao: str
    uhi_correction_f: float = 0.0
    marine_correction_f: float = 0.0
    elevation_correction_f: float = 0.0
    snow_albedo_correction_f: float = 0.0
    total_correction_f: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    confidence: float = 0.7

    def __post_init__(self) -> None:
        self.total_correction_f = round(
            self.uhi_correction_f
            + self.marine_correction_f
            + self.elevation_correction_f
            + self.snow_albedo_correction_f,
            2,
        )
        self.components = {
            "uhi": round(self.uhi_correction_f, 2),
            "marine": round(self.marine_correction_f, 2),
            "elevation": round(self.elevation_correction_f, 2),
            "snow_albedo": round(self.snow_albedo_correction_f, 2),
        }


# =============================================================================
# Microclimate Corrector
# =============================================================================


class MicroclimateCorrector:
    """
    Applies local microclimate corrections to ensemble temperature forecasts.

    Pipeline (applied sequentially):
      1. Elevation correction: standard lapse rate
      2. UHI correction: urban warming proportional to station UHI factor,
         dampened by wind speed (wind disperses UHI)
      3. Marine layer correction: seasonal sea-breeze cooling/warming
      4. Snow albedo correction: surface reflectivity cooling

    All corrections are in the native temperature unit (°F or °C) and
    are converted based on station metadata.
    """

    # Standard environmental lapse rate: -6.5°C per 1000m
    LAPSE_RATE_C_PER_KM: float = -6.5

    # UHI wind-dampening coefficient: UHI dissipates at wind > 5 m/s
    UHI_WIND_THRESHOLD_MS: float = 5.0
    UHI_MAX_DAMPENING: float = 0.85  # 85% reduction at high wind

    # Marine layer: seasonal correction amplitudes in °C
    # Coastal stations are cooler in spring/summer (sea breeze), warmer in fall/winter
    MARINE_SEASONAL_AMPLITUDE_C: float = 2.0

    # Snow albedo: maximum cooling effect in °C
    SNOW_ALBEDO_MAX_COOLING_C: float = -4.0  # ~-7.2°F
    SNOW_ALBEDO_MONTHS: set[int] = {11, 12, 1, 2, 3}  # Northern Hemisphere snow season

    def __init__(self) -> None:
        logger.info("microclimate_corrector_initialized")

    # =========================================================================
    # Main correction entry point
    # =========================================================================

    def calculate_corrections(
        self,
        location: str,
        wind_speed_ms: float = 1.0,
        cloud_cover_pct: float = 50.0,
        has_snow_cover: bool = False,
        month: int | None = None,
    ) -> MicroclimateCorrection:
        """
        Compute all microclimate corrections for a station.

        Args:
            location: Station location name (must match STATION_METADATA key)
            wind_speed_ms: Wind speed at 10m in m/s (from ensemble or METAR)
            cloud_cover_pct: Cloud cover percentage 0-100
            has_snow_cover: True if snow cover is present
            month: Month 1-12 for seasonal corrections (defaults to current)

        Returns:
            MicroclimateCorrection with all components in °F.
        """
        if month is None:
            month = datetime.now(timezone.utc).month

        station = STATION_METADATA.get(location.lower(), {})
        if not station:
            return MicroclimateCorrection(location=location, icao="UNKN")

        icao = str(station.get("icao", "UNKN"))
        uhi_factor = float(station.get("uhi_factor", 0.0))
        marine_influence = bool(station.get("marine_influence", False))
        elevation_m = float(station.get("elevation_m", 0.0))
        unit = str(station.get("unit", "°F"))

        # Compute individual corrections
        uhi_f = self._compute_uhi(uhi_factor, wind_speed_ms, cloud_cover_pct, unit)
        marine_f = self._compute_marine(marine_influence, month, unit)
        elev_f = self._compute_elevation(elevation_m, unit)
        snow_f = self._compute_snow_albedo(has_snow_cover, month, unit)

        # Confidence: based on data quality of inputs
        confidence = 0.7
        if wind_speed_ms > 10:
            confidence = 0.85  # High wind → more confident about UHI reduction
        if has_snow_cover:
            confidence = 0.65  # Snow extent uncertain without satellite

        return MicroclimateCorrection(
            location=location,
            icao=icao,
            uhi_correction_f=round(uhi_f, 2),
            marine_correction_f=round(marine_f, 2),
            elevation_correction_f=round(elev_f, 2),
            snow_albedo_correction_f=round(snow_f, 2),
            confidence=round(confidence, 2),
        )

    # =========================================================================
    # Individual corrections
    # =========================================================================

    def _compute_uhi(
        self,
        uhi_factor: float,
        wind_speed_ms: float,
        cloud_cover_pct: float,
        unit: str,
    ) -> float:
        """
        Urban Heat Island correction.

        UHI effect peaks at night with clear skies and weak wind:
          - Maximum: uhi_factor °C in calm, clear conditions
          - Reduced by: wind speed (disperses heat dome)
          - Reduced by: cloud cover (traps heat everywhere equally)
          - Reduced by: daytime mixing (max temp less affected than min)

        The uhi_factor from STATION_METADATA represents the maximum
        expected UHI intensity in °C under ideal conditions.

        Returns correction in native unit (converted to °F if needed).
        """
        if uhi_factor <= 0:
            return 0.0

        # Wind dampening: UHI reduces as wind increases
        # At 0 m/s: full effect. At UHI_WIND_THRESHOLD_MS: ~15% remaining
        wind_ratio = wind_speed_ms / self.UHI_WIND_THRESHOLD_MS
        wind_dampen = max(0.15, math.exp(-2.0 * wind_ratio))

        # Cloud dampening: clouds reduce UHI by trapping heat everywhere
        # Clear sky (0%): full effect. Overcast (100%): ~40% remaining
        cloud_ratio = cloud_cover_pct / 100.0
        cloud_dampen = 1.0 - 0.6 * cloud_ratio

        # For max temp, UHI is about 40% of full effect
        # (daytime mixing reduces the urban-rural temp differential)
        daytime_factor = 0.4

        correction_c = uhi_factor * wind_dampen * cloud_dampen * daytime_factor

        if unit == "°F":
            return correction_c * 9.0 / 5.0
        return correction_c

    def _compute_marine(
        self,
        marine_influence: bool,
        month: int,
        unit: str,
    ) -> float:
        """
        Marine layer correction for coastal stations.

        Spring/Summer: sea breeze keeps coastal stations cooler
        Fall/Winter: ocean retains heat → coastal stations warmer

        The effect is sinusoidal with a peak in summer (cooling, negative)
        and trough in winter (warming, positive).

        Returns correction in native unit.
        """
        if not marine_influence:
            return 0.0

        # Peak cooling in July (month 7), peak warming in January (month 1)
        # sin wave: max negative at month 7 (July), max positive at month 1 (Jan)
        # sin(pi/2 + (month-1)*2pi/12) → range [-1, 1] with zero-crossing at April/October
        phase = math.pi / 2.0 + (month - 1) * 2.0 * math.pi / 12.0
        seasonal_factor = -math.sin(phase)  # -1 in July, +1 in Jan

        correction_c = self.MARINE_SEASONAL_AMPLITUDE_C * seasonal_factor

        if unit == "°F":
            return correction_c * 9.0 / 5.0
        return correction_c

    def _compute_elevation(self, elevation_m: float, unit: str) -> float:
        """
        Elevation lapse rate correction.

        Standard environmental lapse rate: temperature decreases by
        6.5°C per 1000m of elevation increase.

        This is already accounted for in the raw NWP models, but we
        apply a small correction for stations with unusual siting
        (e.g., Denver at 1656m, Mexico City at 2230m).

        Returns correction in native unit.
        """
        # Minimal correction — NWP models handle elevation well.
        # Only apply for stations > 500m to correct residual model errors.
        if elevation_m <= 500:
            return 0.0

        # Residual lapse rate error: ~0.5°C per km (models are mostly correct)
        residual_lapse = 0.5  # °C/km residual error
        correction_c = -(elevation_m / 1000.0) * residual_lapse

        # Cap at ±3°C
        correction_c = max(-3.0, min(3.0, correction_c))

        if unit == "°F":
            return correction_c * 9.0 / 5.0
        return correction_c

    def _compute_snow_albedo(
        self,
        has_snow_cover: bool,
        month: int,
        unit: str,
    ) -> float:
        """
        Snow albedo cooling correction.

        Fresh snow has albedo 0.8-0.9 (reflects 80-90% of solar radiation),
        vs. bare ground at 0.1-0.2. This can reduce daytime max temps by
        3-8°F (2-4°C) depending on snow depth and solar angle.

        Most effective in late winter/early spring when solar angle is higher
        but snow cover persists.

        Returns correction in native unit.
        """
        if not has_snow_cover:
            return 0.0

        # Snow effect varies by month (solar angle dependent)
        # Max effect: March (month 3) — high sun + snow cover
        # Min effect: December (month 12) — low sun angle
        if month in {2, 3}:
            snow_factor = 1.0  # Full effect: high sun + snow
        elif month in {1, 11}:
            snow_factor = 0.7  # Moderate sun
        elif month in {12}:
            snow_factor = 0.4  # Low sun angle
        elif month in {4, 10}:
            snow_factor = 0.5  # Late/early season
        else:
            snow_factor = 0.3  # Unusual snow event

        correction_c = self.SNOW_ALBEDO_MAX_COOLING_C * snow_factor

        if unit == "°F":
            return correction_c * 9.0 / 5.0
        return correction_c

    # =========================================================================
    # Batch and convenience
    # =========================================================================

    def apply_to_ensemble(
        self,
        bma_mean_f: float,
        location: str,
        wind_speed_ms: float = 1.0,
        cloud_cover_pct: float = 50.0,
        has_snow_cover: bool = False,
        month: int | None = None,
    ) -> tuple[float, MicroclimateCorrection]:
        """
        Apply microclimate corrections to a BMA ensemble mean.

        Returns (corrected_mean_f, MicroclimateCorrection).
        """
        correction = self.calculate_corrections(
            location=location,
            wind_speed_ms=wind_speed_ms,
            cloud_cover_pct=cloud_cover_pct,
            has_snow_cover=has_snow_cover,
            month=month,
        )

        corrected_mean = bma_mean_f + correction.total_correction_f

        logger.debug(
            "microclimate_applied",
            location=location,
            raw_mean_f=round(bma_mean_f, 1),
            total_correction_f=correction.total_correction_f,
            corrected_mean_f=round(corrected_mean, 1),
            components=correction.components,
        )

        return corrected_mean, correction
