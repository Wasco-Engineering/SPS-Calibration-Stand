"""Configuration loader for the standalone quality calibration app."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)


PROFILE_MENSOR_0_30 = 'mensor_0_30'
PROFILE_HIGH_0_115 = 'high_0_115'
PROFILE_CAL10_WCS02075 = 'cal10_wcs02075'

# CAL 10 Rev 000 (WCS02075) — section 5.3.2 test points, high-to-low sweep order.
MIN_VACUUM_SETPOINT_PSIA = 0.05

CAL10_WCS02075_PRESSURE_POINTS_PSIA: list[float] = [
    115.0,
    100.0,
    90.0,
    75.0,
    50.0,
    40.0,
    30.0,
    25.0,
    20.0,
    15.0,
    10.0,
    5.0,
    3.0,
    2.0,
    1.0,
    0.5,
    0.2,
    0.05,
]

DEFAULT_PROFILE_ID = PROFILE_CAL10_WCS02075
DEFAULT_MENSOR_MAX_PSIA = 165.0


@dataclass(frozen=True, slots=True)
class QualitySettings:
    profile_id: str
    profile_label: str
    pressure_points_psia: list[float]
    pressure_tolerance_psia: float
    settle_tolerance_psia: float
    settle_hold_s: float
    settle_timeout_s: float
    static_hold_s: float
    settle_hold_at_or_below_5_psia_s: float
    settle_hold_above_5_psia_s: float
    settle_hold_above_30_psia_s: float
    static_hold_at_or_below_5_psia_s: float
    static_hold_above_5_psia_s: float
    static_hold_above_30_psia_s: float
    static_discard_s: float
    sample_hz: float
    mensor_max_psia: float
    fit_max_psia: float
    alicat_fit_max_psia: float
    require_mensor: bool
    prompt_disconnect_mensor_above_psi: Optional[float]
    capture_raw_during_sweep: bool
    pass_threshold_torr: float
    leak_check_target_psia: float
    leak_check_duration_s: float
    leak_check_sample_hz: float
    leak_check_max_rate_psi_per_min: Optional[float]
    leak_check_ramp_rate_psi_per_s: float
    report_output_dir: Path
    report_template_path: Path
    report_filename_prefix: str
    desktop_output_dir: Path
    also_write_records_path: bool


def get_default_config_path() -> Path:
    from app.core.paths import get_quality_cal_config_path

    return get_quality_cal_config_path()


def load_config(config_path: Optional[Path] = None) -> dict[str, Any]:
    path = config_path or get_default_config_path()
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with open(path, "r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)

    if not isinstance(config, dict):
        raise ValueError("Configuration root must be a mapping")

    validate_config(config)
    return config


def validate_config(config: dict[str, Any]) -> None:
    required = ["app", "hardware", "quality", "logging"]
    for section in required:
        if section not in config:
            raise ValueError(f"Missing required config section: {section}")

    hardware = config.get("hardware", {})
    if "labjack" not in hardware or "alicat" not in hardware:
        raise ValueError('Config "hardware" must define both "labjack" and "alicat"')

    quality_cfg = config.get("quality", {})
    profile_id = str(quality_cfg.get("default_profile_id", DEFAULT_PROFILE_ID))
    points = build_pressure_points_for_profile(profile_id, quality_cfg)
    if not points:
        raise ValueError("quality configuration must define at least one pressure point")


def build_pressure_points(quality_cfg: dict[str, Any]) -> list[float]:
    explicit_points = quality_cfg.get("pressure_points_psia")
    if isinstance(explicit_points, list) and explicit_points:
        points = [_coerce_float(value) for value in explicit_points]
        return _normalize_points(points)

    schedule_cfg = quality_cfg.get("pressure_schedule", {}) or {}
    max_psia = _coerce_float(schedule_cfg.get("max_psia", 30.0))
    dense_up_to = _coerce_float(schedule_cfg.get("dense_up_to_psia", 30.0))
    dense_step = _coerce_float(schedule_cfg.get("dense_step_psia", 1.0))
    medium_up_to = _coerce_float(schedule_cfg.get("medium_up_to_psia", 30.0))
    medium_step = _coerce_float(schedule_cfg.get("medium_step_psia", 2.0))
    high_step = _coerce_float(schedule_cfg.get("high_step_psia", 5.0))
    start_psia = _coerce_float(schedule_cfg.get("start_psia", 0.0))

    points: list[float] = []
    points.extend(_build_range(start_psia, dense_up_to, dense_step))
    points.extend(_build_range(dense_up_to + medium_step, medium_up_to, medium_step))
    points.extend(_build_range(medium_up_to + high_step, max_psia, high_step))
    if max_psia not in points:
        points.append(max_psia)
    return _normalize_points(points, include_zero=True)


def get_profile_ids(config: dict[str, Any]) -> list[str]:
    profiles = (config.get('quality', {}).get('calibration_profiles', {}) or {})
    if isinstance(profiles, dict) and profiles:
        return list(profiles.keys())
    return [PROFILE_CAL10_WCS02075]


def get_profile_config(config: dict[str, Any], profile_id: str) -> dict[str, Any]:
    profiles = (config.get('quality', {}).get('calibration_profiles', {}) or {})
    if not isinstance(profiles, dict):
        raise ValueError('quality.calibration_profiles must be a mapping')
    profile = profiles.get(profile_id)
    if not isinstance(profile, dict):
        raise ValueError(f'Unknown calibration profile: {profile_id}')
    return profile


def build_pressure_points_for_profile(profile_id: str, quality_cfg: dict[str, Any]) -> list[float]:
    """Build pressure point list for a named calibration profile."""
    profiles = quality_cfg.get('calibration_profiles', {}) or {}
    if isinstance(profiles, dict) and profile_id in profiles:
        profile = profiles[profile_id]
        explicit = profile.get('pressure_points_psia')
        if isinstance(explicit, list) and explicit:
            ordered = profile.get('preserve_point_order', True)
            points = [_coerce_float(v) for v in explicit]
            if ordered:
                return _apply_vacuum_floor(_dedupe_preserve_order(points))
            return _normalize_points(points, include_zero=True)

        schedule = profile.get('pressure_schedule', {}) or {}
        max_psia = _coerce_float(schedule.get('max_psia', 30.0))
        dense_up_to = _coerce_float(schedule.get('dense_up_to_psia', max_psia))
        dense_step = _coerce_float(schedule.get('dense_step_psia', 1.0))
        medium_up_to = _coerce_float(schedule.get('medium_up_to_psia', dense_up_to))
        medium_step = _coerce_float(schedule.get('medium_step_psia', 2.0))
        high_step = _coerce_float(schedule.get('high_step_psia', 5.0))
        start_psia = _coerce_float(schedule.get('start_psia', 0.0))
        high_start = _coerce_optional_float(schedule.get('high_range_start_psia'))
        points: list[float] = []
        points.extend(_build_range(start_psia, dense_up_to, dense_step))
        if high_start is not None and high_start > dense_up_to:
            points.extend(_build_range(high_start, max_psia, high_step))
        else:
            points.extend(_build_range(dense_up_to + medium_step, medium_up_to, medium_step))
            points.extend(_build_range(medium_up_to + high_step, max_psia, high_step))
        if max_psia not in points:
            points.append(max_psia)
        return _normalize_points(points, include_zero=True)

    if profile_id == PROFILE_MENSOR_0_30:
        return _normalize_points(_build_range(0.0, 30.0, 1.0), include_zero=True)
    if profile_id == PROFILE_HIGH_0_115:
        high_max_psia = 115.0
        points = _build_range(0.0, 30.0, 1.0)
        points.extend(_build_range(35.0, high_max_psia, 5.0))
        if high_max_psia not in points:
            points.append(high_max_psia)
        return _normalize_points(points, include_zero=True)
    if profile_id == PROFILE_CAL10_WCS02075:
        return _apply_vacuum_floor(_dedupe_preserve_order(list(CAL10_WCS02075_PRESSURE_POINTS_PSIA)))
    raise ValueError(f'Unknown calibration profile: {profile_id}')


def point_timing_for_target(target_psia: float, settings: QualitySettings) -> tuple[float, float]:
    """Return (settle_hold_s, static_hold_s) for a calibration target."""
    if target_psia <= 5.0 + 1e-9:
        return settings.settle_hold_at_or_below_5_psia_s, settings.static_hold_at_or_below_5_psia_s
    if target_psia <= 30.0 + 1e-9:
        return settings.settle_hold_above_5_psia_s, settings.static_hold_above_5_psia_s
    return settings.settle_hold_above_30_psia_s, settings.static_hold_above_30_psia_s


def estimate_profile_duration_s(settings: QualitySettings) -> float:
    """Rough duration estimate for UI display (seconds)."""
    route_overhead_s = 8.0
    typical_settle_s = 4.0
    total = 0.0
    for target_psia in settings.pressure_points_psia:
        settle_hold_s, static_hold_s = point_timing_for_target(target_psia, settings)
        total += route_overhead_s + typical_settle_s + settle_hold_s + static_hold_s
    return total


def parse_quality_settings(
    config: dict[str, Any],
    *,
    profile_id: str | None = None,
) -> QualitySettings:
    quality_cfg = config.get("quality", {})
    report_cfg = quality_cfg.get("report", {}) or {}
    leak_cfg = quality_cfg.get("leak_check", {}) or {}

    output_dir = Path(
        report_cfg.get(
            "output_dir",
            r"I:\Level 5 Documentation\Records\Calibration Certificates",
        )
    )
    template_default = (
        Path(__file__).resolve().parent.parent
        / 'deploy'
        / 'templates'
        / 'qf87'
        / 'QF87_Stinger_TestStand.docx'
    )
    template_raw = Path(report_cfg.get('template_path', str(template_default)))
    if template_raw.is_absolute():
        template_path = template_raw
    else:
        template_path = (Path(__file__).resolve().parent.parent / template_raw).resolve()
    desktop_raw = str(
        report_cfg.get(
            'desktop_output_dir',
            '%USERPROFILE%/Desktop/Stinger/CalibrationReports',
        ),
    )
    desktop_output_dir = Path(
        desktop_raw.replace('%USERPROFILE%', str(Path.home())),
    ).expanduser()

    selected_profile = str(profile_id or quality_cfg.get('default_profile_id', DEFAULT_PROFILE_ID))
    profile_ids = get_profile_ids(config)
    if selected_profile not in profile_ids:
        selected_profile = profile_ids[0] if profile_ids else DEFAULT_PROFILE_ID
    try:
        profile_cfg = get_profile_config(config, selected_profile)
    except ValueError:
        profile_cfg = {}

    mensor_max = _coerce_float(
        profile_cfg.get(
            'mensor_max_psia',
            quality_cfg.get('mensor_max_psia', DEFAULT_MENSOR_MAX_PSIA),
        ),
    )
    fit_max = _coerce_float(profile_cfg.get('fit_max_psia', quality_cfg.get('fit_max_psia', 20.0)))
    pressure_points = build_pressure_points_for_profile(str(selected_profile), quality_cfg)
    alicat_fit_raw = profile_cfg.get('alicat_fit_max_psia', quality_cfg.get('alicat_fit_max_psia'))
    if alicat_fit_raw is None:
        alicat_fit_max = max(pressure_points) if pressure_points else fit_max
    else:
        alicat_fit_max = _coerce_float(alicat_fit_raw)
    prompt_disconnect = profile_cfg.get('prompt_disconnect_mensor_above_psi')
    if prompt_disconnect is not None and prompt_disconnect != '':
        prompt_disconnect_psi: Optional[float] = _coerce_float(prompt_disconnect)
    else:
        prompt_disconnect_psi = None

    return QualitySettings(
        profile_id=str(selected_profile),
        profile_label=str(profile_cfg.get('label', selected_profile)),
        pressure_points_psia=pressure_points,
        pressure_tolerance_psia=_coerce_float(quality_cfg.get("pressure_tolerance_psia", 0.0193)),
        settle_tolerance_psia=_coerce_float(quality_cfg.get("settle_tolerance_psia", 0.05)),
        settle_hold_s=_coerce_float(quality_cfg.get("settle_hold_s", 5.0)),
        settle_timeout_s=_coerce_float(quality_cfg.get("settle_timeout_s", 180.0)),
        static_hold_s=_coerce_float(quality_cfg.get("static_hold_s", 8.0)),
        settle_hold_at_or_below_5_psia_s=_coerce_float(
            quality_cfg.get(
                "settle_hold_at_or_below_5_psia_s",
                quality_cfg.get("settle_hold_s", 5.0),
            ),
        ),
        settle_hold_above_5_psia_s=_coerce_float(
            quality_cfg.get("settle_hold_above_5_psia_s", 1.5),
        ),
        settle_hold_above_30_psia_s=_coerce_float(
            quality_cfg.get("settle_hold_above_30_psia_s", 1.0),
        ),
        static_hold_at_or_below_5_psia_s=_coerce_float(
            quality_cfg.get(
                "static_hold_at_or_below_5_psia_s",
                quality_cfg.get("static_hold_s", 8.0),
            ),
        ),
        static_hold_above_5_psia_s=_coerce_float(
            quality_cfg.get("static_hold_above_5_psia_s", 3.0),
        ),
        static_hold_above_30_psia_s=_coerce_float(
            quality_cfg.get("static_hold_above_30_psia_s", 2.5),
        ),
        static_discard_s=_coerce_float(quality_cfg.get("static_discard_s", 1.0)),
        sample_hz=_coerce_float(quality_cfg.get("sample_hz", 4.0)),
        mensor_max_psia=mensor_max,
        fit_max_psia=fit_max,
        alicat_fit_max_psia=alicat_fit_max,
        require_mensor=bool(profile_cfg.get('require_mensor', True)),
        prompt_disconnect_mensor_above_psi=prompt_disconnect_psi,
        capture_raw_during_sweep=bool(
            profile_cfg.get('capture_raw_during_sweep', quality_cfg.get('capture_raw_during_sweep', True)),
        ),
        pass_threshold_torr=_coerce_float(profile_cfg.get('pass_threshold_torr', 1.0)),
        leak_check_target_psia=_coerce_float(leak_cfg.get("target_psia", 100.0)),
        leak_check_duration_s=_coerce_float(leak_cfg.get("duration_s", 90.0)),
        leak_check_sample_hz=_coerce_float(leak_cfg.get("sample_hz", 4.0)),
        leak_check_max_rate_psi_per_min=_coerce_optional_float(
            leak_cfg.get("max_rate_psi_per_min", 0.2)
        ),
        leak_check_ramp_rate_psi_per_s=_coerce_float(
            leak_cfg.get("ramp_rate_psi_per_s", 8.0)
        ),
        report_output_dir=output_dir,
        report_template_path=template_path,
        report_filename_prefix=str(report_cfg.get("filename_prefix", "QualityCalibration")),
        desktop_output_dir=desktop_output_dir,
        also_write_records_path=bool(report_cfg.get('also_write_records_path', True)),
    )


def setup_logging(config: dict[str, Any]) -> None:
    log_cfg = config.get("logging", {})
    level = getattr(logging, str(log_cfg.get("level", "INFO")).upper(), logging.INFO)
    log_dir = Path(log_cfg.get("log_dir", "logs"))
    if not log_dir.is_absolute():
        log_dir = get_default_config_path().parent / log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    brief_formatter = logging.Formatter("%(levelname)s: %(message)s")

    current_log = log_dir / "quality_cal.log"
    session_log = log_dir / f"quality_cal_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(brief_formatter)

    rotating = logging.handlers.RotatingFileHandler(
        current_log,
        maxBytes=int(log_cfg.get("max_bytes", 10_485_760)),
        backupCount=int(log_cfg.get("backup_count", 5)),
        encoding="utf-8",
    )
    rotating.setLevel(level)
    rotating.setFormatter(formatter)

    session = logging.FileHandler(session_log, encoding="utf-8")
    session.setLevel(level)
    session.setFormatter(formatter)

    root_logger.addHandler(console)
    root_logger.addHandler(rotating)
    root_logger.addHandler(session)

    logger.info("Logging configured")
    logger.info("Log file: %s", current_log)


def _build_range(start: float, stop: float, step: float) -> list[float]:
    if step <= 0:
        return []
    if start > stop:
        return []
    values: list[float] = []
    current = start
    while current <= stop + 1e-9:
        values.append(round(current, 4))
        current += step
    return values


def _normalize_points(points: list[float], *, include_zero: bool = False) -> list[float]:
    cleaned = sorted(
        {round(point, 4) for point in points if include_zero or point > 0.0},
    )
    return _apply_vacuum_floor(cleaned)


def _apply_vacuum_floor(points: list[float]) -> list[float]:
    """Alicat S0 / exhaust breaks the next vacuum step — never schedule true 0 PSIA."""
    return [
        MIN_VACUUM_SETPOINT_PSIA if point < MIN_VACUUM_SETPOINT_PSIA else point
        for point in points
    ]


def _dedupe_preserve_order(points: list[float]) -> list[float]:
    """Keep first occurrence order (e.g. CAL 10 high-to-low sweep)."""
    seen: set[float] = set()
    ordered: list[float] = []
    for point in points:
        key = round(float(point), 4)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    return ordered


def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Expected numeric value, got {value!r}") from exc


def _coerce_optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    return _coerce_float(value)
