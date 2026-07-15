"""Helpers for selecting the main pressure measurement source."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from app.core.config import (
    MEASUREMENT_SOURCE_ALICAT,
    MEASUREMENT_SOURCE_AUTO,
    MEASUREMENT_SOURCE_TRANSDUCER,
    normalize_measurement_source,
)
from app.hardware.port import PortReading
from app.services.pressure_domain import (
    infer_barometric_pressure,
    is_plausible_barometric_psi,
    to_absolute_pressure,
)
from app.services.sweep_utils import ptp_limit_is_absolute_psia_scale

MEASUREMENT_SOURCE_BLEND = 'blend'

DEFAULT_TRANSDUCER_ONLY_BELOW_PSI = 10.0
DEFAULT_ALICAT_ONLY_ABOVE_PSI = 31.0
DEFAULT_SWITCH_PIVOT_MIN_PSI = 8.0
DEFAULT_SENSOR_DISAGREEMENT_MAX_PSI = 0.1


@dataclass(frozen=True)
class MeasurementSettings:
    """Resolved measurement-source policy from config."""

    preferred_source: str
    fallback_on_unavailable: bool
    transducer_only_below_psi: float = DEFAULT_TRANSDUCER_ONLY_BELOW_PSI
    alicat_only_above_psi: float = DEFAULT_ALICAT_ONLY_ABOVE_PSI
    switch_pivot_min_psi: float = DEFAULT_SWITCH_PIVOT_MIN_PSI
    sensor_disagreement_fallback_enabled: bool = True
    sensor_disagreement_max_psi: float = DEFAULT_SENSOR_DISAGREEMENT_MAX_PSI


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_measurement_settings(config: Dict[str, Any]) -> MeasurementSettings:
    """Return normalized measurement-source settings."""
    measurement = config.get('hardware', {}).get('measurement', {})
    if not isinstance(measurement, dict):
        return MeasurementSettings(
            preferred_source=MEASUREMENT_SOURCE_AUTO,
            fallback_on_unavailable=True,
        )

    transducer_only_below = _coerce_float(
        measurement.get('transducer_only_below_psi'),
        DEFAULT_TRANSDUCER_ONLY_BELOW_PSI,
    )
    alicat_only_above = _coerce_float(
        measurement.get('alicat_only_above_psi'),
        DEFAULT_ALICAT_ONLY_ABOVE_PSI,
    )
    if alicat_only_above < transducer_only_below:
        alicat_only_above = transducer_only_below + 2.0

    switch_pivot_min = _coerce_float(
        measurement.get('switch_pivot_min_psi'),
        DEFAULT_SWITCH_PIVOT_MIN_PSI,
    )

    disagreement_max = _coerce_float(
        measurement.get('sensor_disagreement_max_psi'),
        DEFAULT_SENSOR_DISAGREEMENT_MAX_PSI,
    )

    return MeasurementSettings(
        preferred_source=normalize_measurement_source(
            measurement.get('preferred_source', MEASUREMENT_SOURCE_AUTO),
        ),
        fallback_on_unavailable=bool(measurement.get('fallback_on_unavailable', True)),
        transducer_only_below_psi=transducer_only_below,
        alicat_only_above_psi=alicat_only_above,
        switch_pivot_min_psi=switch_pivot_min,
        sensor_disagreement_fallback_enabled=bool(
            measurement.get('sensor_disagreement_fallback_enabled', True),
        ),
        sensor_disagreement_max_psi=max(0.0, disagreement_max),
    )


def _transducer_pressure_abs_psi(reading: PortReading, barometric_psi: Optional[float]) -> Optional[float]:
    transducer = reading.transducer
    if transducer is None:
        return None
    if barometric_psi is None:
        inferred_baro = infer_barometric_pressure(reading)
        if inferred_baro is not None:
            barometric_psi = inferred_baro
    if barometric_psi is None and str(transducer.pressure_reference or '').strip().lower() == 'gauge':
        return None
    baro = barometric_psi or 0.0
    raw = float(transducer.pressure)
    ref = str(transducer.pressure_reference or '').strip().lower()
    # Some stand configs/PTP handoffs have historically mislabeled absolute
    # transducer readings as gauge. If the raw line pressure is already near
    # local baro, treating it as gauge would double-count atmosphere.
    if ref == 'gauge' and (
        ptp_limit_is_absolute_psia_scale(raw, baro)
        or (is_plausible_barometric_psi(raw) and abs(raw - baro) <= 1.0)
    ):
        return raw
    return to_absolute_pressure(
        value_psi=raw,
        pressure_reference=transducer.pressure_reference,
        barometric_psi=baro,
    )


def _alicat_pressure_abs_psi(reading: PortReading, barometric_psi: Optional[float]) -> Optional[float]:
    alicat = reading.alicat
    if alicat is None:
        return None
    if alicat.pressure is not None:
        return alicat.pressure
    if alicat.gauge_pressure is not None:
        if barometric_psi is None:
            inferred_baro = infer_barometric_pressure(reading)
            if inferred_baro is not None:
                barometric_psi = inferred_baro
        if barometric_psi is not None:
            return to_absolute_pressure(
                value_psi=alicat.gauge_pressure,
                pressure_reference='gauge',
                barometric_psi=barometric_psi,
            )
    return None


def _sensor_disagreement_psi(
    transducer_psi: Optional[float],
    alicat_psi: Optional[float],
) -> Optional[float]:
    """Return |transducer - alicat| when both readings are present."""
    if transducer_psi is None or alicat_psi is None:
        return None
    return abs(float(transducer_psi) - float(alicat_psi))


def _select_alicat_on_sensor_disagreement(
    *,
    transducer_psi: Optional[float],
    alicat_psi: Optional[float],
    settings: MeasurementSettings,
) -> Optional[Tuple[float, str]]:
    """Prefer Alicat when corrected transducer and Alicat disagree beyond tolerance."""
    if not settings.sensor_disagreement_fallback_enabled:
        return None
    gap = _sensor_disagreement_psi(transducer_psi, alicat_psi)
    if gap is None or gap <= settings.sensor_disagreement_max_psi:
        return None
    if alicat_psi is None:
        return None
    return float(alicat_psi), MEASUREMENT_SOURCE_ALICAT


def _switch_requests_alicat_pivot(
    reading: PortReading,
    settings: MeasurementSettings,
    transducer_psi: Optional[float],
    alicat_psi: Optional[float],
) -> bool:
    """Return True when an activated switch near cutover should snap to Alicat."""
    switch = reading.switch
    if switch is None or not switch.switch_activated:
        return False
    reference = transducer_psi if transducer_psi is not None else alicat_psi
    if reference is None:
        return False
    return reference >= settings.switch_pivot_min_psi


def _select_auto_pressure_abs_psi(
    reading: PortReading,
    settings: MeasurementSettings,
    barometric_psi: Optional[float],
) -> Tuple[Optional[float], str]:
    """Blend transducer (low) and Alicat (high) with optional switch pivot."""
    transducer_psi = _transducer_pressure_abs_psi(reading, barometric_psi)
    alicat_psi = _alicat_pressure_abs_psi(reading, barometric_psi)

    low = settings.transducer_only_below_psi
    high = settings.alicat_only_above_psi

    if transducer_psi is not None and transducer_psi < low:
        return transducer_psi, MEASUREMENT_SOURCE_TRANSDUCER

    reference_high = transducer_psi if transducer_psi is not None else alicat_psi
    if reference_high is not None and reference_high >= high:
        if alicat_psi is not None:
            return alicat_psi, MEASUREMENT_SOURCE_ALICAT
        if settings.fallback_on_unavailable and transducer_psi is not None:
            return transducer_psi, MEASUREMENT_SOURCE_TRANSDUCER
        return None, MEASUREMENT_SOURCE_ALICAT

    disagreement = _select_alicat_on_sensor_disagreement(
        transducer_psi=transducer_psi,
        alicat_psi=alicat_psi,
        settings=settings,
    )
    if disagreement is not None:
        return disagreement

    if _switch_requests_alicat_pivot(reading, settings, transducer_psi, alicat_psi):
        if alicat_psi is not None:
            return alicat_psi, MEASUREMENT_SOURCE_ALICAT
        if settings.fallback_on_unavailable and transducer_psi is not None:
            return transducer_psi, MEASUREMENT_SOURCE_TRANSDUCER
        return None, MEASUREMENT_SOURCE_ALICAT

    if transducer_psi is not None and alicat_psi is not None:
        span = high - low
        if span <= 0.0:
            return alicat_psi, MEASUREMENT_SOURCE_ALICAT
        blend_t = (transducer_psi - low) / span
        blend_t = max(0.0, min(1.0, blend_t))
        blended = transducer_psi * (1.0 - blend_t) + alicat_psi * blend_t
        return blended, MEASUREMENT_SOURCE_BLEND

    if transducer_psi is not None:
        return transducer_psi, MEASUREMENT_SOURCE_TRANSDUCER
    if alicat_psi is not None:
        return alicat_psi, MEASUREMENT_SOURCE_ALICAT
    return None, MEASUREMENT_SOURCE_AUTO


def _select_fixed_source_pressure_abs_psi(
    reading: PortReading,
    settings: MeasurementSettings,
    barometric_psi: Optional[float],
) -> Tuple[Optional[float], str]:
    """Legacy fixed transducer or Alicat preference with optional fallback."""
    preferred = settings.preferred_source
    transducer_psi = _transducer_pressure_abs_psi(reading, barometric_psi)
    alicat_psi = _alicat_pressure_abs_psi(reading, barometric_psi)
    disagreement = _select_alicat_on_sensor_disagreement(
        transducer_psi=transducer_psi,
        alicat_psi=alicat_psi,
        settings=settings,
    )
    if disagreement is not None:
        return disagreement

    primary = transducer_psi if preferred == MEASUREMENT_SOURCE_TRANSDUCER else alicat_psi
    if primary is not None or not settings.fallback_on_unavailable:
        return primary, preferred

    secondary_source = (
        MEASUREMENT_SOURCE_ALICAT
        if preferred == MEASUREMENT_SOURCE_TRANSDUCER
        else MEASUREMENT_SOURCE_TRANSDUCER
    )
    secondary = (
        _transducer_pressure_abs_psi(reading, barometric_psi)
        if secondary_source == MEASUREMENT_SOURCE_TRANSDUCER
        else _alicat_pressure_abs_psi(reading, barometric_psi)
    )
    return secondary, secondary_source


def select_main_pressure_abs_psi(
    reading: PortReading,
    settings: MeasurementSettings,
    barometric_psi: Optional[float],
) -> Tuple[Optional[float], str]:
    """Select pressure in absolute PSI using configured source policy."""
    if settings.preferred_source == MEASUREMENT_SOURCE_AUTO:
        return _select_auto_pressure_abs_psi(reading, settings, barometric_psi)
    return _select_fixed_source_pressure_abs_psi(reading, settings, barometric_psi)


def select_ui_pressure_abs_psi(
    reading: PortReading,
    settings: MeasurementSettings,
    barometric_psi: Optional[float],
) -> Tuple[Optional[float], str]:
    """Pressure for the main operator readout — transducer first (physical lag).

    Test/edge logic still uses :func:`select_main_pressure_abs_psi` (auto blend,
    switch pivot, Alicat above range). The large UI number should track the
  transducer so ramps do not look like they instantly match the Alicat setpoint.
    """
    if settings.preferred_source != MEASUREMENT_SOURCE_AUTO:
        return select_main_pressure_abs_psi(reading, settings, barometric_psi)

    transducer_psi = _transducer_pressure_abs_psi(reading, barometric_psi)
    if transducer_psi is not None:
        return transducer_psi, MEASUREMENT_SOURCE_TRANSDUCER
    return select_main_pressure_abs_psi(reading, settings, barometric_psi)
