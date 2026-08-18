"""Tests for Quality Cal versioned offset history."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from quality_cal.config import QualitySettings
from quality_cal.core.offset_history_store import (
    extract_port_models_from_stinger,
    latest_offset_history_path,
    list_offset_history,
    offset_history_index_path,
    record_offset_history,
)
from quality_cal.core.port_calibrator import (
    PortCalibrationFitResult,
    SensorFitResult,
    finalize_port_calibration,
)
from quality_cal.session import CalibrationPointResult


def _settings(**overrides: Any) -> QualitySettings:
    base = dict(
        profile_id='cal10_wcs02075',
        profile_label='CAL 10',
        pressure_points_psia=[1.0, 5.0],
        pressure_tolerance_psia=0.02,
        settle_tolerance_psia=0.05,
        settle_hold_s=1.0,
        settle_timeout_s=30.0,
        static_hold_s=1.0,
        settle_hold_at_or_below_5_psia_s=1.0,
        settle_hold_above_5_psia_s=1.0,
        settle_hold_above_30_psia_s=1.0,
        static_hold_at_or_below_5_psia_s=1.0,
        static_hold_above_5_psia_s=1.0,
        static_hold_above_30_psia_s=1.0,
        static_discard_s=0.0,
        sample_hz=10.0,
        mensor_max_psia=30.0,
        fit_max_psia=30.0,
        alicat_fit_max_psia=30.0,
        require_mensor=True,
        prompt_disconnect_mensor_above_psi=None,
        capture_raw_during_sweep=True,
        pass_threshold_torr=1.0,
        leak_check_target_psia=5.0,
        leak_check_duration_s=10.0,
        leak_check_sample_hz=2.0,
        leak_check_max_rate_psi_per_min=None,
        leak_check_ramp_rate_psi_per_s=1.0,
        report_output_dir=Path('.'),
        report_template_path=Path('.'),
        report_filename_prefix='test',
        desktop_output_dir=Path('.'),
        also_write_records_path=False,
    )
    base.update(overrides)
    return QualitySettings(**base)


def _model(tag: str) -> dict[str, Any]:
    return {
        'type': 'piecewise_linear',
        'segments': [
            {
                'max_psi': None,
                'slope_error_per_psi': 0.0,
                'intercept_error_psi': 0.01 if tag == 'new' else 0.02,
            }
        ],
        'tag': tag,
    }


def _fit(
    *,
    port_id: str = 'port_a',
    sweep: Path | None = None,
    transducer_passed: bool = True,
    alicat_passed: bool = True,
    error_message: str | None = None,
) -> PortCalibrationFitResult:
    return PortCalibrationFitResult(
        port_id=port_id,
        sweep_csv_path=sweep or Path('sweep.csv'),
        transducer=SensorFitResult(
            sensor='transducer',
            p99_abs_torr=0.2,
            mean_abs_torr=0.1,
            max_abs_torr=0.3,
            passed=transducer_passed,
            model=_model('new'),
            ema_alpha=0.0,
        ),
        alicat=SensorFitResult(
            sensor='alicat',
            p99_abs_torr=0.15,
            mean_abs_torr=0.08,
            max_abs_torr=0.25,
            passed=alicat_passed,
            model=_model('new_alicat'),
            ema_alpha=0.0,
        ),
        error_message=error_message,
    )


def test_extract_port_models_from_stinger() -> None:
    config = {
        'hardware': {
            'labjack': {
                'port_a': {'transducer_error_model': _model('prev_t')},
            },
            'alicat': {
                'port_a': {'alicat_error_model': _model('prev_a')},
            },
        }
    }
    models = extract_port_models_from_stinger(config, 'port_a')
    assert models['transducer_error_model']['tag'] == 'prev_t'
    assert models['alicat_error_model']['tag'] == 'prev_a'
    empty = extract_port_models_from_stinger({}, 'port_b')
    assert empty['transducer_error_model'] is None
    assert empty['alicat_error_model'] is None


def test_record_offset_history_creates_entry_index_and_latest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = tmp_path / 'logs'
    logs.mkdir()
    monkeypatch.setattr(
        'quality_cal.core.offset_history_store.get_quality_cal_logs_dir',
        lambda: logs,
    )

    fit = _fit(sweep=tmp_path / 'quality_cal_sweep_port_a.csv')
    previous = {
        'transducer_error_model': _model('prev_t'),
        'alicat_error_model': _model('prev_a'),
    }
    entry = record_offset_history(
        port_id='port_a',
        fit=fit,
        previous_models=previous,
        applied=True,
        profile_id='cal10_wcs02075',
        profile_label='CAL 10',
        stinger_config_path=tmp_path / 'stinger_config.yaml',
        sweep_csv_path=fit.sweep_csv_path,
    )

    assert entry.exists()
    payload = json.loads(entry.read_text(encoding='utf-8'))
    assert payload['port_id'] == 'port_a'
    assert payload['applied'] is True
    assert payload['previous_models']['transducer_error_model']['tag'] == 'prev_t'
    assert payload['new_models']['alicat_error_model']['tag'] == 'new_alicat'
    assert payload['transducer_fit']['p99_abs_torr'] == pytest.approx(0.2)

    index = offset_history_index_path()
    assert index.exists()
    lines = [ln for ln in index.read_text(encoding='utf-8').splitlines() if ln.strip()]
    assert len(lines) == 1
    index_row = json.loads(lines[0])
    assert index_row['port_id'] == 'port_a'
    assert index_row['applied'] is True
    assert index_row['entry_path'] == str(entry)

    latest = latest_offset_history_path('port_a')
    assert latest.exists()
    latest_payload = json.loads(latest.read_text(encoding='utf-8'))
    assert latest_payload['recorded_at'] == payload['recorded_at']


def test_list_offset_history_newest_first_and_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = tmp_path / 'logs'
    logs.mkdir()
    monkeypatch.setattr(
        'quality_cal.core.offset_history_store.get_quality_cal_logs_dir',
        lambda: logs,
    )

    fit_a = _fit(port_id='port_a')
    fit_b = _fit(port_id='port_b')
    record_offset_history(port_id='port_a', fit=fit_a, applied=False)
    record_offset_history(port_id='port_b', fit=fit_b, applied=True)
    record_offset_history(port_id='port_a', fit=fit_a, applied=True)

    all_rows = list_offset_history()
    assert len(all_rows) == 3
    assert all_rows[0]['applied'] is True
    assert all_rows[0]['port_id'] == 'port_a'

    port_a_rows = list_offset_history('port_a')
    assert len(port_a_rows) == 2
    assert all(row['port_id'] == 'port_a' for row in port_a_rows)

    limited = list_offset_history(limit=1)
    assert len(limited) == 1
    assert limited[0]['port_id'] == 'port_a'


def test_finalize_archives_previous_models_when_applied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = tmp_path / 'logs'
    logs.mkdir()
    stinger_path = tmp_path / 'stinger_config.yaml'
    sweep = tmp_path / 'sweep.csv'
    sweep.write_text('timestamp,port_id\n', encoding='utf-8')

    monkeypatch.setattr(
        'quality_cal.core.offset_history_store.get_quality_cal_logs_dir',
        lambda: logs,
    )
    monkeypatch.setattr(
        'quality_cal.core.port_calibrator.get_stinger_config_path',
        lambda: stinger_path,
    )

    previous = {
        'transducer_error_model': _model('prev_t'),
        'alicat_error_model': _model('prev_a'),
    }
    fit = _fit(sweep=sweep, transducer_passed=True, alicat_passed=True)

    monkeypatch.setattr(
        'quality_cal.core.port_calibrator.fit_port_from_sweep_csv',
        lambda *_args, **_kwargs: fit,
    )
    monkeypatch.setattr(
        'quality_cal.core.port_calibrator._read_previous_port_models',
        lambda port_id, stinger_path=None: (previous, stinger_path or Path('cfg')),
    )

    applied_calls: list[str] = []

    def _apply(port_id: str, _fit: PortCalibrationFitResult, **_kwargs: Any) -> Path:
        applied_calls.append(port_id)
        return stinger_path

    monkeypatch.setattr(
        'quality_cal.core.port_calibrator.apply_port_models_to_stinger_config',
        _apply,
    )
    monkeypatch.setattr(
        'quality_cal.core.port_calibrator.rescore_points_with_models',
        lambda points, *_a, **_k: points,
    )

    settings = _settings()
    points = [
        CalibrationPointResult(
            port_id='port_a',
            point_index=1,
            point_total=1,
            target_psia=1.0,
            route='vacuum',
            mensor_psia=1.0,
            alicat_psia=1.01,
            transducer_psia=1.02,
            deviation_psia=-0.01,
            passed=True,
            settle_duration_s=0.0,
            hold_duration_s=1.0,
            sample_count=5,
        )
    ]

    result = finalize_port_calibration(
        sweep,
        'port_a',
        points,
        settings,
        apply_to_stinger=True,
    )
    assert result.fit_summary.applied_to_stinger_config is True
    assert applied_calls == ['port_a']

    rows = list_offset_history('port_a')
    assert len(rows) == 1
    assert rows[0]['applied'] is True
    entry = json.loads(Path(rows[0]['entry_path']).read_text(encoding='utf-8'))
    assert entry['previous_models']['transducer_error_model']['tag'] == 'prev_t'
    assert entry['new_models']['alicat_error_model']['tag'] == 'new_alicat'
    assert entry['apply_skipped_reason'] is None


def test_finalize_archives_when_not_applied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = tmp_path / 'logs'
    logs.mkdir()
    sweep = tmp_path / 'sweep.csv'
    sweep.write_text('timestamp,port_id\n', encoding='utf-8')

    monkeypatch.setattr(
        'quality_cal.core.offset_history_store.get_quality_cal_logs_dir',
        lambda: logs,
    )
    monkeypatch.setattr(
        'quality_cal.core.port_calibrator._read_previous_port_models',
        lambda port_id, stinger_path=None: (
            {'transducer_error_model': None, 'alicat_error_model': None},
            Path('cfg'),
        ),
    )

    fit = _fit(sweep=sweep, transducer_passed=False, alicat_passed=False)
    monkeypatch.setattr(
        'quality_cal.core.port_calibrator.fit_port_from_sweep_csv',
        lambda *_args, **_kwargs: fit,
    )
    monkeypatch.setattr(
        'quality_cal.core.port_calibrator.rescore_points_with_models',
        lambda points, *_a, **_k: points,
    )

    def _should_not_apply(*_a: Any, **_k: Any) -> Path:
        raise AssertionError('apply should not run when models fail')

    monkeypatch.setattr(
        'quality_cal.core.port_calibrator.apply_port_models_to_stinger_config',
        _should_not_apply,
    )

    result = finalize_port_calibration(
        sweep,
        'port_b',
        [],
        _settings(),
        apply_to_stinger=True,
    )
    assert result.fit_summary.applied_to_stinger_config is False

    rows = list_offset_history('port_b')
    assert len(rows) == 1
    assert rows[0]['applied'] is False
    entry = json.loads(Path(rows[0]['entry_path']).read_text(encoding='utf-8'))
    assert entry['apply_skipped_reason'] == 'no_passing_models'
