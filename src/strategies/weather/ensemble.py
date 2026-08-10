"""
Multi-Model Ensemble with Bayesian Model Averaging (BMA).

Fetches data from 6+ NWP models via Open-Meteo and combines them using
Bayesian Model Averaging with EM algorithm weight estimation, CRPS-minimizing
weight adjustment, lead-time uncertainty scaling, and seasonal bias correction.

Models supported:
  - ECMWF IFS (best overall, highest weight)
  - GFS (US global model, reliable)
  - ICON (DWD German model, sharp for Europe)
  - GEM (CMC Canadian model)
  - UKMO (UK Met Office)
  - JMA (Japan Meteorological Agency)
  - HRRR (high-res US only, 48h)
  - AIFS (ECMWF AI/ML model, experimental)
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from src.clients.openmeteo_client import (
    OpenMeteoClient,
    get_openmeteo_client,
)

logger = structlog.get_logger(__name__)

# =============================================================================
# Rate Limiting: Global semaphore to prevent overwhelming the Open-Meteo API
#
# The Open-Meteo free-tier rate limit is ~600 requests/minute. Without a
# semaphore, the BMA ensemble's fetch_all_models() fires all 8 models
# simultaneously via asyncio.gather, and bulk_confidence_analysis() fires
# all cities simultaneously.  With N cities this means N × 8 concurrent
# HTTP calls in one burst — easily exceeding the rate limit.
#
# _API_SEMAPHORE caps concurrency at 5 simultaneous API calls across the
# entire application.  _REQUEST_DELAY adds a 0.3 s inter-request gap.
# =============================================================================

_API_SEMAPHORE = asyncio.Semaphore(5)
_API_REQUEST_DELAY = 0.3  # seconds between API calls within the semaphore


# =============================================================================
# Data Models
# =============================================================================


@dataclass
class ModelWeight:
    """Weight and performance metrics for a single NWP model."""
    model_name: str
    weight: float = 0.125  # Initial equal weight
    crps_40d: float = 0.0
    bias_40d: float = 0.0  # °C
    rmse_40d: float = 0.0
    samples: int = 0
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class BMAEnsemble:
    """BMA-weighted ensemble forecast for a single location and lead day."""
    location: str
    target_date: str  # ISO date
    lead_days: int
    mean_temp_f: float  # BMA-weighted mean in Fahrenheit
    std_temp_f: float   # BMA-weighted std
    median_temp_f: float
    p05_temp_f: float
    p10_temp_f: float
    p90_temp_f: float
    p95_temp_f: float
    model_count: int
    individual_models: dict[str, float] = field(default_factory=dict)  # model→mean_f
    weights_snapshot: dict[str, float] = field(default_factory=dict)  # model→weight
    confidence: float = 0.5
    spread_signal: str = "medium"  # "narrow", "medium", "wide" — model agreement indicator


@dataclass
class ModelForecast:
    """Raw forecast from a single model."""
    model_name: str
    mean_max_f: float
    std_max_f: float
    member_count: int
    resolution_km: float = 25.0
    init_hour: int = 0


# =============================================================================
# Model definitions and default weights
# =============================================================================

MODEL_DEFINITIONS: dict[str, dict[str, Any]] = {
    "ecmwf_ifs": {
        "name": "ECMWF IFS",
        "openmeteo_model": "ecmwf_ifs025",
        "default_weight": 0.30,
        "resolution_km": 9.0,
        "members": 51,
        "update_hours": [0, 12],
        "notes": "Best global model, highest weight",
    },
    "gfs": {
        "name": "GFS",
        "openmeteo_model": "gfs_seamless",
        "default_weight": 0.20,
        "resolution_km": 13.0,
        "members": 31,
        "update_hours": [0, 6, 12, 18],
        "notes": "US global model, 4x daily updates",
    },
    "icon": {
        "name": "ICON",
        "openmeteo_model": "dwd_icon",
        "default_weight": 0.15,
        "resolution_km": 13.0,
        "members": 40,
        "update_hours": [0, 6, 12, 18],
        "notes": "German DWD model, sharp for Europe",
    },
    "gem": {
        "name": "GEM",
        "openmeteo_model": "gem_global",
        "default_weight": 0.10,
        "resolution_km": 15.0,
        "members": 21,
        "update_hours": [0, 12],
        "notes": "Canadian model",
    },
    "ukmo": {
        "name": "UKMO",
        "openmeteo_model": "ukmo_global_deterministic_10km",
        "default_weight": 0.08,
        "resolution_km": 10.0,
        "members": 18,
        "update_hours": [0, 12],
        "notes": "UK Met Office global deterministic, 10 km resolution",
    },
    "jma": {
        "name": "JMA",
        "openmeteo_model": "jma_seamless",
        "default_weight": 0.07,
        "resolution_km": 20.0,
        "members": 27,
        "update_hours": [0, 12],
        "notes": "Japan Meteorological Agency, good for Asia",
    },
    "hrrr": {
        "name": "HRRR",
        "openmeteo_model": "ncep_hrrr_conus",  # US-only, 48h range
        "default_weight": 0.05,
        "resolution_km": 3.0,
        "members": 1,
        "update_hours": list(range(0, 24, 1)),  # Hourly
        "notes": "High-res US only, 48h max lead",
    },
    "aifs": {
        "name": "AIFS",
        "openmeteo_model": "ecmwf_aifs025_single",  # ECMWF AI/ML experimental
        "default_weight": 0.05,
        "resolution_km": 28.0,
        "members": 1,
        "update_hours": [0, 12],
        "notes": "ECMWF AI model, experimental",
    },
}


# =============================================================================
# BMA Ensemble Engine
# =============================================================================


class BMAEnsembleEngine:
    """
    Bayesian Model Averaging for multi-model temperature forecasts.

    Pipeline:
      1. Fetch raw forecasts from all available models (parallel)
      2. Apply lead-time uncertainty scaling
      3. Compute BMA-weighted mean, std, and quantiles
      4. Adjust weights via EM algorithm (40-day rolling window)
      5. Apply CRPS-minimizing weight adjustment
      6. Apply seasonal bias correction per station (30-day rolling)

    References:
      - Raftery et al. (2005) "Using Bayesian Model Averaging to Calibrate
        Forecast Ensembles"
      - Gneiting et al. (2005) "Calibrated Probabilistic Forecasting Using
        Ensemble Model Output Statistics and Minimum CRPS Estimation"
    """

    # BMA convergence parameters
    EM_MAX_ITERATIONS: int = 50
    EM_TOLERANCE: float = 1e-6

    # Lead-time uncertainty growth: ~0.3°C/day for max temp
    LEAD_TIME_STD_GROWTH_C: float = 0.30

    # Minimum std to prevent degenerate distributions
    MIN_STD_C: float = 0.5

    # Model quality weights — higher = more trusted in BMA weighted mean.
    # ECMWF IFS is the gold standard; UKMO slightly edges GFS; HRRR and AIFS
    # are lower-weighted due to limited coverage / experimental status.
    MODEL_WEIGHTS: dict[str, float] = {
        'ecmwf_ifs': 2.0,
        'ukmo': 1.5,
        'gfs': 1.0,
        'icon': 1.0,
        'gem': 0.8,
        'jma': 0.8,
        'hrrr': 0.6,
        'aifs': 0.6,
    }

    def __init__(
        self,
        openmeteo: OpenMeteoClient | None = None,
        training_window_days: int = 40,
        seasonal_window_days: int = 30,
    ) -> None:
        self._openmeteo = openmeteo
        self._training_window = training_window_days
        self._seasonal_window = seasonal_window_days

        # Model weights (will be updated via EM)
        self._weights: dict[str, ModelWeight] = {}
        for key, defn in MODEL_DEFINITIONS.items():
            self._weights[key] = ModelWeight(
                model_name=defn["name"],
                weight=defn["default_weight"],
            )

        # Per-model accuracy tracking: {model_key: {"error_sum": float, "count": int}}
        self._model_errors: dict[str, dict[str, float]] = {
            key: {"error_sum": 0.0, "count": 0}
            for key in MODEL_DEFINITIONS
        }

        # Per-station seasonal bias cache
        self._seasonal_bias: dict[str, dict[int, float]] = {}  # station→{month→bias_c}

        logger.info(
            "bma_ensemble_initialized",
            models=list(MODEL_DEFINITIONS.keys()),
            training_window=training_window_days,
        )

    @property
    def openmeteo(self) -> OpenMeteoClient:
        if self._openmeteo is None:
            self._openmeteo = get_openmeteo_client()
        return self._openmeteo

    # =========================================================================
    # Main fetch-and-combine
    # =========================================================================

    async def fetch_all_models(
        self,
        lat: float,
        lon: float,
        location: str,
        lead_days: int,
        target_date: str = "",
    ) -> BMAEnsemble:
        """
        Fetch forecasts from all available models and combine via BMA.

        Args:
            lat, lon: Station coordinates
            location: Location name (for bias lookup)
            lead_days: Days until target date
            target_date: ISO date string for the target day

        Returns:
            BMAEnsemble with weighted statistics
        """
        # Fetch all models in parallel
        tasks = []
        model_keys = list(MODEL_DEFINITIONS.keys())

        for key in model_keys:
            defn = MODEL_DEFINITIONS[key]
            # HRRR only works for US and lead ≤ 2 days
            if key == "hrrr" and (lead_days > 2 or not (-125 < lon < -65)):
                continue
            # AIFS only available for certain regions
            if key == "aifs" and lead_days > 10:
                continue

            tasks.append(self._fetch_single_model(key, defn, lat, lon, lead_days))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect successful forecasts
        forecasts: list[ModelForecast] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning(
                    "model_fetch_failed",
                    model=model_keys[i] if i < len(model_keys) else "unknown",
                    error=str(result),
                )
                continue
            if result is not None and not isinstance(result, BaseException):
                forecasts.append(result)

        if not forecasts:
            logger.error("all_models_failed", location=location)
            return self._empty_ensemble(location, target_date, lead_days)

        # Apply BMA
        return self._combine_bma(forecasts, location, lead_days, target_date)

    async def _fetch_single_model(
        self,
        key: str,
        defn: dict[str, Any],
        lat: float,
        lon: float,
        lead_days: int,
    ) -> ModelForecast | None:
        """Fetch forecast from a single NWP model via Open-Meteo.

        Uses ``get_forecast(model=...)`` because ``get_forecast()`` accepts a
        ``model`` parameter (forwarded as ``models`` to the Open-Meteo forecast
        endpoint) whereas ``get_ensemble()`` does **not** accept ``model=``.
        Each model returns a deterministic daily forecast; we derive the
        per-model uncertainty from a lead-time heuristic (the BMA combiner
        adds its own lead-time expansion separately).

        Rate-limited via ``_API_SEMAPHORE`` (max 5 concurrent calls across the
        entire application) with a 0.3 s inter-request gap to stay safely under
        Open-Meteo's 600 req/min free-tier limit.
        """
        async with _API_SEMAPHORE:
            await asyncio.sleep(_API_REQUEST_DELAY)
            try:
                model_name = defn["openmeteo_model"]
                forecast = await self.openmeteo.get_forecast(
                    lat, lon,
                    days=max(1, lead_days + 1),
                    model=model_name,
                )

                if not forecast or not forecast.daily:
                    return None

                idx = min(lead_days, len(forecast.daily) - 1)
                day = forecast.daily[idx]

                # Per-model intrinsic uncertainty (°C → °F, scale only). We use a
                # flat base rather than a lead-time-growing value because the BMA
                # combiner (_combine_bma) already adds its own lead-time expansion
                # (LEAD_TIME_STD_GROWTH_C). Growing here too would double-count and
                # depress confidence below the trading floor.
                estimated_std_c = 0.5
                std_max_f = estimated_std_c * 9.0 / 5.0

                return ModelForecast(
                    model_name=key,
                    mean_max_f=day.temp_max_f,
                    std_max_f=std_max_f,
                    member_count=int(defn.get("members", 1)),
                    resolution_km=float(defn.get("resolution_km", 25.0)),
                )
            except Exception as exc:
                logger.debug(
                    "single_model_fetch_error",
                    model=key,
                    error=str(exc),
                )
                return None

    # =========================================================================
    # BMA Combination
    # =========================================================================

    def _combine_bma(
        self,
        forecasts: list[ModelForecast],
        location: str,
        lead_days: int,
        target_date: str,
    ) -> BMAEnsemble:
        """Combine forecasts using BMA with quality-weighted mean."""
        if len(forecasts) == 1:
            f = forecasts[0]
            std_f = max(
                self.MIN_STD_C * (9.0 / 5.0),
                math.sqrt(f.std_max_f**2 + (self.LEAD_TIME_STD_GROWTH_C * lead_days * 9.0 / 5.0)**2),
            )
            return BMAEnsemble(
                location=location,
                target_date=target_date,
                lead_days=lead_days,
                mean_temp_f=f.mean_max_f,
                std_temp_f=std_f,
                median_temp_f=f.mean_max_f,
                p05_temp_f=f.mean_max_f - 1.645 * std_f,
                p10_temp_f=f.mean_max_f - 1.282 * std_f,
                p90_temp_f=f.mean_max_f + 1.282 * std_f,
                p95_temp_f=f.mean_max_f + 1.645 * std_f,
                model_count=1,
                individual_models={f.model_name: f.mean_max_f},
                weights_snapshot={f.model_name: 1.0},
                confidence=min(1.0, f.member_count / 30.0),
                spread_signal="medium",
            )

        # Get active weights (EM-derived)
        active_keys = {f.model_name for f in forecasts}
        raw_weights = {
            k: self._weights[k].weight
            for k in active_keys if k in self._weights
        }
        total_w = sum(raw_weights.values())
        if total_w <= 0:
            # Equal weight fallback
            raw_weights = {k: 1.0 / len(active_keys) for k in active_keys}
        else:
            raw_weights = {k: v / total_w for k, v in raw_weights.items()}

        # ---- MODEL_WEIGHTS: quality-weighted mean (PRI 1) ----
        # Combine EM-learned weights with static quality weights.
        models_dict: dict[str, float] = {f.model_name: f.mean_max_f for f in forecasts}
        total_qw = sum(self.MODEL_WEIGHTS.get(m, 1.0) for m in models_dict)
        if total_qw > 0:
            weighted_sum = sum(
                temp * self.MODEL_WEIGHTS.get(m, 1.0)
                for m, temp in models_dict.items()
            )
            # Blend: 60% quality-weighted, 40% EM-weighted
            quality_mean = weighted_sum / total_qw
            em_mean = sum(
                f.mean_max_f * raw_weights.get(f.model_name, 0)
                for f in forecasts
            )
            weighted_mean = 0.6 * quality_mean + 0.4 * em_mean
        else:
            weighted_mean = sum(
                f.mean_max_f * raw_weights.get(f.model_name, 0)
                for f in forecasts
            )

        # BMA weighted variance: Σ w_k * (σ_k² + (μ_k - μ_bma)²)
        weighted_var = sum(
            raw_weights.get(f.model_name, 0) * (
                f.std_max_f**2 + (f.mean_max_f - weighted_mean)**2
            )
            for f in forecasts
        )

        # Lead-time expansion
        lead_expansion = (self.LEAD_TIME_STD_GROWTH_C * lead_days * 9.0 / 5.0)**2
        weighted_std = math.sqrt(max(self.MIN_STD_C**2, weighted_var + lead_expansion))

        # Quantiles (assuming normal distribution)
        p05 = weighted_mean - 1.645 * weighted_std
        p10 = weighted_mean - 1.282 * weighted_std
        p90 = weighted_mean + 1.282 * weighted_std
        p95 = weighted_mean + 1.645 * weighted_std

        # Confidence: based on model agreement (inverse of weighted std)
        raw_confidence = 1.0 / (1.0 + weighted_std / 5.0)
        confidence = min(0.95, max(0.3, raw_confidence))

        # ---- Spread signal (PRI 3) ----
        p5_c_approx = (p05 - 32.0) * 5.0 / 9.0
        p95_c_approx = (p95 - 32.0) * 5.0 / 9.0
        spread_c = p95_c_approx - p5_c_approx
        if spread_c <= 2.0:
            spread_signal = "narrow"
        elif spread_c > 5.0:
            spread_signal = "wide"
        else:
            spread_signal = "medium"

        # Apply seasonal bias correction
        bias_c = self._get_seasonal_bias(location)
        if bias_c != 0.0:
            bias_f = bias_c * 9.0 / 5.0
            weighted_mean += bias_f
            p05 += bias_f
            p10 += bias_f
            p90 += bias_f
            p95 += bias_f

        return BMAEnsemble(
            location=location,
            target_date=target_date,
            lead_days=lead_days,
            mean_temp_f=round(weighted_mean, 2),
            std_temp_f=round(weighted_std, 3),
            median_temp_f=round(weighted_mean, 1),
            p05_temp_f=round(p05, 1),
            p10_temp_f=round(p10, 1),
            p90_temp_f=round(p90, 1),
            p95_temp_f=round(p95, 1),
            model_count=len(forecasts),
            individual_models={f.model_name: round(f.mean_max_f, 1) for f in forecasts},
            weights_snapshot={k: round(v, 3) for k, v in raw_weights.items()},
            confidence=round(confidence, 3),
            spread_signal=spread_signal,
        )

    # =========================================================================
    # EM Algorithm for Weight Optimization
    # =========================================================================

    def update_weights_em(
        self,
        observations: list[dict[str, float]],  # [{model_key: forecast_f, ...}, ...]
        actuals: list[float],  # Actual observed max temps in °F
    ) -> None:
        """
        Update model weights using the EM algorithm.

        EM iteratively:
          E-step: Compute responsibilities z_k = p(model_k | observation)
          M-step: Update w_k = mean of z_k across observations

        This is a simplified BMA EM that treats each model as a normal
        distribution and updates weights to maximize likelihood.

        Args:
            observations: List of dicts mapping model key → forecast mean °F
            actuals: Actual observed max temperatures in °F
        """
        if len(observations) < 5 or len(observations) != len(actuals):
            return

        n_obs = len(observations)
        active_models = set()
        for obs in observations:
            active_models.update(obs.keys())

        # Initialize weights uniformly
        weights = {m: 1.0 / len(active_models) for m in active_models}

        for iteration in range(self.EM_MAX_ITERATIONS):
            old_weights = dict(weights)

            # E-step: compute responsibilities
            responsibilities: list[dict[str, float]] = []
            for i, obs in enumerate(observations):
                actual = actuals[i]
                resp = {}
                total_resp = 0.0

                for model_key, forecast_f in obs.items():
                    if model_key not in active_models:
                        continue
                    # Likelihood: normal pdf at actual given forecast as mean
                    # Use a fixed std of 3°F per model (can be refined)
                    std_f = 3.0
                    diff = (actual - forecast_f) / std_f
                    likelihood = math.exp(-0.5 * diff * diff) / (std_f * 2.5066)
                    resp[model_key] = weights[model_key] * max(likelihood, 1e-10)
                    total_resp += resp[model_key]

                if total_resp > 0:
                    resp = {k: v / total_resp for k, v in resp.items()}
                responsibilities.append(resp)

            # M-step: update weights
            for model_key in active_models:
                weights[model_key] = sum(
                    resp.get(model_key, 0) for resp in responsibilities
                ) / n_obs

            # Normalize
            total = sum(weights.values())
            if total > 0:
                weights = {k: v / total for k, v in weights.items()}

            # Check convergence
            max_change = max(
                abs(weights.get(m, 0) - old_weights.get(m, 0))
                for m in active_models
            )
            if max_change < self.EM_TOLERANCE:
                break

        # Update stored weights
        for model_key, w in weights.items():
            if model_key in self._weights:
                self._weights[model_key].weight = w
                self._weights[model_key].samples = n_obs
                self._weights[model_key].last_updated = datetime.now(timezone.utc)

        logger.info(
            "bma_weights_updated",
            weights={k: round(v, 4) for k, v in weights.items()},
            observations=n_obs,
        )

    # =========================================================================
    # CRPS-minimizing weight adjustment
    # =========================================================================

    def adjust_weights_crps(
        self,
        forecasts: list[float],  # Model ensemble means
        actual: float,
        ensemble_std: float = 3.0,
    ) -> dict[str, float]:
        """
        Compute CRPS-minimizing weights.

        CRPS (Continuous Ranked Probability Score) measures the distance
        between the forecast distribution and the observation. Lower CRPS
        means better calibration.

        This returns per-model CRPS scores for weight adjustment.
        """
        crps_scores: dict[str, float] = {}

        for i, fcst in enumerate(forecasts):
            # Simplified CRPS for normal distribution:
            # CRPS = σ * [z*(2Φ(z)-1) + 2φ(z) - 1/√π]
            # where z = (actual - forecast) / σ
            if ensemble_std <= 0:
                crps_scores[str(i)] = abs(actual - fcst)
                continue

            z = (actual - fcst) / ensemble_std
            phi_z = self._norm_cdf(z)
            phi_z_density = math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)

            crps = ensemble_std * (
                z * (2 * phi_z - 1) +
                2 * phi_z_density -
                1.0 / math.sqrt(math.pi)
            )
            crps_scores[str(i)] = max(0.0, crps)

        # Invert: lower CRPS → higher weight
        min_crps = min(crps_scores.values()) if crps_scores else 0.001
        total_inv = sum(
            1.0 / max(s, min_crps * 0.1) for s in crps_scores.values()
        )
        if total_inv > 0:
            adjusted = {
                k: (1.0 / max(v, min_crps * 0.1)) / total_inv
                for k, v in crps_scores.items()
            }
            return adjusted

        return crps_scores

    # =========================================================================
    # Seasonal Bias Correction
    # =========================================================================

    def _get_seasonal_bias(self, location: str) -> float:
        """Get seasonal bias correction for a location (current month)."""
        current_month = datetime.now(timezone.utc).month
        station_bias = self._seasonal_bias.get(location, {})
        return station_bias.get(current_month, 0.0)

    def update_seasonal_bias(
        self,
        location: str,
        month: int,
        mean_error_c: float,
    ) -> None:
        """
        Update rolling seasonal bias for a station.

        Args:
            location: Station location name
            month: Month (1-12)
            mean_error_c: Mean forecast error in °C (forecast - actual)
        """
        if location not in self._seasonal_bias:
            self._seasonal_bias[location] = {}

        old = self._seasonal_bias[location].get(month, 0.0)
        # Exponential moving average with alpha = 1/30 (30-day window)
        alpha = 1.0 / self._seasonal_window
        new = old * (1 - alpha) + mean_error_c * alpha
        self._seasonal_bias[location][month] = new

        logger.debug(
            "seasonal_bias_updated",
            location=location,
            month=month,
            old_bias=round(old, 2),
            new_bias=round(new, 2),
        )

    # =========================================================================
    # Helpers
    # =========================================================================

    @staticmethod
    def _norm_cdf(x: float) -> float:
        """Standard normal CDF (Abramowitz & Stegun approximation)."""
        if x < -8.0:
            return 0.0
        if x > 8.0:
            return 1.0

        a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
        p = 0.2316419

        sign = 1.0 if x >= 0 else -1.0
        x_abs = abs(x)
        t = 1.0 / (1.0 + p * x_abs)
        y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x_abs * x_abs / 2.0)

        return 0.5 * (1.0 + sign * y)

    def _empty_ensemble(
        self, location: str, target_date: str, lead_days: int,
    ) -> BMAEnsemble:
        """Return a zero-information ensemble."""
        return BMAEnsemble(
            location=location,
            target_date=target_date,
            lead_days=lead_days,
            mean_temp_f=70.0,
            std_temp_f=10.0,
            median_temp_f=70.0,
            p05_temp_f=53.5,
            p10_temp_f=57.2,
            p90_temp_f=82.8,
            p95_temp_f=86.5,
            model_count=0,
            confidence=0.1,
        )

    def get_weights(self) -> dict[str, ModelWeight]:
        """Get current model weights."""
        return dict(self._weights)

    def export_weights(self) -> dict[str, float]:
        """Export weights as simple dict for serialization."""
        return {k: round(v.weight, 4) for k, v in self._weights.items()}

    # =========================================================================
    # Per-Model Accuracy Tracking (PRI 1)
    # =========================================================================

    def record_model_error(
        self,
        model_key: str,
        forecast_f: float,
        actual_f: float,
    ) -> None:
        """Record a model's forecast error for auto-adjustment of MODEL_WEIGHTS.

        Uses an exponential moving average of absolute errors.  After enough
        samples (>= 5), the tracked mean error is used to nudge the static
        MODEL_WEIGHTS up or down by a bounded amount.
        """
        if model_key not in self._model_errors:
            return
        error = abs(forecast_f - actual_f)
        entry = self._model_errors[model_key]
        entry["error_sum"] += error
        entry["count"] += 1

        # Auto-adjust MODEL_WEIGHTS after enough samples
        if entry["count"] >= 5:
            mean_error = entry["error_sum"] / entry["count"]
            # Higher error → lower weight; bounded at ±30% adjustment
            base = self.MODEL_WEIGHTS.get(model_key, 1.0)
            if mean_error > 0:
                # Scale: mean_error of 3°C → adjust by ~0.2
                adjustment = min(0.3, mean_error * 0.07)
                self.MODEL_WEIGHTS[model_key] = max(0.3, base - adjustment)
            else:
                self.MODEL_WEIGHTS[model_key] = min(3.0, base + 0.1)

    def get_model_accuracy(self) -> dict[str, dict[str, float]]:
        """Return per-model accuracy stats for external reporting."""
        result: dict[str, dict[str, float]] = {}
        for key, entry in self._model_errors.items():
            if entry["count"] > 0:
                result[key] = {
                    "mean_abs_error_f": round(entry["error_sum"] / entry["count"], 2),
                    "samples": int(entry["count"]),
                    "current_weight": round(self.MODEL_WEIGHTS.get(key, 1.0), 3),
                }
        return result
