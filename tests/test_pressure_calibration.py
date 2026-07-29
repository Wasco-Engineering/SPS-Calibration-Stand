"""Unit tests for pressure calibration helpers."""

from __future__ import annotations

import math

import pytest

from app.services.pressure_calibration import (
    REFERENCE_MENSOR,
    SENSOR_TRANSDUCER,
    apply_error_model,
    filter_samples_pressure_band,
    fit_interpolating_piecewise_error_model,
    fit_piecewise_linear_error_model,
    fit_quadratic_error_model,
    psi_to_torr,
    replay_corrected_series,
    score_replay,
    select_near_target_samples,
    select_stable_hold_tail_samples,
    split_train_validation,
    summarize_static_hold_means,
    torr_to_psi,
)
from tests.fixtures.pressure_data import calibration_sample


def _sample(idx: int, *, phase: str = 'static_10', target: float = 10.0, transducer: float = 10.0, alicat: float = 10.0):
    return calibration_sample(
        idx,
        phase=phase,
        target=target,
        transducer=transducer,
        alicat=alicat,
    )


def test_psi_torr_round_trip() -> None:
    psi = 2.5
    torr = psi_to_torr(psi)
    assert torr_to_psi(torr) == pytest.approx(psi)


def test_select_near_target_mensor_reference() -> None:
    samples = [
        calibration_sample(0, phase='static_10', target=10.0, mensor=10.05, alicat=10.2),
        calibration_sample(1, phase='static_10', target=10.0, mensor=12.0, alicat=10.0),
    ]
    selected = select_near_target_samples(
        samples,
        tolerance_psi=0.2,
        static_only=True,
        reference=REFERENCE_MENSOR,
    )
    assert [s.index for s in selected] == [0]


def test_filter_samples_pressure_band() -> None:
    samples = [
        calibration_sample(0, target=5.0, mensor=5.0),
        calibration_sample(1, target=25.0, mensor=25.0),
    ]
    filtered = filter_samples_pressure_band(samples, min_psi=0.0, max_psi=20.0, reference=REFERENCE_MENSOR)
    assert [s.index for s in filtered] == [0]


def test_select_near_target_static_only() -> None:
    samples = [
        _sample(0, phase='static_10', target=10.0, alicat=10.1, transducer=10.2),
        _sample(1, phase='dynamic_up', target=10.0, alicat=10.0, transducer=10.3),
        _sample(2, phase='static_10', target=10.0, alicat=10.4, transducer=10.3),
    ]
    selected = select_near_target_samples(samples, tolerance_psi=0.2, static_only=True)
    assert [s.index for s in selected] == [0]


def test_split_train_validation_stride() -> None:
    samples = [_sample(i) for i in range(10)]
    train, val = split_train_validation(samples, holdout_stride=4)
    assert [s.index for s in val] == [0, 4, 8]
    assert len(train) == 7


def test_apply_error_model_piecewise_and_replay_ema() -> None:
    model = {
        'type': 'piecewise_linear',
        'segments': [
            {'max_psi': 15.0, 'slope_error_per_psi': 0.0, 'intercept_error_psi': 1.0},
            {'max_psi': None, 'slope_error_per_psi': 0.0, 'intercept_error_psi': 2.0},
        ],
    }
    assert apply_error_model(10.0, model) == pytest.approx(9.0)
    assert apply_error_model(20.0, model) == pytest.approx(18.0)

    series = replay_corrected_series([10.0, 20.0], model=model, ema_alpha=0.5)
    # corrected raw = [9.0, 18.0], EMA(alpha=0.5) => [9.0, 13.5]
    assert series == pytest.approx([9.0, 13.5])


def test_fit_models_and_score_replay() -> None:
    # Synthetic linear error model: error = 0.01*x + 0.2
    # alicat = transducer - error
    samples = []
    for idx, transducer in enumerate([float(v) for v in range(1, 101)]):
        error = 0.01 * transducer + 0.2
        alicat = transducer - error
        samples.append(_sample(idx, target=transducer, transducer=transducer, alicat=alicat))

    near = select_near_target_samples(samples, tolerance_psi=2.0, static_only=True)
    train, validation = split_train_validation(near, holdout_stride=5)
    validation_idx = {s.index for s in validation}
    mask = [s.index in validation_idx for s in near]

    piecewise3 = fit_piecewise_linear_error_model(train, segment_count=3, min_segment_size=10)
    quadratic = fit_quadratic_error_model(train)

    piecewise_score = score_replay(near, model=piecewise3, ema_alpha=0.0, include_mask=mask)
    quadratic_score = score_replay(near, model=quadratic, ema_alpha=0.0, include_mask=mask)

    assert piecewise_score['p99_abs_torr'] < 0.5
    assert quadratic_score['p99_abs_torr'] < 0.5
    assert math.isfinite(piecewise_score['mean_abs_torr'])
    assert math.isfinite(quadratic_score['mean_abs_torr'])


def test_fit_and_score_vs_mensor_reference() -> None:
    samples = []
    for idx, mensor in enumerate([float(v) for v in range(1, 121)]):
        error = 0.008 * mensor + 0.05
        transducer = mensor + error
        samples.append(
            calibration_sample(
                idx,
                target=mensor,
                transducer=transducer,
                alicat=mensor,
                mensor=mensor,
            )
        )
    near = select_near_target_samples(
        samples,
        tolerance_psi=2.0,
        static_only=True,
        reference=REFERENCE_MENSOR,
    )
    train, validation = split_train_validation(near, holdout_stride=5)
    validation_idx = {s.index for s in validation}
    mask = [s.index in validation_idx for s in near]

    model = fit_piecewise_linear_error_model(
        train,
        segment_count=3,
        min_segment_size=10,
        sensor=SENSOR_TRANSDUCER,
        reference=REFERENCE_MENSOR,
    )
    score = score_replay(
        near,
        model=model,
        ema_alpha=0.0,
        include_mask=mask,
        sensor=SENSOR_TRANSDUCER,
        reference=REFERENCE_MENSOR,
    )
    assert score['p99_abs_torr'] < 1.0


def test_interpolating_piecewise_hits_static_means() -> None:
    """Knot interpolator zeros mean residual at each static target."""
    targets = [0.29, 1.0, 5.0, 15.0, 30.0]
    samples = []
    idx = 0
    for target in targets:
        # Nonlinear bias vs Mensor (shape quantile fits struggle with).
        mensor = target
        error = 0.07 + 0.01 * math.sin(target) - 0.002 * target
        transducer = mensor + error
        for _ in range(5):
            samples.append(
                calibration_sample(
                    idx,
                    phase=f'static_{target}',
                    target=target,
                    transducer=transducer,
                    alicat=mensor,
                    mensor=mensor,
                )
            )
            idx += 1

    model = fit_interpolating_piecewise_error_model(
        samples,
        sensor=SENSOR_TRANSDUCER,
        reference=REFERENCE_MENSOR,
        min_samples_per_knot=3,
    )
    assert model['fit_method'] == 'interpolating_knots'
    assert len(model['segments']) == len(targets)

    for target in targets:
        mensor = target
        error = 0.07 + 0.01 * math.sin(target) - 0.002 * target
        transducer = mensor + error
        corrected = apply_error_model(transducer, model)
        assert corrected == pytest.approx(mensor, abs=1e-9)

    score = score_replay(
        samples,
        model=model,
        ema_alpha=0.0,
        sensor=SENSOR_TRANSDUCER,
        reference=REFERENCE_MENSOR,
    )
    assert score['max_abs_torr'] == pytest.approx(0.0, abs=1e-6)


def test_interpolating_merges_close_knots() -> None:
    samples = []
    idx = 0
    for target, transducer, mensor in (
        (0.29, 0.36, 0.29),
        (0.30, 0.361, 0.295),  # nearly same measured X → merge
        (5.0, 5.1, 5.0),
    ):
        for _ in range(3):
            samples.append(
                calibration_sample(
                    idx,
                    phase=f'static_{target}',
                    target=target,
                    transducer=transducer,
                    alicat=mensor,
                    mensor=mensor,
                )
            )
            idx += 1
    model = fit_interpolating_piecewise_error_model(
        samples,
        sensor=SENSOR_TRANSDUCER,
        reference=REFERENCE_MENSOR,
        min_knot_spacing_psi=0.05,
        min_samples_per_knot=3,
    )
    # 3 targets but first two merge → 2 knots → 2 segments (last open-ended)
    assert len(model['segments']) == 2


def test_select_stable_hold_tail_samples() -> None:
    samples = []
    idx = 0
    for target in (1.0, 5.0):
        for i in range(10):
            samples.append(
                calibration_sample(
                    idx,
                    phase=f'static_{target}',
                    target=target,
                    transducer=float(target + i),
                    alicat=target,
                    mensor=target,
                )
            )
            idx += 1
    tail = select_stable_hold_tail_samples(samples, tail_fraction=0.4, min_tail=3)
    # 40% of 10 = 4 per target, min_tail 3 → 4 each → 8 total
    assert len(tail) == 8
    assert all(s.transducer_abs_psi >= s.target_abs_psi + 6 for s in tail)


def test_summarize_static_hold_means_for_interp_pass() -> None:
    samples = []
    idx = 0
    for target, bias in ((0.29, 0.07), (5.0, 0.04), (15.0, -0.15)):
        for i in range(12):
            # Early samples noisy; last samples stable around true bias.
            noise = 0.2 if i < 4 else 0.0
            mensor = target
            transducer = target + bias + noise
            samples.append(
                calibration_sample(
                    idx,
                    phase=f'static_{target}',
                    target=target,
                    transducer=transducer,
                    alicat=mensor,
                    mensor=mensor,
                )
            )
            idx += 1
    means, stds = summarize_static_hold_means(samples, tail_count=8)
    assert len(means) == 3
    assert all(s >= 0.0 for s in stds)
    model = fit_interpolating_piecewise_error_model(
        means,
        sensor=SENSOR_TRANSDUCER,
        reference=REFERENCE_MENSOR,
        min_samples_per_knot=1,
        min_knot_spacing_psi=0.05,
    )
    score = score_replay(
        means,
        model=model,
        ema_alpha=0.0,
        sensor=SENSOR_TRANSDUCER,
        reference=REFERENCE_MENSOR,
    )
    assert score['p99_abs_torr'] < 0.05

