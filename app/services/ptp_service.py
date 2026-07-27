"""
PTP service for loading and normalizing Product Test Parameters.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.database.operations import load_test_parameters

logger = logging.getLogger(__name__)

PTP_DUMP_DIR = Path(__file__).resolve().parents[2] / "docs" / "reference" / "ptp_dumps"

# Optional Postgres PTP repository built once from config (wasco_instruments).
_ptp_repository: Any = None
_ptp_repository_config_id: Optional[int] = None


def configure_ptp_repository(repository: Any) -> None:
    """Install a shared PtpRepository used by load_ptp_from_db when present."""
    global _ptp_repository, _ptp_repository_config_id
    _ptp_repository = repository
    _ptp_repository_config_id = id(repository) if repository is not None else None
    if repository is None:
        logger.info("PTP repository disabled — using SQL Server ProductTestParameters")
    else:
        logger.info("PTP repository configured (Postgres mirror via wasco-instruments)")


def get_ptp_repository() -> Any:
    return _ptp_repository

UNITS_MAP = {
    "1": "PSI",
    "12": "mTorr",
    "13": "Torr",
    "14": "mmHg",
    "15": "INHG",
    "19": "mmHg @ 0 C",
    "21": "Torr",
}

ATMOSPHERE_BY_UNIT = {
    "PSI": 14.7,
    "PSIA": 14.7,
    "TORR": 760.0,
    "MTORR": 760000.0,
    "MMHG": 760.0,
    "MMHG @ 0 C": 760.0,
    "INHG": 29.92,
}


def convert_pressure(value: float, from_units: Optional[str], to_units: Optional[str]) -> float:
    """Convert a pressure value between unit labels."""
    if from_units is None or to_units is None:
        return value
    from_label = _normalize_unit_label(from_units)
    to_label = _normalize_unit_label(to_units)
    if from_label == to_label:
        return value

    psi_value = _to_psi(value, from_label)
    return _from_psi(psi_value, to_label)


REQUIRED_PTP_FIELDS = {
    "ActivationTarget": "ActivationTarget",
    "IncreasingLowerLimit": "IncreasingLowerLimit",
    "IncreasingUpperLimit": "IncreasingUpperLimit",
    "DecreasingLowerLimit": "DecreasingLowerLimit",
    "DecreasingUpperLimit": "DecreasingUpperLimit",
    "ResetBandLowerLimit": "ResetBandLowerLimit",
    "ResetBandUpperLimit": "ResetBandUpperLimit",
    "TargetActivationDirection": "TargetActivationDirection",
    "UnitsOfMeasure": "UnitsOfMeasure",
    "CommonTerminal": "CommonTerminal",
    "NormallyOpenTerminal": "NormallyOpenTerminal",
    "NormallyClosedTerminal": "NormallyClosedTerminal",
}


@dataclass(frozen=True)
class TestSetup:
    part_id: str
    sequence_id: str
    units_code: Optional[str]
    units_label: Optional[str]
    activation_direction: Optional[str]
    activation_target: Optional[float]
    pressure_reference: Optional[str]
    terminals: Dict[str, Optional[int]]
    bands: Dict[str, Dict[str, Optional[float]]]
    raw: Dict[str, Any]


def load_ptp_from_db(part_id: str, sequence_id: str) -> Dict[str, str]:
    """Load raw PTP parameters from Postgres (preferred) or SQL Server fallback."""
    repo = _ptp_repository
    if repo is not None:
        try:
            ptp_data = repo.get_ptp(part_id, sequence=sequence_id)
            seq = ptp_data.primary_sequence
            if seq is not None and seq.parameters:
                params = {str(k): str(v) for k, v in dict(seq.parameters).items()}
                logger.info(
                    "Loaded %d PTP params from Postgres (%s) for %s/%s",
                    len(params),
                    getattr(ptp_data, "source", "unknown"),
                    part_id,
                    sequence_id,
                )
                return params
            logger.warning(
                "Postgres PTP returned no sequence for %s/%s (source=%s)",
                part_id,
                sequence_id,
                getattr(ptp_data, "source", None),
            )
        except Exception as exc:
            logger.warning(
                "Postgres PTP lookup failed for %s/%s (%s); falling back to SQL Server",
                part_id,
                sequence_id,
                exc,
            )

    return load_test_parameters(part_id, sequence_id)


def load_ptp_from_dump(part_id: str, sequence_id: str) -> Dict[str, str]:
    """Load raw PTP parameters from local JSON dumps."""
    params = {}
    if not PTP_DUMP_DIR.exists():
        return params

    normalized_part = (part_id or "").strip()
    seq_key = _normalize_sequence_id(sequence_id)

    for dump_path in PTP_DUMP_DIR.glob("*.json"):
        try:
            data = json.loads(dump_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to parse PTP dump %s: %s", dump_path, exc)
            continue

        if isinstance(data, dict) and "parameters" in data:
            params = _load_from_parameter_list_dump(data, normalized_part, seq_key)
        elif isinstance(data, dict):
            params = _load_from_part_dict_dump(data, normalized_part, seq_key)

        if params:
            logger.info("Loaded %d PTP params from %s", len(params), dump_path.name)
            return params

    return {}


def normalize_ptp(ptp_params: Dict[str, str]) -> Dict[str, Any]:
    """Normalize PTP parameters into typed values."""
    normalized: Dict[str, Any] = {}
    for key, value in ptp_params.items():
        if value is None:
            normalized[key] = None
            continue

        if isinstance(value, str):
            raw_value = value.strip()
        else:
            raw_value = value

        if isinstance(raw_value, str):
            if raw_value.lower() == "inf":
                normalized[key] = float("inf")
                continue
            if raw_value.lower() == "-inf":
                normalized[key] = float("-inf")
                continue

        normalized[key] = _try_parse_number(raw_value)

    return normalized


def derive_test_setup(part_id: str, sequence_id: str, ptp_params: Dict[str, str]) -> TestSetup:
    """Derive a structured test setup from normalized PTP parameters."""
    normalized = normalize_ptp(ptp_params)

    units_code = _to_str(normalized.get("UnitsOfMeasure"))
    units_label = UNITS_MAP.get(units_code) if units_code else None
    pressure_reference_raw = _to_str(normalized.get("PressureReference"))
    pressure_reference = _resolve_pressure_reference(pressure_reference_raw, units_label)

    if pressure_reference_raw is None:
        logger.warning(
            "Missing PressureReference for %s/%s; inferred %s from units=%s",
            part_id,
            sequence_id,
            pressure_reference,
            units_label,
        )
    elif pressure_reference_raw.strip().lower() not in {
        'absolute', 'abs', 'psia', 'a', 'gauge', 'gage', 'psig', 'g'
    }:
        logger.warning(
            "Unrecognized PressureReference %r for %s/%s; inferred %s from units=%s",
            pressure_reference_raw,
            part_id,
            sequence_id,
            pressure_reference,
            units_label,
        )

    terminals = {
        "common": _to_int(normalized.get("CommonTerminal")),
        "normally_open": _to_int(normalized.get("NormallyOpenTerminal")),
        "normally_closed": _to_int(normalized.get("NormallyClosedTerminal")),
    }

    bands = {
        "increasing": {
            "lower": _to_float(normalized.get("IncreasingLowerLimit")),
            "upper": _to_float(normalized.get("IncreasingUpperLimit")),
        },
        "decreasing": {
            "lower": _to_float(normalized.get("DecreasingLowerLimit")),
            "upper": _to_float(normalized.get("DecreasingUpperLimit")),
        },
        "reset": {
            "lower": _to_float(normalized.get("ResetBandLowerLimit")),
            "upper": _to_float(normalized.get("ResetBandUpperLimit")),
        },
    }

    return TestSetup(
        part_id=part_id.strip(),
        sequence_id=_normalize_sequence_id(sequence_id),
        units_code=units_code,
        units_label=units_label,
        activation_direction=_to_str(normalized.get("TargetActivationDirection")),
        activation_target=_to_float(normalized.get("ActivationTarget")),
        pressure_reference=pressure_reference,
        terminals=terminals,
        bands=bands,
        raw=normalized,
    )


def validate_ptp_params(ptp_params: Dict[str, str]) -> Tuple[bool, List[str]]:
    """Validate that core PTP parameters exist and are well-formed."""
    normalized = normalize_ptp(ptp_params)
    errors: List[str] = []

    for key, label in REQUIRED_PTP_FIELDS.items():
        if key not in normalized or normalized.get(key) in ("", None):
            errors.append(f"Missing {label}")

    direction = _to_str(normalized.get("TargetActivationDirection"))
    if direction and direction.lower() not in ("increasing", "decreasing"):
        errors.append("TargetActivationDirection must be Increasing or Decreasing")

    units_code = _to_str(normalized.get("UnitsOfMeasure"))
    if units_code and units_code not in UNITS_MAP:
        errors.append(f"UnitsOfMeasure '{units_code}' is not mapped")

    terminals = (
        ("CommonTerminal", _to_int(normalized.get("CommonTerminal"))),
        ("NormallyOpenTerminal", _to_int(normalized.get("NormallyOpenTerminal"))),
        ("NormallyClosedTerminal", _to_int(normalized.get("NormallyClosedTerminal"))),
    )
    terminal_values: Dict[str, Optional[int]] = {}
    for label, value in terminals:
        terminal_values[label] = value
        if value is None:
            errors.append(f"{label} must be a number")
    common_terminal = terminal_values.get("CommonTerminal")
    no_terminal = terminal_values.get("NormallyOpenTerminal")
    nc_terminal = terminal_values.get("NormallyClosedTerminal")
    if common_terminal is not None and not 1 <= common_terminal <= 9:
        errors.append("CommonTerminal must be a DB9 pin from 1 to 9")
    for label, value in (
        ("NormallyOpenTerminal", no_terminal),
        ("NormallyClosedTerminal", nc_terminal),
    ):
        if value is not None and value != 0 and not 1 <= value <= 9:
            errors.append(f"{label} must be 0 or a DB9 pin from 1 to 9")
    if no_terminal == 0 and nc_terminal == 0:
        errors.append("At least one of NormallyOpenTerminal or NormallyClosedTerminal must be connected")
    if common_terminal is not None and common_terminal in {no_terminal, nc_terminal}:
        errors.append("CommonTerminal must be different from NO and NC terminals")

    for label in (
        "ActivationTarget",
        "IncreasingLowerLimit",
        "IncreasingUpperLimit",
        "DecreasingLowerLimit",
        "DecreasingUpperLimit",
        "ResetBandLowerLimit",
        "ResetBandUpperLimit",
    ):
        if normalized.get(label) in ("", None):
            continue
        value = _to_float(normalized.get(label))
        if value is None:
            errors.append(f"{label} must be a number")

    _validate_band_limits(
        errors,
        "Increasing",
        _to_float(normalized.get("IncreasingLowerLimit")),
        _to_float(normalized.get("IncreasingUpperLimit")),
    )
    _validate_band_limits(
        errors,
        "Decreasing",
        _to_float(normalized.get("DecreasingLowerLimit")),
        _to_float(normalized.get("DecreasingUpperLimit")),
    )
    _validate_band_limits(
        errors,
        "Reset",
        _to_float(normalized.get("ResetBandLowerLimit")),
        _to_float(normalized.get("ResetBandUpperLimit")),
    )

    return (len(errors) == 0, errors)


def build_pressure_visualization(
    test_setup: TestSetup,
    ui_config: Dict[str, Any],
    atmosphere_override: Optional[float] = None,
    display_units_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Build pressure bar visualization settings from a test setup.
    
    Args:
        test_setup: Test setup configuration
        ui_config: UI configuration dictionary
        atmosphere_override: Optional barometric pressure override from Alicat (if available)
    """
    pressure_bar = ui_config.get("pressure_bar", {})

    # Map bands based on activation direction.
    # For Decreasing-direction switches, activation occurs on the "decreasing"
    # band and deactivation occurs on the "increasing" band.
    direction = (test_setup.activation_direction or '').strip().lower()
    activation_label, deactivation_label = _activation_display_labels(direction)
    if direction == 'decreasing':
        activation_band_raw = _band_from_limits(test_setup.bands.get("decreasing", {}))
        deactivation_band_raw = _band_from_limits(test_setup.bands.get("increasing", {}))
    else:
        activation_band_raw = _band_from_limits(test_setup.bands.get("increasing", {}))
        deactivation_band_raw = _band_from_limits(test_setup.bands.get("decreasing", {}))
    
    # Convert bands to PSI for display
    units_label = test_setup.units_label or "PSI"
    activation_band = None
    if activation_band_raw:
        lower_psi = _to_psi(activation_band_raw[0], units_label)
        upper_psi = _to_psi(activation_band_raw[1], units_label)
        activation_band = (lower_psi, upper_psi)
    
    deactivation_band = None
    if deactivation_band_raw:
        lower_psi = _to_psi(deactivation_band_raw[0], units_label)
        upper_psi = _to_psi(deactivation_band_raw[1], units_label)
        deactivation_band = (lower_psi, upper_psi)

    # Use Alicat barometric override if available, otherwise fall back to unit-based constant
    pressure_ref = (test_setup.pressure_reference or '').strip().lower()
    baro_guess = (
        float(atmosphere_override)
        if atmosphere_override is not None and math.isfinite(atmosphere_override)
        else 14.7
    )
    from app.services.sweep_utils import ptp_limits_use_psia_scale

    psia_scale_limits = bool(
        ptp_limits_use_psia_scale(test_setup, {}, baro_guess)
        and _normalize_unit_label(units_label) in {'PSI', 'PSIA'}
    )
    if psia_scale_limits:
        pressure_ref = 'absolute'
        atmosphere = baro_guess
        if display_units_override is None and (test_setup.units_label or '').upper() == 'PSI':
            display_units_override = 'PSIA'
    elif pressure_ref == 'gauge':
        atmosphere = 0.0  # Atmosphere is 0 in gauge units
    elif atmosphere_override is not None and math.isfinite(atmosphere_override):
        atmosphere = atmosphere_override
    else:
        atmosphere = _get_atmosphere_psi(test_setup.units_label, test_setup.pressure_reference)
    
    min_psi, max_psi = _compute_scale(atmosphere, test_setup, pressure_ref)
    # For gauge reference, keep min at 0 (atmosphere at bottom)
    # For absolute reference, adjust based on bands
    if pressure_ref != 'gauge':
        min_psi = _adjust_min_for_bands(min_psi, activation_band, deactivation_band)
    activation_band = _clamp_band_to_scale(activation_band, min_psi, max_psi)
    deactivation_band = _clamp_band_to_scale(deactivation_band, min_psi, max_psi)
    activation_band, deactivation_band = _position_bands_by_direction(
        activation_band, deactivation_band, direction, min_psi, max_psi,
    )

    display_units = display_units_override or test_setup.units_label or "PSI"
    min_display = convert_pressure(min_psi, "PSI", display_units)
    max_display = convert_pressure(max_psi, "PSI", display_units)
    activation_band_display = None
    if activation_band:
        activation_band_display = (
            convert_pressure(activation_band[0], "PSI", display_units),
            convert_pressure(activation_band[1], "PSI", display_units),
        )
    deactivation_band_display = None
    if deactivation_band:
        deactivation_band_display = (
            convert_pressure(deactivation_band[0], "PSI", display_units),
            convert_pressure(deactivation_band[1], "PSI", display_units),
        )
    atmosphere_display = convert_pressure(atmosphere, "PSI", display_units)

    return {
        "min_psi": min_display,
        "max_psi": max_display,
        "activation_band": activation_band_display,
        "deactivation_band": deactivation_band_display,
        "activation_label": activation_label,
        "deactivation_label": deactivation_label,
        "atmosphere_psi": atmosphere_display,
        "show_atmosphere_reference": pressure_bar.get("show_atmosphere_reference", True),
        "show_acceptance_bands": pressure_bar.get("show_acceptance_bands", True),
        "show_measured_points": pressure_bar.get("show_measured_points", True),
    }


def _activation_display_labels(direction: str) -> Tuple[str, str]:
    """Return compact labels pairing activation terms with sweep direction."""
    if direction == 'decreasing':
        return 'ACT/DEC', 'DEACT/INC'
    return 'ACT/INC', 'DEACT/DEC'


def _normalize_sequence_id(sequence_id: str) -> str:
    if sequence_id is None:
        return ""
    seq = str(sequence_id).strip()
    try:
        return str(int(seq))
    except ValueError:
        return seq


def _load_from_parameter_list_dump(
    data: Dict[str, Any], part_id: str, seq_key: str
) -> Dict[str, str]:
    params_section = data.get("parameters", {})
    if not isinstance(params_section, dict):
        return {}

    lookup_key = f"{part_id}::{seq_key}"
    records = params_section.get(lookup_key)
    if not isinstance(records, Iterable):
        return {}

    params: Dict[str, str] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        name = record.get("ParameterName")
        value = record.get("ParameterValue")
        if name:
            params[str(name)] = str(value) if value is not None else ""
    return params


def _load_from_part_dict_dump(
    data: Dict[str, Any], part_id: str, seq_key: str
) -> Dict[str, str]:
    part_data = data.get(part_id)
    if not isinstance(part_data, dict):
        return {}

    for key, value in part_data.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        if key.endswith(f"_{seq_key}"):
            return {str(k): str(v) for k, v in value.items()}
    return {}


def _try_parse_number(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        if "." in value or "e" in value.lower():
            return float(value)
        return int(value)
    except ValueError:
        return value


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _validate_band_limits(errors: List[str], label: str, lower: Optional[float], upper: Optional[float]) -> None:
    if lower is None or upper is None:
        return
    if math.isfinite(lower) and math.isfinite(upper) and lower > upper:
        errors.append(f"{label} band lower limit exceeds upper limit")


def _band_from_limits(band: Dict[str, Optional[float]]) -> Optional[Tuple[float, float]]:
    lower = band.get("lower")
    upper = band.get("upper")
    if lower is None or upper is None:
        return None
    # Allow infinite values (-Inf, Inf) — they'll be clamped to scale
    # bounds later by _clamp_band_to_scale.
    return (lower, upper)


def _clamp_band_to_scale(
    band: Optional[Tuple[float, float]],
    min_value: float,
    max_value: float,
) -> Optional[Tuple[float, float]]:
    if band is None:
        return None
    lower, upper = band
    if not math.isfinite(lower):
        lower = min_value
    if not math.isfinite(upper):
        upper = max_value
    if not (math.isfinite(lower) and math.isfinite(upper)):
        return None
    if lower > upper:
        lower, upper = upper, lower
    return (lower, upper)


def _get_atmosphere_value(units_label: Optional[str], pressure_reference: Optional[str]) -> float:
    if pressure_reference and pressure_reference.lower() == "gauge":
        return 0.0
    if not units_label:
        return 14.7
    return ATMOSPHERE_BY_UNIT.get(units_label.strip().upper(), 14.7)


def _get_atmosphere_psi(units_label: Optional[str], pressure_reference: Optional[str]) -> float:
    atmosphere = _get_atmosphere_value(units_label, pressure_reference)
    if pressure_reference and pressure_reference.lower() == "gauge":
        return 0.0
    return _to_psi(atmosphere, units_label)


@lru_cache(maxsize=32)
def _normalize_unit_label(units_label: Optional[str]) -> str:
    """Normalize unit label with LRU caching for repeated conversions."""
    label = (units_label or "PSI").strip().upper()
    if label in ("PSIG", "PSI G", "PSI(G)"):
        return "PSI"
    return label


def _resolve_pressure_reference(
    pressure_reference: Optional[str],
    units_label: Optional[str],
) -> str:
    """Resolve pressure reference to either 'absolute' or 'gauge'."""
    text = (pressure_reference or '').strip().lower()
    if text in {'absolute', 'abs', 'psia', 'a'}:
        return 'absolute'
    if text in {'gauge', 'gage', 'psig', 'g'}:
        return 'gauge'

    # Units that are always treated as absolute in this system.
    unit = _normalize_unit_label(units_label)
    if unit in {'TORR', 'MTORR', 'MMHG', 'MMHG @ 0 C', 'INHG', 'PSIA'}:
        return 'absolute'

    # Default conservatively to absolute for ambiguous/missing inputs.
    return 'absolute'


def _to_psi(value: float, units_label: Optional[str]) -> float:
    if not units_label or units_label.upper() in ("PSI", "PSIA"):
        return value
    atmosphere_native = ATMOSPHERE_BY_UNIT.get(units_label.strip().upper(), 14.7)
    if atmosphere_native == 0:
        return value
    return value * (14.7 / atmosphere_native)


def _from_psi(value: float, units_label: Optional[str]) -> float:
    if not units_label or units_label.upper() in ("PSI", "PSIA"):
        return value
    atmosphere_native = ATMOSPHERE_BY_UNIT.get(units_label.strip().upper(), 14.7)
    return value * (atmosphere_native / 14.7)


def _collect_scale_values(test_setup: TestSetup) -> List[float]:
    """Collect pressure values from test setup and convert them to PSI.

    The scale is derived from acceptance band limits and the activation
    target only.  ControlPressure1-5 are *operational* Alicat setpoints
    used during cycling and are deliberately excluded — including them
    inflates the scale far beyond the region of interest.
    
    NOTE: ControlPressure1-5 are NOT NEEDED and should be IGNORED.
    They are legacy fields that Stinger never uses.
    """
    values: List[float] = []
    units_label = test_setup.units_label

    if test_setup.activation_target is not None and math.isfinite(test_setup.activation_target):
        values.append(_to_psi(test_setup.activation_target, units_label))

    for band in test_setup.bands.values():
        for key in ("lower", "upper"):
            value = band.get(key)
            if value is not None and math.isfinite(value):
                values.append(_to_psi(value, units_label))

    return values


def _compute_scale(atmosphere: float, test_setup: TestSetup, pressure_reference: Optional[str] = None) -> Tuple[float, float]:
    """Compute adaptive min/max scale for pressure bar.
    
    For gauge units, 0 is at bottom (atmosphere), max extends past highest band.
    For absolute units (PSIA, etc.), min can go to 0 (vacuum).
    """
    values = _collect_scale_values(test_setup)
    values.append(atmosphere)
    finite_values = [value for value in values if math.isfinite(value)]
    
    is_gauge = pressure_reference and pressure_reference.lower() == "gauge"
    
    if not finite_values:
        # Default scale
        if is_gauge:
            return (0.0, 30.0)  # Gauge: 0 at bottom (atmosphere)
        else:
            return (0.0, 30.0)  # Absolute: can go to 0
    
    max_val = max(finite_values)
    
    if math.isclose(min(finite_values), max_val):
        max_val = min(finite_values) + 1.0
    
    if is_gauge:
        # For gauge: start at 0 (atmosphere at bottom), extend past max value
        min_result = 0.0
        padding = max(max_val * 0.2, 1.0)  # 20% padding or at least 1 PSI
        max_result = max_val + padding
    else:
        # For absolute: ensure 0 is included, extend past max
        min_val = min(finite_values)
        min_result = min(min_val, 0.0)
        padding = max((max_val - min_val) * 0.1, 1.0)
        max_result = max_val + padding
        min_result = min_result - padding
    
    return (min_result, max_result)


def _adjust_min_for_bands(
    min_psi: float,
    activation_band: Optional[Tuple[float, float]],
    deactivation_band: Optional[Tuple[float, float]],
) -> float:
    band_limits: List[float] = []
    for band in (activation_band, deactivation_band):
        if not band:
            continue
        band_limits.extend(band)

    # Filter to only finite values (bands may contain -Inf/Inf before clamping)
    finite_limits = [v for v in band_limits if math.isfinite(v)]

    if len(finite_limits) < 2:
        return min_psi

    zone_min = min(finite_limits)
    zone_max = max(finite_limits)
    if math.isclose(zone_min, zone_max):
        return min_psi

    padding = (zone_max - zone_min) * 0.15
    return zone_min - padding


def _position_bands_by_direction(
    activation_band: Optional[Tuple[float, float]],
    deactivation_band: Optional[Tuple[float, float]],
    direction: str,
    min_scale: float,
    max_scale: float,
) -> tuple[Optional[Tuple[float, float]], Optional[Tuple[float, float]]]:
    """Position activation and deactivation bands using the activation direction.

    For **increasing** activation the switch activates at higher pressure and
    deactivates at lower pressure, so activation is drawn above deactivation.
    For **decreasing** activation the switch activates at lower pressure and
    deactivates at higher pressure, so activation is drawn below deactivation.

    This replaces the old ``_separate_overlapping_bands`` which could not
    resolve Inf-clamped bands correctly because it lacked direction context.
    """
    if activation_band is None or deactivation_band is None:
        return activation_band, deactivation_band

    a_low, a_high = activation_band
    d_low, d_high = deactivation_band

    if direction == 'increasing':
        # Activation is ABOVE deactivation.
        # Deactivation must not extend above the activation lower limit.
        d_high = min(d_high, a_low)
        # Activation must not extend below the deactivation upper limit.
        a_low = max(a_low, d_high)
    else:
        # Decreasing: activation is BELOW deactivation.
        # Deactivation must not extend below the activation upper limit.
        d_low = max(d_low, a_high)
        # Activation must not extend above the deactivation lower limit.
        a_high = min(a_high, d_low)

    # Clamp to scale bounds
    a_low = max(min_scale, a_low)
    a_high = min(max_scale, a_high)
    d_low = max(min_scale, d_low)
    d_high = min(max_scale, d_high)

    # Validate bands still have positive extent
    act = (a_low, a_high) if a_high > a_low else None
    deact = (d_low, d_high) if d_high > d_low else None

    return act, deact
