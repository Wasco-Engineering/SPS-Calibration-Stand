"""Tests for quality-cal calibration export."""

from __future__ import annotations

from pathlib import Path

import yaml

from app.services.pressure_calibration import ONE_TORR_PSI, apply_error_model, psi_to_torr
from quality_cal.core.calibration_export import (
    build_recommended_config,
    is_severe_point_failure,
    merge_hardware_into_stinger_config,
    point_passes_after_correction,
    point_passes_mensor_tolerance,
    port_calibration_passed,
)
from quality_cal.session import PortFitSummary
from quality_cal.core.port_calibrator import rescore_points_with_models
from quality_cal.config import QualitySettings
from quality_cal.session import CalibrationPointResult, PortCalibrationResult, QualityCalibrationSession


def _point(target: float, mensor: float, alicat: float, transducer: float, index: int = 1) -> CalibrationPointResult:
    deviation = mensor - alicat
    return CalibrationPointResult(
        port_id='port_b',
        point_index=index,
        point_total=21,
        target_psia=target,
        route='pressure',
        mensor_psia=mensor,
        alicat_psia=alicat,
        transducer_psia=transducer,
        deviation_psia=deviation,
        passed=abs(deviation) <= ONE_TORR_PSI,
        settle_duration_s=10.0,
        hold_duration_s=8.0,
        sample_count=32,
    )


def test_build_recommended_config_from_session_points() -> None:
    session = QualityCalibrationSession()
    points = []
    for idx, target in enumerate(range(1, 22)):
        err = 0.01 * target + 0.03
        points.append(
            _point(
                float(target),
                mensor=float(target),
                alicat=float(target) - 0.05,
                transducer=float(target) + err,
                index=idx + 1,
            )
        )
    session.right_port = PortCalibrationResult(port_id='port_b', points=points)

    snippet = build_recommended_config(session, fit_max_psi=20.0)
    assert 'hardware' in snippet
    assert 'transducer_error_model' in snippet['hardware']['labjack']['port_b']
    assert 'alicat_error_model' in snippet['hardware']['alicat']['port_b']

    model = snippet['hardware']['labjack']['port_b']['transducer_error_model']
    corrected = apply_error_model(points[10].transducer_psia, model)
    assert abs(corrected - points[10].mensor_psia) < 0.05


def test_merge_hardware_into_stinger_config(tmp_path: Path) -> None:
    stinger = {
        'hardware': {
            'labjack': {'port_a': {'transducer_ain': 2}, 'port_b': {}},
            'alicat': {'port_b': {'address': 'B'}},
            'measurement': {'transducer_only_below_psi': 10.0},
        }
    }
    snippet = {
        'hardware': {
            'labjack': {
                'pressure_filter_alpha': 0.0,
                'port_b': {'transducer_error_model': {'type': 'quadratic', 'c_error_psi': 0.1}},
            },
            'alicat': {'port_b': {'alicat_error_model': {'type': 'quadratic', 'c_error_psi': 0.2}}},
        }
    }
    merged = merge_hardware_into_stinger_config(stinger, snippet)
    assert merged['hardware']['measurement']['transducer_only_below_psi'] == 20.0
    assert 'transducer_error_model' in merged['hardware']['labjack']['port_b']

    out = tmp_path / 'stinger.yaml'
    out.write_text(yaml.safe_dump(merged), encoding='utf-8')
    loaded = yaml.safe_load(out.read_text(encoding='utf-8'))
    assert loaded['hardware']['labjack']['port_a']['transducer_ain'] == 2


def test_point_passes_mensor_tolerance() -> None:
    ok = _point(10.0, 10.0, 10.0, 10.1)
    assert point_passes_mensor_tolerance(ok)
    bad = _point(10.0, 10.0, 10.1, 10.1)
    assert not point_passes_mensor_tolerance(bad)


def test_point_passes_after_correction_uses_corrected_residual(tmp_path: Path) -> None:
    tol = 0.0193
    raw_fail = _point(15.0, 15.0, 15.05, 15.0)
    assert not raw_fail.passed
    raw_fail.corrected_deviation_psia = 0.0
    assert point_passes_after_correction(
        raw_fail,
        pass_threshold_torr=1.0,
        fit_max_psia=30.0,
        pressure_tolerance_psia=tol,
    )

    borderline = _point(50.0, 50.0, 50.02, 30.0)
    borderline.deviation_psia = -0.02
    borderline.corrected_deviation_psia = -0.02265
    assert not point_passes_after_correction(
        borderline,
        pass_threshold_torr=1.0,
        fit_max_psia=30.0,
        pressure_tolerance_psia=tol,
    )

    high_ok = _point(115.0, 115.0, 115.01, 30.0)
    high_ok.deviation_psia = -0.01
    high_ok.corrected_deviation_psia = 0.06
    assert point_passes_after_correction(
        high_ok,
        pass_threshold_torr=1.0,
        fit_max_psia=30.0,
        pressure_tolerance_psia=tol,
    )

    over_corrected = _point(115.0, 115.0208, 115.0050, 30.0)
    over_corrected.deviation_psia = 0.0158
    over_corrected.corrected_deviation_psia = 0.0415
    assert point_passes_after_correction(
        over_corrected,
        pass_threshold_torr=1.0,
        fit_max_psia=115.0,
        pressure_tolerance_psia=tol,
    )

    bad_mensor = _point(115.0, 13.5, 115.0, 30.0)
    bad_mensor.corrected_deviation_psia = 0.0
    assert is_severe_point_failure(bad_mensor)
    assert not point_passes_after_correction(
        bad_mensor,
        pass_threshold_torr=1.0,
        fit_max_psia=30.0,
        pressure_tolerance_psia=tol,
    )


def test_rescore_updates_pass_flags(tmp_path: Path) -> None:
    settings = QualitySettings(
        profile_id='cal10',
        profile_label='CAL10',
        pressure_points_psia=[15.0],
        pressure_tolerance_psia=0.0193,
        settle_tolerance_psia=0.05,
        settle_hold_s=5.0,
        settle_timeout_s=60.0,
        static_hold_s=5.0,
        settle_hold_at_or_below_5_psia_s=2.0,
        settle_hold_above_5_psia_s=1.5,
        settle_hold_above_30_psia_s=1.5,
        static_hold_at_or_below_5_psia_s=5.0,
        static_hold_above_5_psia_s=3.0,
        static_hold_above_30_psia_s=3.0,
        static_discard_s=1.0,
        sample_hz=4.0,
        mensor_max_psia=165.0,
        fit_max_psia=30.0,
        alicat_fit_max_psia=115.0,
        require_mensor=True,
        prompt_disconnect_mensor_above_psi=None,
        capture_raw_during_sweep=True,
        pass_threshold_torr=1.0,
        leak_check_target_psia=100.0,
        leak_check_duration_s=90.0,
        leak_check_sample_hz=4.0,
        leak_check_max_rate_psi_per_min=0.2,
        leak_check_ramp_rate_psi_per_s=8.0,
        report_output_dir=tmp_path,
        report_template_path=tmp_path / 't.docx',
        report_filename_prefix='QualityCalibration',
        desktop_output_dir=tmp_path,
        also_write_records_path=False,
    )
    point = _point(15.0, 15.0, 15.05, 15.0)
    assert not point.passed
    model = {'type': 'quadratic', 'a': 0.0, 'b': 0.0, 'c_error_psi': 0.05}
    rescored = rescore_points_with_models(
        [point],
        alicat_model=model,
        settings=settings,
    )
    assert rescored[0].passed
    assert abs(psi_to_torr(rescored[0].corrected_deviation_psia or 0.0)) < 1.0

    summary = PortFitSummary(
        port_id='port_b',
        transducer_passed=True,
        alicat_passed=True,
        transducer_error_model={'type': 'quadratic'},
        alicat_error_model={'type': 'quadratic'},
    )
    assert port_calibration_passed(rescored, summary)
