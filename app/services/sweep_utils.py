"""Shared sweep-planning helpers for test execution paths."""
from __future__ import annotations

import math
from typing import Optional

from app.services.pressure_domain import to_absolute_pressure
from app.services.ptp_service import TestSetup, convert_pressure

# Alicat absolute control bottoms out above true 0 PSIA (~2–3 Torr on this stand).
# Commanding / waiting for 0.0000 hangs NC-derived vacuum cycles.
_ABS_VACUUM_FLOOR_TORR = 2.5
_ABS_VACUUM_NEAR_TARGET_TOL_TORR = 3.0


def absolute_vacuum_reachable_floor_psi() -> float:
    """Lowest absolute PSI setpoint expected to be reachable under control."""
    return float(convert_pressure(_ABS_VACUUM_FLOOR_TORR, 'Torr', 'PSI'))


def absolute_vacuum_near_target_tolerance_psi() -> float:
    """Tolerance when deciding a deep-vacuum descending target was reached."""
    return float(convert_pressure(_ABS_VACUUM_NEAR_TARGET_TOL_TORR, 'Torr', 'PSI'))


def clamp_absolute_vacuum_low_target_psi(low_target_psi: float, band_min_psi: float) -> float:
    """Raise unreachable absolute lows while staying below the activation band."""
    floor = absolute_vacuum_reachable_floor_psi()
    below_band = band_min_psi - float(convert_pressure(1.0, 'Torr', 'PSI'))
    raised = max(float(low_target_psi), floor)
    if below_band > floor:
        return min(raised, below_band)
    return raised


def band_midpoint(band: Optional[dict[str, Optional[float]]]) -> Optional[float]:
    """Return midpoint of a pressure band when both limits exist."""
    if not band:
        return None
    lower = band.get('lower')
    upper = band.get('upper')
    if lower is None or upper is None:
        return None
    return (lower + upper) / 2.0


def ptp_limit_is_absolute_psia_scale(value_psi: float, atmosphere_psi: float) -> bool:
    """True when a PTP numeric limit is already on an absolute (PSIA) scale.

    QAL16 vacuum parts in the DB often use ``pressure_reference=gauge`` while
    band limits are stored as sub-atmospheric PSIA values (e.g. 7.8–11 PSIA).
    """
    baro = atmosphere_psi if atmosphere_psi > 1.0 else 14.7
    return value_psi < baro + 2.0


def resolve_sweep_mode(setup: Optional[TestSetup], atmosphere_psi: float) -> str:
    """Determine whether to sweep in pressure or vacuum direction.

    ``atmosphere_psi`` must be barometric absolute PSI (~14.7), not gauge zero.
    """
    if not setup:
        return 'pressure'

    baro = atmosphere_psi if atmosphere_psi > 1.0 else 14.7
    units_label = setup.units_label or 'PSI'
    pressure_ref = (setup.pressure_reference or 'absolute').strip().lower()
    psia_scale_limits = ptp_limits_use_psia_scale(setup, {}, baro)

    for band in setup.bands.values():
        for key in ('lower', 'upper'):
            raw = band.get(key)
            if raw is None or not math.isfinite(float(raw)):
                continue
            val_psi = convert_pressure(float(raw), units_label, 'PSI')
            if psia_scale_limits:
                if val_psi < baro - 0.5:
                    return 'vacuum'
            elif to_absolute_pressure(val_psi, pressure_ref, baro) < baro - 0.5:
                return 'vacuum'

    target = setup.activation_target
    if target is None:
        target = band_midpoint(setup.bands.get('increasing'))
    if target is None:
        target = band_midpoint(setup.bands.get('decreasing'))
    if target is None:
        return 'pressure'

    target_psi = convert_pressure(target, units_label, 'PSI')
    if psia_scale_limits:
        return 'vacuum'
    target_abs = to_absolute_pressure(target_psi, pressure_ref, baro)
    return 'vacuum' if target_abs < baro else 'pressure'


def ptp_limits_use_psia_scale(
    setup: Optional[TestSetup],
    fallback_port_cfg: dict[str, object],
    barometric_psi: float,
) -> bool:
    """True when PTP band limits are stored as sub-atmospheric PSIA (QAL16 vacuum).

    Only applies to PSI/PSIA unit labels. Torr/mmHg gauge PTPs (e.g. SPS01438-02/300
    at 75 mmHg gauge) are true gauge: convert then add baro — do not treat the
    raw mmHg numbers as absolute vacuum.
    """
    if setup:
        unit_label = (setup.units_label or 'PSI').strip().upper()
        if unit_label not in {'PSI', 'PSIA'}:
            return False
        pressure_ref = (setup.pressure_reference or '').strip().lower()
        direction = (setup.activation_direction or '').strip().lower()
        if pressure_ref == 'gauge' and direction.startswith('increas'):
            return False
    _min_psi, max_psi = resolve_sweep_bounds(setup, fallback_port_cfg)
    baro = barometric_psi if barometric_psi > 1.0 else 14.7
    return ptp_limit_is_absolute_psia_scale(max_psi, baro)


def resolve_sweep_bounds(
    setup: Optional[TestSetup],
    fallback_port_cfg: dict[str, object],
) -> tuple[float, float]:
    """Resolve sweep min/max PSI from PTP setup or hardware fallback config."""
    if setup:
        units_label = setup.units_label or 'PSI'
        candidates = []
        for band_name in ('increasing', 'decreasing', 'reset'):
            band = setup.bands.get(band_name, {})
            for key in ('lower', 'upper'):
                raw = band.get(key)
                if raw is not None and math.isfinite(raw):
                    candidates.append(convert_pressure(raw, units_label, 'PSI'))
        if candidates:
            return (min(candidates), max(candidates))

    min_psi = float(fallback_port_cfg.get('transducer_pressure_min', 0.0))
    max_psi = float(fallback_port_cfg.get('transducer_pressure_max', 115.0))
    return (min_psi, max_psi)


def resolve_cycle_ramp_targets(
    *,
    sweep_mode: str,
    activation_direction: int,
    min_psi: float,
    max_psi: float,
    overshoot: float,
    barometric_psi: float,
    hw_min_psi: float,
    hw_max_psi: float,
    pressure_reference: Optional[str] = None,
) -> tuple[float, float]:
    """Return (activation_target, deactivation_target) in test-reference PSI.

    Cycling uses hardware traverse limits, not the expected trip pressure.
    Vacuum: pull to deep vacuum (past the band), then return only far enough
    past the reset/deactivation band to catch the edge quickly.
    """
    baro = barometric_psi if barometric_psi > 1.0 else 14.7
    use_abs_limits = ptp_limit_is_absolute_psia_scale(max_psi, baro)
    ref = str(pressure_reference or '').strip().lower()

    if activation_direction < 0:
        if sweep_mode == 'vacuum':
            if use_abs_limits:
                if max_psi < baro - 5.0:
                    # Entire band is far below atmosphere (e.g. 73–145 Torr absolute).
                    # Stop just below the activation band so the switch trips during ramp.
                    target_activation = max(hw_min_psi, min_psi - overshoot)
                else:
                    # Near-atmosphere absolute band (e.g. 7.8–11 PSIA): traverse well below.
                    past_band = max(overshoot, (baro - min_psi) + overshoot)
                    target_activation = max(0.5, min_psi - past_band)
                target_deactivation = max_psi + overshoot
            elif ref == 'gauge':
                target_activation = min_psi - max(overshoot, max_psi - min_psi + overshoot)
                target_deactivation = 0.0
            else:
                vacuum_margin = max(
                    overshoot,
                    (max_psi - min_psi) + overshoot,
                    convert_pressure(50.0, 'Torr', 'PSI'),
                )
                target_activation = max(0.0, min_psi - vacuum_margin)
                target_deactivation = max_psi + overshoot
        else:
            target_activation = max(0.0, min_psi - overshoot)
            target_deactivation = max_psi + overshoot
    else:
        target_activation = max_psi + overshoot
        target_deactivation = max(0.0, min_psi - overshoot)

    target_activation = min(hw_max_psi, max(hw_min_psi, target_activation))
    target_deactivation = min(hw_max_psi, max(hw_min_psi, target_deactivation))
    return (target_activation, target_deactivation)


def narrow_bounds(
    activation_psi: float,
    deactivation_psi: float,
    min_bound: float,
    max_bound: float,
    factor: float,
    min_pad: float,
) -> tuple[float, float]:
    """Shrink a sweep window around detected activation/deactivation edges."""
    low = min(activation_psi, deactivation_psi)
    high = max(activation_psi, deactivation_psi)
    pad = max(min_pad, abs(activation_psi - deactivation_psi) * factor)
    return (max(min_bound, low - pad), min(max_bound, high + pad))
