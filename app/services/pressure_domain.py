"""Shared pressure/reference conversion and inference helpers."""

from __future__ import annotations

import math
from typing import Optional

from app.hardware.port import AlicatReading, PortReading
from app.services.ptp_service import convert_pressure

_GAUGE_LABELS = {'PSIG', 'PSI G', 'PSI(G)'}


def is_gauge_unit_label(unit_label: Optional[str]) -> bool:
    """Return True when a display unit label represents gauge pressure."""
    label = (unit_label or '').strip().upper()
    return label in _GAUGE_LABELS


def _alicat_status_indicates_vented(reading: AlicatReading) -> bool:
    """True when the Alicat status line looks parked at local atmosphere.

    EXH on Stinger stands is **not** local atmosphere (it vents toward vacuum),
    so EXH must never be treated as a barometric source.
    """
    raw = (reading.raw_response or '').upper()
    if 'EXH' in raw:
        return False
    if 'ATM' in raw:
        return True
    if 'HLD' in raw and reading.pressure is not None:
        # Only idle holds near atmosphere — pressurized HLD (e.g. ~16 PSIA
        # during a gauge cycle) must not become the gauge zero.
        pressure = float(reading.pressure)
        if not (11.0 <= pressure <= 15.2):
            return False
        if reading.setpoint is not None:
            setpoint = float(reading.setpoint)
            return abs(pressure - setpoint) <= 0.75
        return True
    if reading.setpoint is not None and abs(float(reading.setpoint)) <= 0.25:
        return 'HLD' in raw
    return False


def infer_barometric_pressure_from_alicat(
    reading: Optional[AlicatReading],
    *,
    anchor_baro_psi: Optional[float] = None,
) -> Optional[float]:
    """Infer barometric PSI directly from Alicat absolute/gauge fields.

    When ``anchor_baro_psi`` is provided (site config / last good), line-pressure
    heuristics are only accepted within 0.75 PSI of that anchor. This prevents
    pressurized HLD / mid-vent samples from becoming the gauge zero.

    Explicit Alicat barometric index and ``P_abs − P_gauge`` are always trusted
    when plausible (those are device-reported, not line-as-baro guesses).
    """
    if reading is None:
        return None
    direct = _direct_barometric_from_alicat_fields(reading)
    if direct is not None:
        return direct

    raw = (reading.raw_response or '').upper()
    if 'EXH' in raw:
        return None

    if (
        reading.pressure is not None
        and float(reading.pressure) >= 12.0
        and is_plausible_barometric_psi(reading.pressure)
        and _alicat_status_indicates_vented(reading)
    ):
        pressure = float(reading.pressure)
        if is_plausible_barometric_psi(anchor_baro_psi):
            if abs(pressure - float(anchor_baro_psi)) <= 0.75:
                return pressure
            return None
        return pressure
    return None


def _direct_barometric_from_alicat_fields(reading: AlicatReading) -> Optional[float]:
    """Return device-reported baro (index or P−gauge), ignoring line heuristics."""
    if reading.barometric_pressure is not None:
        baro = float(reading.barometric_pressure)
        if is_plausible_barometric_psi(baro):
            return baro
    if reading.pressure is not None and reading.gauge_pressure is not None:
        inferred = float(reading.pressure - reading.gauge_pressure)
        if is_plausible_barometric_psi(inferred):
            return inferred
    return None


def infer_barometric_pressure(
    reading: Optional[PortReading],
    *,
    anchor_baro_psi: Optional[float] = None,
) -> Optional[float]:
    """Infer barometric PSI from an Alicat reading if available."""
    if reading is None or reading.alicat is None:
        return None
    return infer_barometric_pressure_from_alicat(
        reading.alicat,
        anchor_baro_psi=anchor_baro_psi,
    )


DEFAULT_BAROMETRIC_PSI = 14.7
_BARO_MAX_JUMP_PSI = 0.75


def configured_barometric_psi(config: Optional[dict] = None) -> float:
    """Return site-local barometric PSI from config, else sea-level default."""
    if not isinstance(config, dict):
        return DEFAULT_BAROMETRIC_PSI
    hardware = config.get('hardware') if isinstance(config.get('hardware'), dict) else {}
    labjack = hardware.get('labjack') if isinstance(hardware.get('labjack'), dict) else {}
    # Prefer shared / port_a local_barometric when present.
    for section in (
        labjack,
        labjack.get('port_a') if isinstance(labjack.get('port_a'), dict) else {},
        labjack.get('port_b') if isinstance(labjack.get('port_b'), dict) else {},
    ):
        raw = section.get('local_barometric_psi') if isinstance(section, dict) else None
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if is_plausible_barometric_psi(value):
            return value
    return DEFAULT_BAROMETRIC_PSI


def is_plausible_barometric_psi(
    value: Optional[float],
    minimum: float = 8.0,
    maximum: float = 17.5,
) -> bool:
    """Return True when a barometric PSI value looks physically plausible."""
    if value is None or not math.isfinite(value):
        return False
    return minimum <= value <= maximum


def resolve_barometric_psi(
    reading: Optional[PortReading],
    *,
    fallback: float = DEFAULT_BAROMETRIC_PSI,
    last_value: Optional[float] = None,
    max_jump_psi: float = _BARO_MAX_JUMP_PSI,
    anchor_baro_psi: Optional[float] = None,
) -> float:
    """Return a usable barometric PSI for conversions and vacuum safety checks."""
    # Device-reported baro (index / P−gauge) updates the zero without jump clamp.
    if reading is not None and reading.alicat is not None:
        direct = _direct_barometric_from_alicat_fields(reading.alicat)
        if is_plausible_barometric_psi(direct):
            return float(direct)

    anchor = anchor_baro_psi if is_plausible_barometric_psi(anchor_baro_psi) else last_value
    inferred = infer_barometric_pressure(reading, anchor_baro_psi=anchor)
    if is_plausible_barometric_psi(inferred):
        if (
            is_plausible_barometric_psi(last_value)
            and abs(float(inferred) - float(last_value)) > max_jump_psi
        ):
            return float(last_value)
        return float(inferred)
    if is_plausible_barometric_psi(last_value):
        return float(last_value)
    if is_plausible_barometric_psi(anchor_baro_psi):
        return float(anchor_baro_psi)
    return float(fallback)


def to_absolute_pressure(value_psi: float, pressure_reference: Optional[str], barometric_psi: float) -> float:
    """Convert a value in PSI from gauge/absolute reference to absolute PSI."""
    if str(pressure_reference or '').strip().lower() == 'gauge':
        return float(value_psi + barometric_psi)
    return float(value_psi)


def to_display_pressure(
    value_abs_psi: Optional[float],
    unit_label: Optional[str],
    barometric_psi: float,
    pressure_reference: Optional[str] = None,
) -> Optional[float]:
    """Convert absolute PSI to requested display units.

    When *pressure_reference* is ``'gauge'`` and the unit label is not already
    a PSI gauge variant, the result is converted to gauge in the target unit
    (i.e. barometric pressure in the target unit is subtracted).
    """
    if value_abs_psi is None:
        return None
    if is_gauge_unit_label(unit_label):
        return float(value_abs_psi - barometric_psi)
    converted = float(convert_pressure(value_abs_psi, 'PSI', unit_label or 'PSI'))
    if str(pressure_reference or '').strip().lower() == 'gauge':
        baro_in_display_units = float(convert_pressure(barometric_psi, 'PSI', unit_label or 'PSI'))
        return converted - baro_in_display_units
    return converted


def resolve_display_reference(unit_label: Optional[str], default_reference: Optional[str]) -> str:
    """Resolve the implied reference frame for a UI unit label."""
    if is_gauge_unit_label(unit_label):
        return 'gauge'
    if default_reference:
        return str(default_reference).strip().lower()
    if (unit_label or '').strip().upper() == 'PSI' and default_reference:
        return str(default_reference).strip().lower()
    return 'absolute'


def infer_setpoint_reference(
    *,
    setpoint: Optional[float],
    absolute_pressure: Optional[float],
    gauge_pressure: Optional[float],
    barometric_psi: float,
    fallback_reference: Optional[str] = None,
) -> str:
    """Infer whether Alicat setpoint appears gauge- or absolute-referenced."""
    if setpoint is None:
        return str(fallback_reference or 'absolute').strip().lower()
    # Sub-atmospheric PSIA setpoint while the line is still near atmosphere (e.g. 7.8 PSIA
    # target during a vacuum pull from ~14.7 PSIA) must not be treated as PSIG.
    if (
        absolute_pressure is not None
        and 0.0 < setpoint < barometric_psi - 0.5
        and absolute_pressure > setpoint + 1.0
    ):
        return 'absolute'
    if setpoint <= 0.0:
        return 'gauge'
    if gauge_pressure is not None:
        absolute_candidate = gauge_pressure + barometric_psi
        if abs(setpoint - absolute_candidate) < abs(setpoint - gauge_pressure):
            return 'absolute'
        return 'gauge'
    if absolute_pressure is not None:
        gauge_candidate = absolute_pressure - barometric_psi
        if abs(setpoint - absolute_pressure) <= abs(setpoint - gauge_candidate):
            return 'absolute'
        return 'gauge'
    return str(fallback_reference or 'absolute').strip().lower()


def infer_setpoint_abs_psi(
    *,
    setpoint: Optional[float],
    absolute_alicat: Optional[float],
    gauge_pressure: Optional[float],
    barometric_psi: float,
) -> Optional[float]:
    """Infer an absolute-PSI setpoint value from available Alicat fields."""
    if setpoint is None:
        return None

    reference = infer_setpoint_reference(
        setpoint=setpoint,
        absolute_pressure=absolute_alicat,
        gauge_pressure=gauge_pressure,
        barometric_psi=barometric_psi,
        fallback_reference='absolute',
    )
    if reference == 'gauge':
        return float(setpoint + barometric_psi)
    return float(setpoint)


def to_alicat_setpoint_psi(
    target_abs_psi: float,
    *,
    barometric_psi: float,
    setpoint_reference: str,
) -> float:
    """Convert an absolute-PSI target to the value the Alicat ``S`` command expects."""
    ref = str(setpoint_reference or 'absolute').strip().lower()
    if ref == 'gauge':
        gauge_psi = float(target_abs_psi - barometric_psi)
        # Vacuum targets below atmosphere: Alicat rejects negative PSIG; send PSIA.
        if gauge_psi < 0.0:
            return float(target_abs_psi)
        return gauge_psi
    return float(target_abs_psi)


def resolve_alicat_setpoint_reference_for_test(
    *,
    ptp_pressure_reference: Optional[str],
    ptp_units_label: Optional[str] = None,
    config_reference: Optional[str] = None,
    reading: Optional[PortReading] = None,
    barometric_psi: float = DEFAULT_BAROMETRIC_PSI,
) -> str:
    """Pick gauge vs absolute Alicat ``S`` command semantics for an entire test run.

    The stand Alicats use absolute pressure commands by default.  PTP gauge vs
    absolute describes the product limits, not the controller command frame. If
    a future controller is configured for gauge commands, set
    ``hardware.alicat.<port>.setpoint_reference: gauge``.
    """
    configured = str(config_reference or '').strip().lower()
    if configured in {'gauge', 'absolute'}:
        return configured
    return 'absolute'

