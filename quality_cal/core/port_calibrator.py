"""Fit and apply per-port calibration models from quality-cal sweep data."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from app.core.config import is_transducer_installed, load_config
from app.services.pressure_calibration import (
    REFERENCE_MENSOR,
    SENSOR_ALICAT,
    SENSOR_TRANSDUCER,
    apply_error_model,
)
from quality_cal.config import QualitySettings, get_default_config_path
from quality_cal.core.calibration_export import (
    merge_hardware_into_stinger_config,
    point_passes_after_correction,
)
from quality_cal.session import CalibrationPointResult, PortFitSummary

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SensorFitResult:
    sensor: str
    p99_abs_torr: float
    mean_abs_torr: float
    max_abs_torr: float
    passed: bool
    model: Dict[str, Any]
    ema_alpha: float = 0.0


@dataclass(slots=True)
class PortCalibrationFitResult:
    port_id: str
    sweep_csv_path: Path
    transducer: Optional[SensorFitResult]
    alicat: Optional[SensorFitResult]
    error_message: Optional[str] = None

    @property
    def overall_passed(self) -> bool:
        if self.error_message:
            return False
        checks = []
        if self.transducer is not None:
            checks.append(self.transducer.passed)
        if self.alicat is not None:
            checks.append(self.alicat.passed)
        return bool(checks) and all(checks)


def get_stinger_config_path() -> Path:
    from app.core.paths import get_stinger_config_path as _stinger_path

    return _stinger_path()


def _filter_mensor_agreement_samples(
    samples: list,
    *,
    max_gap_psi: float = 0.05,
) -> list:
    """Drop static samples where transducer and Mensor disagree sharply (bad settle/leak)."""
    kept: list = []
    for sample in samples:
        mensor = sample.mensor_abs_psia
        transducer = sample.transducer_abs_psi
        if mensor is None or transducer is None:
            continue
        if abs(float(mensor) - float(transducer)) <= max_gap_psi:
            kept.append(sample)
    return kept


def _fit_sensor_from_samples(
    *,
    port_id: str,
    sensor: str,
    samples: list,
    settings: QualitySettings,
    min_samples: int,
) -> Optional[SensorFitResult]:
    """Try several optimizer strategies until pass or best achievable p99."""
    from scripts.optimize_pressure_calibration import (  # noqa: WPS433
        _optimize_for_port_sensor,
    )

    agreement_gaps_psi = (0.05, 0.08, 0.1, 0.2, 1.5)
    if sensor == SENSOR_TRANSDUCER:
        for gap in agreement_gaps_psi:
            filtered = _filter_mensor_agreement_samples(samples, max_gap_psi=gap)
            if len(filtered) >= min_samples // 2:
                logger.info(
                    '%s/%s: using %s/%s samples after Mensor agreement filter (|T-M| <= %.3f psi)',
                    port_id,
                    sensor,
                    len(filtered),
                    len(samples),
                    gap,
                )
                samples = filtered
                break

    strategies = [
        {
            'tolerance_psi': max(0.2, settings.settle_tolerance_psia),
            'holdout_stride': 3,
            'pressure_axis': 'measured',
            'robust_refit_torr': 0.5,
            'segment_counts': (5, 3),
            'alpha_grid': [0.0, 0.15, 0.2, 0.25, 0.3],
        },
        {
            'tolerance_psi': max(0.2, settings.settle_tolerance_psia),
            'holdout_stride': 5,
            'pressure_axis': 'measured',
            'robust_refit_torr': 0.5,
            'segment_counts': (5, 3),
            'alpha_grid': [0.0, 0.15, 0.2, 0.25, 0.3],
        },
        {
            'tolerance_psi': max(0.16, settings.settle_tolerance_psia * 2),
            'holdout_stride': 5,
            'pressure_axis': 'measured',
            'robust_refit_torr': 1.0,
            'segment_counts': (5, 3),
            'alpha_grid': [0.0, 0.2, 0.3],
        },
        {
            'tolerance_psi': max(0.2, settings.settle_tolerance_psia * 2),
            'holdout_stride': 5,
            'pressure_axis': 'target',
            'robust_refit_torr': None,
            'segment_counts': (3, 5),
            'alpha_grid': [0.0],
        },
    ]

    best: Optional[SensorFitResult] = None
    for index, strategy in enumerate(strategies, start=1):
        try:
            opt = _optimize_for_port_sensor(
                port_id=port_id,
                sensor=sensor,  # type: ignore[arg-type]
                reference=REFERENCE_MENSOR,
                samples=samples,
                tolerance_psi=strategy['tolerance_psi'],
                static_only=True,
                holdout_stride=strategy['holdout_stride'],
                alpha_grid=strategy.get('alpha_grid', [0.0]),
                pass_threshold_torr=settings.pass_threshold_torr,
                min_near_target=min_samples,
                pressure_axis=strategy['pressure_axis'],
                robust_refit_torr=strategy['robust_refit_torr'],
                segment_counts=strategy['segment_counts'],
            )
            candidate = opt['best']
            fit = SensorFitResult(
                sensor=sensor,
                p99_abs_torr=float(candidate.p99_abs_torr),
                mean_abs_torr=float(candidate.mean_abs_torr),
                max_abs_torr=float(candidate.max_abs_torr),
                passed=bool(candidate.passed),
                model=dict(candidate.model),
                ema_alpha=float(candidate.ema_alpha),
            )
            logger.info(
                '%s/%s strategy %s: p99=%.3f Torr (%s) axis=%s',
                port_id,
                sensor,
                index,
                fit.p99_abs_torr,
                'PASS' if fit.passed else 'FAIL',
                strategy['pressure_axis'],
            )
            if best is None or fit.p99_abs_torr < best.p99_abs_torr:
                best = fit
            if fit.passed:
                return fit
        except ValueError as exc:
            logger.warning(
                '%s/%s strategy %s skipped: %s',
                port_id,
                sensor,
                index,
                exc,
            )
        except Exception as exc:
            logger.warning(
                '%s/%s strategy %s failed: %s',
                port_id,
                sensor,
                index,
                exc,
            )
    return best


def fit_port_from_sweep_csv(
    csv_path: Path | str,
    port_id: str,
    settings: QualitySettings,
) -> PortCalibrationFitResult:
    """Fit transducer and Alicat error models vs Mensor from dense sweep CSV."""
    from scripts.optimize_pressure_calibration import _load_samples  # noqa: WPS433

    path = Path(csv_path)
    if not path.exists():
        return PortCalibrationFitResult(
            port_id=port_id,
            sweep_csv_path=path,
            transducer=None,
            alicat=None,
            error_message=f'Sweep CSV not found: {path}',
        )

    try:
        from app.services.pressure_calibration import filter_samples_pressure_band

        all_samples = _load_samples([path], port_id)
        transducer_samples = filter_samples_pressure_band(
            all_samples,
            min_psi=0.0,
            max_psi=settings.fit_max_psia,
            reference=REFERENCE_MENSOR,
        )
        alicat_samples = filter_samples_pressure_band(
            all_samples,
            min_psi=0.0,
            max_psi=settings.alicat_fit_max_psia,
            reference=REFERENCE_MENSOR,
        )
        if not any(s.mensor_abs_psia is not None for s in all_samples):
            return PortCalibrationFitResult(
                port_id=port_id,
                sweep_csv_path=path,
                transducer=None,
                alicat=None,
                error_message='Sweep CSV has no Mensor data; cannot fit vs Mensor reference.',
            )

        min_samples = max(30, len(settings.pressure_points_psia) * 2)
        transducer_fit = None
        if is_transducer_installed(load_config(), port_id):
            transducer_fit = _fit_sensor_from_samples(
                port_id=port_id,
                sensor=SENSOR_TRANSDUCER,
                samples=transducer_samples,
                settings=settings,
                min_samples=min_samples,
            )
        alicat_fit = _fit_sensor_from_samples(
            port_id=port_id,
            sensor=SENSOR_ALICAT,
            samples=alicat_samples,
            settings=settings,
            min_samples=min_samples,
        )

        return PortCalibrationFitResult(
            port_id=port_id,
            sweep_csv_path=path,
            transducer=transducer_fit,
            alicat=alicat_fit,
        )
    except Exception as exc:
        logger.exception('Port calibration fit failed')
        return PortCalibrationFitResult(
            port_id=port_id,
            sweep_csv_path=path,
            transducer=None,
            alicat=None,
            error_message=str(exc),
        )


def build_port_config_snippet(
    port_id: str,
    fit: PortCalibrationFitResult,
    *,
    require_passed: bool = True,
) -> Dict[str, Any]:
    labjack_port: Dict[str, Any] = {}
    alicat_port: Dict[str, Any] = {}
    filter_alpha = 0.0
    if fit.transducer is not None and (not require_passed or fit.transducer.passed):
        labjack_port['transducer_error_model'] = fit.transducer.model
        filter_alpha = float(fit.transducer.ema_alpha)
    elif fit.transducer is not None and not fit.transducer.passed:
        logger.warning(
            'Omitting transducer_error_model for %s (p99 %.3f Torr did not pass)',
            port_id,
            fit.transducer.p99_abs_torr,
        )
    if fit.alicat is not None and (not require_passed or fit.alicat.passed):
        alicat_port['alicat_error_model'] = fit.alicat.model
    elif fit.alicat is not None and not fit.alicat.passed:
        logger.warning(
            'Omitting alicat_error_model for %s (p99 %.3f Torr did not pass)',
            port_id,
            fit.alicat.p99_abs_torr,
        )
    hardware: Dict[str, Any] = {
        'labjack': {
            'pressure_filter_alpha': filter_alpha,
            port_id: labjack_port,
        },
    }
    if alicat_port:
        hardware['alicat'] = {port_id: alicat_port}
    return {'hardware': hardware}


def apply_port_models_to_stinger_config(
    port_id: str,
    fit: PortCalibrationFitResult,
    stinger_path: Optional[Path] = None,
    *,
    require_passed: bool = True,
) -> Path:
    """Merge fitted models for one port into stinger_config.yaml."""
    path = stinger_path or get_stinger_config_path()
    snippet = build_port_config_snippet(port_id, fit, require_passed=require_passed)
    if path.exists():
        stinger = yaml.safe_load(path.read_text(encoding='utf-8'))
        if not isinstance(stinger, dict):
            raise ValueError(f'Invalid stinger config: {path}')
        merged = merge_hardware_into_stinger_config(stinger, snippet)
    else:
        merged = snippet
    path.write_text(yaml.safe_dump(merged, sort_keys=False), encoding='utf-8')
    logger.info('Wrote calibration models for %s to %s', port_id, path)
    return path


def reload_port_calibration(port: Port, snippet: Dict[str, Any]) -> None:
    """Apply error models on connected hardware without reconnecting."""
    hw = snippet.get('hardware', {})
    lj = hw.get('labjack', {})
    ali = hw.get('alicat', {})
    port_key = port.port_id.value

    port_cfg = lj.get(port_key, {})
    if hasattr(port.daq, 'apply_error_model_config'):
        port.daq.apply_error_model_config(port_cfg.get('transducer_error_model'))
    else:
        port.daq._error_model = port_cfg.get('transducer_error_model')

    ali_cfg = ali.get(port_key, {})
    if hasattr(port.alicat, 'apply_error_model_config'):
        port.alicat.apply_error_model_config(ali_cfg.get('alicat_error_model'))
    else:
        port.alicat._error_model = ali_cfg.get('alicat_error_model')


@dataclass(slots=True)
class FinalizeCalibrationResult:
    """Raw sweep complete → fit models → rescore → optional apply."""

    points: list[CalibrationPointResult]
    fit: PortCalibrationFitResult
    fit_summary: PortFitSummary
    message: str


def finalize_port_calibration(
    sweep_csv_path: Path | str,
    port_id: str,
    raw_points: list[CalibrationPointResult],
    settings: QualitySettings,
    *,
    port: Optional[Port] = None,
    apply_to_stinger: bool = True,
) -> FinalizeCalibrationResult:
    """Fit correlation models from the sweep, rescore points, optionally apply to config."""
    path = Path(sweep_csv_path)
    fit = fit_port_from_sweep_csv(path, port_id, settings)
    if fit.error_message and fit.transducer is None and fit.alicat is None:
        return FinalizeCalibrationResult(
            points=raw_points,
            fit=fit,
            fit_summary=fit_summary_from_result(port_id, fit, applied=False),
            message=f'Fit failed: {fit.error_message}',
        )

    rescored = rescore_points_with_models(raw_points, fit, settings=settings)
    applied = False
    message_lines = [f'{settings.profile_label} — {port_id}']

    if fit.transducer is not None:
        message_lines.append(
            f'Transducer fit p99: {fit.transducer.p99_abs_torr:.3f} Torr '
            f'({"PASS" if fit.transducer.passed else "FAIL"})',
        )
    if fit.alicat is not None:
        message_lines.append(
            f'Alicat fit p99: {fit.alicat.p99_abs_torr:.3f} Torr '
            f'({"PASS" if fit.alicat.passed else "FAIL"})',
        )

    if apply_to_stinger and (fit.transducer is not None or fit.alicat is not None):
        snippet = build_port_config_snippet(port_id, fit, require_passed=True)
        has_models = bool(
            snippet.get('hardware', {}).get('labjack', {}).get(port_id)
            or snippet.get('hardware', {}).get('alicat', {}).get(port_id)
        )
        if has_models:
            apply_port_models_to_stinger_config(port_id, fit, require_passed=True)
            if port is not None:
                reload_port_calibration(port, snippet)
            applied = True
            message_lines.append('Applied passing error models to stinger_config.yaml.')
        else:
            message_lines.append('No passing models met apply criteria; config unchanged.')

    passed = sum(1 for p in rescored if p.passed)
    message_lines.append(
        f'Final result after correlation: {passed}/{len(rescored)} points pass '
        f'(tolerance +/- {settings.pressure_tolerance_psia:.4f} psia).',
    )

    return FinalizeCalibrationResult(
        points=rescored,
        fit=fit,
        fit_summary=fit_summary_from_result(port_id, fit, applied=applied),
        message='\n'.join(message_lines),
    )


def fit_summary_from_result(
    port_id: str,
    fit: PortCalibrationFitResult,
    *,
    applied: bool,
) -> PortFitSummary:
    return PortFitSummary(
        port_id=port_id,
        sweep_csv_path=fit.sweep_csv_path,
        transducer_p99_abs_torr=(
            fit.transducer.p99_abs_torr if fit.transducer is not None else None
        ),
        alicat_p99_abs_torr=fit.alicat.p99_abs_torr if fit.alicat is not None else None,
        transducer_passed=bool(fit.transducer.passed) if fit.transducer else False,
        alicat_passed=bool(fit.alicat.passed) if fit.alicat else False,
        applied_to_stinger_config=applied,
        transducer_error_model=(
            fit.transducer.model if fit.transducer is not None else None
        ),
        alicat_error_model=fit.alicat.model if fit.alicat is not None else None,
    )


def rescore_points_with_models(
    points: list[CalibrationPointResult],
    fit: PortCalibrationFitResult | None = None,
    *,
    alicat_model: Optional[Dict[str, Any]] = None,
    transducer_model: Optional[Dict[str, Any]] = None,
    settings: Optional[QualitySettings] = None,
) -> list[CalibrationPointResult]:
    """Recompute corrected deviations vs Mensor; optionally update pass after fit."""
    if fit is not None:
        if alicat_model is None and fit.alicat is not None:
            alicat_model = fit.alicat.model
        if transducer_model is None and fit.transducer is not None:
            transducer_model = fit.transducer.model
    if alicat_model is None and transducer_model is None:
        return points

    updated: list[CalibrationPointResult] = []
    for point in points:
        corrected_alicat_dev = point.corrected_deviation_psia
        corrected_transducer_dev = point.corrected_transducer_deviation_psia
        in_fit_band = (
            settings is None
            or point.target_psia <= settings.fit_max_psia + 1e-6
        )
        if point.mensor_used and point.mensor_psia is not None:
            if alicat_model is not None and point.alicat_psia is not None:
                corrected_alicat = apply_error_model(point.alicat_psia, alicat_model)
                corrected_alicat_dev = point.mensor_psia - corrected_alicat
            if (
                in_fit_band
                and transducer_model is not None
                and point.transducer_psia is not None
            ):
                corrected_transducer = apply_error_model(
                    point.transducer_psia,
                    transducer_model,
                )
                corrected_transducer_dev = point.mensor_psia - corrected_transducer
        passed = point.passed
        if settings is not None and (alicat_model is not None or transducer_model is not None):
            passed = point_passes_after_correction(
                CalibrationPointResult(
                    port_id=point.port_id,
                    point_index=point.point_index,
                    point_total=point.point_total,
                    target_psia=point.target_psia,
                    route=point.route,
                    mensor_psia=point.mensor_psia,
                    alicat_psia=point.alicat_psia,
                    transducer_psia=point.transducer_psia,
                    deviation_psia=point.deviation_psia,
                    passed=point.passed,
                    settle_duration_s=point.settle_duration_s,
                    hold_duration_s=point.hold_duration_s,
                    sample_count=point.sample_count,
                    transducer_deviation_psia=point.transducer_deviation_psia,
                    corrected_deviation_psia=corrected_alicat_dev,
                    corrected_transducer_deviation_psia=corrected_transducer_dev,
                    mensor_used=point.mensor_used,
                    measured_at=point.measured_at,
                ),
                pass_threshold_torr=settings.pass_threshold_torr,
                fit_max_psia=settings.fit_max_psia,
                pressure_tolerance_psia=settings.pressure_tolerance_psia,
            )
        updated.append(
            CalibrationPointResult(
                port_id=point.port_id,
                point_index=point.point_index,
                point_total=point.point_total,
                target_psia=point.target_psia,
                route=point.route,
                mensor_psia=point.mensor_psia,
                alicat_psia=point.alicat_psia,
                transducer_psia=point.transducer_psia,
                deviation_psia=point.deviation_psia,
                passed=passed,
                settle_duration_s=point.settle_duration_s,
                hold_duration_s=point.hold_duration_s,
                sample_count=point.sample_count,
                transducer_deviation_psia=point.transducer_deviation_psia,
                corrected_deviation_psia=corrected_alicat_dev,
                corrected_transducer_deviation_psia=corrected_transducer_dev,
                mensor_used=point.mensor_used,
                measured_at=point.measured_at,
            )
        )
    return updated


def format_config_apply_preview(port_id: str, fit: PortCalibrationFitResult) -> str:
    """YAML snippet that would be merged into stinger_config.yaml on Apply."""
    snippet = build_port_config_snippet(port_id, fit)
    text = yaml.safe_dump(snippet, sort_keys=False, default_flow_style=False)
    return f'Config changes for {port_id}:\n\n{text}'


def format_fit_dialog_text(fit: PortCalibrationFitResult, settings: QualitySettings) -> str:
    if fit.error_message:
        return f'Fit failed: {fit.error_message}'
    lines = [
        f'{settings.profile_label} — {fit.port_id}',
        f'Sweep: {fit.sweep_csv_path.name}',
        '',
    ]
    if fit.transducer is not None:
        lines.append(
            f'Transducer: {fit.transducer.p99_abs_torr:.3f} Torr p99 '
            f'({"PASS" if fit.transducer.passed else "FAIL"})',
        )
    if fit.alicat is not None:
        lines.append(
            f'Alicat: {fit.alicat.p99_abs_torr:.3f} Torr p99 '
            f'({"PASS" if fit.alicat.passed else "FAIL"})',
        )
    lines.append('')
    lines.append('Apply correction models to stinger_config.yaml on this machine?')
    return '\n'.join(lines)
