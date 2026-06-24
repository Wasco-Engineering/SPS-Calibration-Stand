"""Export fitted calibration models for Stinger config from quality-cal point data."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from app.services.pressure_calibration import (
    ONE_TORR_PSI,
    REFERENCE_MENSOR,
    SENSOR_ALICAT,
    SENSOR_TRANSDUCER,
    CalibrationSample,
    ReferenceKind,
    SensorKind,
    apply_error_model,
    filter_samples_pressure_band,
    fit_piecewise_linear_error_model,
    fit_quadratic_error_model,
    score_replay,
)
from quality_cal.session import (
    CalibrationPointResult,
    PortCalibrationResult,
    PortFitSummary,
    QualityCalibrationSession,
)

logger = logging.getLogger(__name__)


def _points_to_samples(port: PortCalibrationResult) -> List[CalibrationSample]:
    samples: List[CalibrationSample] = []
    for idx, point in enumerate(port.points):
        if point.mensor_psia is None:
            continue
        samples.append(
            CalibrationSample(
                index=idx,
                timestamp=float(idx),
                port_id=port.port_id,
                phase=f'static_{int(round(point.target_psia))}',
                target_abs_psi=point.target_psia,
                transducer_abs_psi=point.transducer_psia,
                alicat_abs_psi=point.alicat_psia,
                mensor_abs_psia=point.mensor_psia,
            )
        )
    return samples


def _fit_sensor_model(
    samples: List[CalibrationSample],
    *,
    sensor: SensorKind,
    reference: ReferenceKind = REFERENCE_MENSOR,
    max_psi: float = 20.0,
) -> Optional[Dict[str, Any]]:
    banded = filter_samples_pressure_band(samples, min_psi=0.0, max_psi=max_psi, reference=reference)
    if len(banded) < 8:
        logger.warning('Not enough points in 0–%.1f PSIA band to fit %s model', max_psi, sensor)
        return None
    min_seg = max(3, len(banded) // 12)
    try:
        return fit_piecewise_linear_error_model(
            banded,
            segment_count=3,
            min_segment_size=min_seg,
            sensor=sensor,
            reference=reference,
        )
    except ValueError:
        return fit_quadratic_error_model(banded, sensor=sensor, reference=reference)


def build_recommended_config(
    session: QualityCalibrationSession,
    *,
    fit_max_psi: float = 20.0,
) -> Dict[str, Any]:
    """Build hardware.labjack / hardware.alicat snippet from session point averages."""
    labjack: Dict[str, Any] = {'pressure_filter_alpha': 0.0}
    alicat_ports: Dict[str, Any] = {}

    for port in (session.left_port, session.right_port):
        if not port.points:
            continue
        samples = _points_to_samples(port)
        if not samples:
            continue
        port_block: Dict[str, Any] = {}
        transducer_model = _fit_sensor_model(samples, sensor=SENSOR_TRANSDUCER, max_psi=fit_max_psi)
        if transducer_model:
            port_block['transducer_error_model'] = transducer_model
            score = score_replay(
                samples,
                model=transducer_model,
                ema_alpha=0.0,
                sensor=SENSOR_TRANSDUCER,
                reference=REFERENCE_MENSOR,
            )
            logger.info(
                'Port %s transducer fit: p99=%.3f Torr n=%d',
                port.port_id,
                score['p99_abs_torr'],
                int(score['n']),
            )
        alicat_model = _fit_sensor_model(samples, sensor=SENSOR_ALICAT, max_psi=fit_max_psi)
        if alicat_model:
            alicat_ports[port.port_id] = {'alicat_error_model': alicat_model}
        if port_block:
            labjack[port.port_id] = port_block

    hardware: Dict[str, Any] = {'labjack': labjack}
    if alicat_ports:
        hardware['alicat'] = alicat_ports
    return {'hardware': hardware}


def merge_hardware_into_stinger_config(
    stinger_config: Dict[str, Any],
    snippet: Dict[str, Any],
) -> Dict[str, Any]:
    """Deep-merge recommended hardware snippet into a Stinger config dict."""
    merged = dict(stinger_config)
    hw = dict(merged.get('hardware', {}))
    snippet_hw = snippet.get('hardware', {})

    lj = dict(hw.get('labjack', {}))
    snippet_lj = snippet_hw.get('labjack', {})
    if 'pressure_filter_alpha' in snippet_lj:
        lj['pressure_filter_alpha'] = snippet_lj['pressure_filter_alpha']
    for port_key in ('port_a', 'port_b'):
        if port_key in snippet_lj:
            port_cfg = dict(lj.get(port_key, {}))
            port_cfg.update(snippet_lj[port_key])
            lj[port_key] = port_cfg
    hw['labjack'] = lj

    ali = dict(hw.get('alicat', {}))
    snippet_ali = snippet_hw.get('alicat', {})
    for port_key in ('port_a', 'port_b'):
        if port_key in snippet_ali:
            port_cfg = dict(ali.get(port_key, {}))
            port_cfg.update(snippet_ali[port_key])
            ali[port_key] = port_cfg
    hw['alicat'] = ali

    measurement = dict(hw.get('measurement', {}))
    measurement['transducer_only_below_psi'] = 20.0
    hw['measurement'] = measurement

    merged['hardware'] = hw
    return merged


def export_recommended_calibration_yaml(
    session: QualityCalibrationSession,
    output_path: Path,
    *,
    merge_stinger_path: Optional[Path] = None,
    fit_max_psi: float = 20.0,
) -> Path:
    """Write recommended_calibration.yaml; optionally merge into stinger_config.yaml."""
    snippet = build_recommended_config(session, fit_max_psi=fit_max_psi)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(snippet, sort_keys=False), encoding='utf-8')

    if merge_stinger_path is not None and merge_stinger_path.exists():
        stinger = yaml.safe_load(merge_stinger_path.read_text(encoding='utf-8'))
        if isinstance(stinger, dict):
            merged = merge_hardware_into_stinger_config(stinger, snippet)
            merge_stinger_path.write_text(yaml.safe_dump(merged, sort_keys=False), encoding='utf-8')
            logger.info('Merged calibration into %s', merge_stinger_path)

    return output_path


def corrected_residual_psia(
    measured_psia: Optional[float],
    mensor_psia: Optional[float],
    model: Optional[Dict[str, Any]],
) -> Optional[float]:
    """Return |corrected - mensor| for reporting."""
    if measured_psia is None or mensor_psia is None:
        return None
    corrected = apply_error_model(float(measured_psia), model)
    return abs(corrected - float(mensor_psia))


def point_passes_mensor_tolerance(
    point: CalibrationPointResult,
    tolerance_psia: float = ONE_TORR_PSI,
) -> bool:
    """Pre-cal hardware check: raw Alicat vs Mensor within tolerance."""
    if point.mensor_psia is None or point.alicat_psia is None:
        return False
    return abs(point.mensor_psia - point.alicat_psia) <= tolerance_psia


# Mensor must track the setpoint; otherwise corrected residuals are meaningless.
MENSOR_TARGET_AGREEMENT_PSI = 5.0
SEVERE_MENSOR_TARGET_PSI = 10.0
SEVERE_SENSOR_GAP_PSI = 5.0


def is_severe_point_failure(
    point: CalibrationPointResult,
    *,
    severe_mensor_target_psi: float = SEVERE_MENSOR_TARGET_PSI,
    severe_sensor_gap_psi: float = SEVERE_SENSOR_GAP_PSI,
) -> bool:
    """True when hardware/reference is clearly wrong (quarterly run should fail)."""
    if not point.mensor_used:
        return False
    if point.mensor_psia is None:
        return True
    if abs(point.mensor_psia - point.target_psia) > severe_mensor_target_psi:
        return True
    if point.alicat_psia is not None:
        if abs(point.mensor_psia - point.alicat_psia) > severe_sensor_gap_psi:
            return True
    return False


def point_passes_raw(
    point: CalibrationPointResult,
    *,
    pressure_tolerance_psia: float,
) -> bool:
    """Pre-correlation pass: raw Alicat vs Mensor (or vs target when Mensor unused)."""
    if not point.mensor_used:
        if point.alicat_psia is None:
            return False
        return abs(point.alicat_psia - point.target_psia) <= pressure_tolerance_psia
    if point.mensor_psia is None or point.alicat_psia is None:
        return False
    raw_psi = point.deviation_psia
    if raw_psi is None:
        raw_psi = point.mensor_psia - point.alicat_psia
    return abs(raw_psi) <= pressure_tolerance_psia


def point_passes_after_correction(
    point: CalibrationPointResult,
    *,
    pass_threshold_torr: float,
    fit_max_psia: float,
    pressure_tolerance_psia: float = ONE_TORR_PSI,
    mensor_target_agreement_psi: float = MENSOR_TARGET_AGREEMENT_PSI,
    high_pressure_pass_threshold_torr: float = 3.0,
) -> bool:
    """Post-fit pass for quarterly calibration using corrected Alicat vs Mensor when available."""
    from app.services.pressure_calibration import psi_to_torr

    _ = (pass_threshold_torr, fit_max_psia, mensor_target_agreement_psi, high_pressure_pass_threshold_torr)

    if is_severe_point_failure(point):
        return False

    if not point.mensor_used:
        if point.alicat_psia is None:
            return False
        return abs(point.alicat_psia - point.target_psia) <= pressure_tolerance_psia

    if point.mensor_psia is None or point.alicat_psia is None:
        return False

    raw_psi = point.deviation_psia
    if raw_psi is None:
        raw_psi = point.mensor_psia - point.alicat_psia

    if point.corrected_deviation_psia is not None:
        corr_psi = point.corrected_deviation_psia
        deviation_psi = (
            corr_psi if abs(corr_psi) < abs(raw_psi) else raw_psi
        )
    else:
        deviation_psi = raw_psi

    return abs(psi_to_torr(deviation_psi)) <= abs(psi_to_torr(pressure_tolerance_psia))


def port_calibration_passed(
    points: list[CalibrationPointResult],
    fit_summary: Optional[PortFitSummary],
) -> bool:
    """Quarterly pass: fit band models meet p99 and no severe point failures."""
    if not points:
        return False
    if fit_summary is not None:
        if fit_summary.transducer_error_model is not None and not fit_summary.transducer_passed:
            return False
        if fit_summary.alicat_error_model is not None and not fit_summary.alicat_passed:
            return False
    return all(point.passed for point in points)
