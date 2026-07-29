"""Pressure calibration helpers for offline fitting and runtime correction."""

from __future__ import annotations

import itertools
import math
import statistics
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

import numpy as np

TORR_PER_PSI = 51.71493256
ONE_TORR_PSI = 1.0 / TORR_PER_PSI

REFERENCE_ALICAT = 'alicat'
REFERENCE_MENSOR = 'mensor'
SENSOR_TRANSDUCER = 'transducer'
SENSOR_ALICAT = 'alicat'

ReferenceKind = Literal['alicat', 'mensor']
SensorKind = Literal['transducer', 'alicat']

REQUIRED_ALIGNMENT_COLUMNS = {
    'timestamp',
    'port_id',
    'phase',
    'target_abs_psi',
    'transducer_abs_psi',
    'alicat_abs_psi',
}

OPTIONAL_ALIGNMENT_COLUMNS = {
    'mensor_abs_psia',
    'transducer_raw_abs_psi',
}


@dataclass
class CalibrationSample:
    """Single alignment sample used by calibration fitting/scoring."""

    index: int
    timestamp: float
    port_id: str
    phase: str
    target_abs_psi: Optional[float]
    transducer_abs_psi: Optional[float]
    alicat_abs_psi: Optional[float]
    mensor_abs_psia: Optional[float] = None


def psi_to_torr(psi: float) -> float:
    """Convert PSI to Torr."""
    return psi * TORR_PER_PSI


def torr_to_psi(torr: float) -> float:
    """Convert Torr to PSI."""
    return torr / TORR_PER_PSI


def _is_static_phase(phase: str) -> bool:
    return phase.startswith('static_')


def _reference_pressure(sample: CalibrationSample, reference: ReferenceKind) -> Optional[float]:
    if reference == REFERENCE_MENSOR:
        return sample.mensor_abs_psia
    return sample.alicat_abs_psi


def _sensor_pressure(sample: CalibrationSample, sensor: SensorKind) -> Optional[float]:
    if sensor == SENSOR_ALICAT:
        return sample.alicat_abs_psi
    return sample.transducer_abs_psi


def filter_samples_pressure_band(
    samples: Sequence[CalibrationSample],
    *,
    min_psi: float = 0.0,
    max_psi: float,
    reference: ReferenceKind = REFERENCE_MENSOR,
) -> List[CalibrationSample]:
    """Keep samples whose reference pressure lies in [min_psi, max_psi]."""
    filtered: List[CalibrationSample] = []
    for sample in samples:
        ref = _reference_pressure(sample, reference)
        if ref is None:
            continue
        if min_psi <= ref <= max_psi:
            filtered.append(sample)
    return filtered


def select_near_target_samples(
    samples: Sequence[CalibrationSample],
    *,
    tolerance_psi: float = 0.2,
    static_only: bool = True,
    reference: ReferenceKind = REFERENCE_ALICAT,
) -> List[CalibrationSample]:
    """Select samples where the reference sensor is near commanded target pressure.

    Rule:
    - target_abs_psi and reference pressure must be present
    - |reference - target_abs_psi| <= tolerance_psi
    - optionally restrict to static phases only
    """
    selected: List[CalibrationSample] = []
    for sample in samples:
        if static_only and not _is_static_phase(sample.phase):
            continue
        ref = _reference_pressure(sample, reference)
        if sample.target_abs_psi is None or ref is None:
            continue
        if abs(ref - sample.target_abs_psi) <= tolerance_psi:
            selected.append(sample)
    return selected


def select_stable_hold_tail_samples(
    samples: Sequence[CalibrationSample],
    *,
    static_only: bool = True,
    tail_fraction: float = 0.5,
    min_tail: int = 5,
) -> List[CalibrationSample]:
    """Keep the last portion of each static target hold (drop settle transients).

    Sweep CSVs include ramp/settle samples inside ``static_*`` phases. Fitting and
    pass/fail on those spikes inflates p99 even when settled means are excellent.
    """
    if not 0.0 < float(tail_fraction) <= 1.0:
        raise ValueError('tail_fraction must be in (0, 1]')
    groups: Dict[float, List[CalibrationSample]] = {}
    for sample in samples:
        if static_only and not _is_static_phase(sample.phase):
            continue
        if sample.target_abs_psi is None:
            continue
        key = round(float(sample.target_abs_psi), 3)
        groups.setdefault(key, []).append(sample)

    selected: List[CalibrationSample] = []
    for key in sorted(groups):
        group = groups[key]
        n_tail = max(int(min_tail), int(math.ceil(len(group) * float(tail_fraction))))
        n_tail = min(len(group), n_tail)
        selected.extend(group[-n_tail:])
    selected.sort(key=lambda s: s.index)
    return selected


def summarize_static_hold_means(
    samples: Sequence[CalibrationSample],
    *,
    sensor: SensorKind = SENSOR_TRANSDUCER,
    static_only: bool = True,
    tail_count: int = 8,
) -> Tuple[List[CalibrationSample], List[float]]:
    """Collapse each static target hold to one mean sample from the last ``tail_count`` rows.

    Returns (mean_samples, per_hold_sensor_std_psi). Mean samples are the right
    unit for interpolating-knot pass/fail: dense within-hold noise can span
    multiple fine segments and inflate p99 even when settled means are excellent.
    """
    groups: Dict[float, List[CalibrationSample]] = {}
    for sample in samples:
        if static_only and not _is_static_phase(sample.phase):
            continue
        if sample.target_abs_psi is None:
            continue
        if _sensor_pressure(sample, sensor) is None:
            continue
        key = round(float(sample.target_abs_psi), 3)
        groups.setdefault(key, []).append(sample)

    means: List[CalibrationSample] = []
    stds: List[float] = []
    for index, key in enumerate(sorted(groups)):
        group = groups[key]
        tail = group[-max(1, int(tail_count)) :]
        sensor_vals = [float(_sensor_pressure(s, sensor)) for s in tail]  # type: ignore[arg-type]
        stds.append(statistics.pstdev(sensor_vals) if len(sensor_vals) > 1 else 0.0)

        def _mean_field(getter) -> Optional[float]:
            vals = [float(v) for s in tail if (v := getter(s)) is not None]
            return statistics.fmean(vals) if vals else None

        means.append(
            CalibrationSample(
                index=index,
                timestamp=float(tail[-1].timestamp),
                port_id=tail[-1].port_id,
                phase=f'static_{key:g}',
                target_abs_psi=float(key),
                transducer_abs_psi=_mean_field(lambda s: s.transducer_abs_psi),
                alicat_abs_psi=_mean_field(lambda s: s.alicat_abs_psi),
                mensor_abs_psia=_mean_field(lambda s: s.mensor_abs_psia),
            )
        )
    return means, stds


def split_train_validation(
    samples: Sequence[CalibrationSample],
    *,
    holdout_stride: int = 5,
) -> Tuple[List[CalibrationSample], List[CalibrationSample]]:
    """Deterministic split by sample index for reproducible holdout."""
    if holdout_stride < 2:
        raise ValueError('holdout_stride must be >= 2')
    train: List[CalibrationSample] = []
    validation: List[CalibrationSample] = []
    for i, sample in enumerate(samples):
        if i % holdout_stride == 0:
            validation.append(sample)
        else:
            train.append(sample)
    return train, validation


def _quantile(values: Sequence[float], q: float) -> float:
    if not values:
        raise ValueError('Cannot compute quantile of empty values')
    if q <= 0:
        return min(values)
    if q >= 1:
        return max(values)
    s = sorted(values)
    pos = (len(s) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return s[lo]
    frac = pos - lo
    return s[lo] * (1.0 - frac) + s[hi] * frac


def _linear_fit(xs: Sequence[float], ys: Sequence[float]) -> Tuple[float, float]:
    if len(xs) != len(ys) or len(xs) < 2:
        raise ValueError('Need at least two samples for linear fit')
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    ss_xx = sum((x - mean_x) ** 2 for x in xs)
    if ss_xx <= 0:
        return 0.0, mean_y
    ss_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = ss_xy / ss_xx
    intercept = mean_y - slope * mean_x
    return slope, intercept


def _percentile_candidates(segment_count: int) -> List[Tuple[float, ...]]:
    if segment_count == 3:
        grid = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90]
        return list(itertools.combinations(grid, 2))
    if segment_count == 5:
        grid = [0.08, 0.16, 0.24, 0.32, 0.40, 0.52, 0.64, 0.76, 0.88]
        return list(itertools.combinations(grid, 4))
    if segment_count == 7:
        grid = [0.06, 0.12, 0.20, 0.30, 0.42, 0.55, 0.68, 0.80, 0.90]
        return list(itertools.combinations(grid, 6))
    raise ValueError(f'Unsupported segment_count={segment_count}')


def _fit_piecewise_for_breakpoints(
    xs: Sequence[float],
    ys: Sequence[float],
    breakpoints: Sequence[float],
    *,
    min_segment_size: int = 20,
) -> Optional[List[Tuple[float, float]]]:
    lines: List[Tuple[float, float]] = []
    lower = -float('inf')
    for upper in list(breakpoints) + [float('inf')]:
        seg_x = [x for x in xs if lower <= x < upper]
        seg_y = [y for x, y in zip(xs, ys) if lower <= x < upper]
        if len(seg_x) < min_segment_size:
            return None
        slope, intercept = _linear_fit(seg_x, seg_y)
        lines.append((slope, intercept))
        lower = upper
    return lines


def _pressure_axis_value(
    sample: CalibrationSample,
    *,
    measured: float,
    pressure_axis: Literal['measured', 'target'],
) -> float:
    if pressure_axis == 'target' and sample.target_abs_psi is not None:
        return float(sample.target_abs_psi)
    return float(measured)


def _extract_fit_pairs(
    samples: Sequence[CalibrationSample],
    *,
    sensor: SensorKind,
    reference: ReferenceKind,
    pressure_axis: Literal['measured', 'target'] = 'measured',
) -> Tuple[List[float], List[float]]:
    xs: List[float] = []
    ys: List[float] = []
    for sample in samples:
        measured = _sensor_pressure(sample, sensor)
        ref = _reference_pressure(sample, reference)
        if measured is None or ref is None:
            continue
        xs.append(_pressure_axis_value(sample, measured=float(measured), pressure_axis=pressure_axis))
        ys.append(float(measured) - float(ref))
    return xs, ys


def evaluate_error_model(
    pressure_psi: float,
    model: Optional[Dict[str, Any]],
    *,
    target_psi: Optional[float] = None,
) -> float:
    """Return modeled sensor error(psi) at the given pressure."""
    if not model:
        return 0.0
    axis = str(model.get('pressure_axis', 'measured')).strip().lower()
    lookup_psi = float(target_psi) if axis == 'target' and target_psi is not None else float(pressure_psi)
    model_type = str(model.get('type', '')).strip().lower()
    if model_type == 'piecewise_linear':
        segments = model.get('segments', [])
        if not isinstance(segments, list) or not segments:
            return 0.0
        for segment in segments:
            max_psi = segment.get('max_psi')
            if max_psi is not None and lookup_psi >= float(max_psi):
                continue
            slope = float(segment.get('slope_error_per_psi', 0.0))
            intercept = float(segment.get('intercept_error_psi', 0.0))
            return slope * lookup_psi + intercept
        last = segments[-1]
        slope = float(last.get('slope_error_per_psi', 0.0))
        intercept = float(last.get('intercept_error_psi', 0.0))
        return slope * lookup_psi + intercept
    if model_type == 'quadratic':
        a = float(model.get('a_error_per_psi2', 0.0))
        b = float(model.get('b_error_per_psi', 0.0))
        c = float(model.get('c_error_psi', 0.0))
        return a * lookup_psi * lookup_psi + b * lookup_psi + c
    return 0.0


def apply_error_model(
    pressure_psi: float,
    model: Optional[Dict[str, Any]],
    *,
    target_psi: Optional[float] = None,
) -> float:
    """Apply error model as corrected = measured - modeled_error."""
    return pressure_psi - evaluate_error_model(pressure_psi, model, target_psi=target_psi)


def build_legacy_two_band_model(
    *,
    breakpoint_psi: float,
    low_slope_error_per_psi: float,
    low_intercept_error_psi: float,
    high_slope_error_per_psi: float,
    high_intercept_error_psi: float,
) -> Dict[str, Any]:
    """Convert existing two-band config fields to generic piecewise config."""
    return {
        'type': 'piecewise_linear',
        'segments': [
            {
                'max_psi': float(breakpoint_psi),
                'slope_error_per_psi': float(low_slope_error_per_psi),
                'intercept_error_psi': float(low_intercept_error_psi),
            },
            {
                'max_psi': None,
                'slope_error_per_psi': float(high_slope_error_per_psi),
                'intercept_error_psi': float(high_intercept_error_psi),
            },
        ],
    }


def replay_corrected_series(
    measured_pressures_psi: Sequence[float],
    *,
    model: Optional[Dict[str, Any]],
    ema_alpha: float,
) -> List[float]:
    """Replay correction + optional EMA over a pressure series."""
    corrected: List[float] = []
    ema_value: Optional[float] = None
    alpha = float(ema_alpha)
    for raw in measured_pressures_psi:
        adjusted = apply_error_model(float(raw), model)
        if alpha <= 0.0 or alpha >= 1.0:
            ema_value = adjusted
        elif ema_value is None:
            ema_value = adjusted
        else:
            ema_value = alpha * adjusted + (1.0 - alpha) * ema_value
        corrected.append(float(ema_value))
    return corrected


def build_piecewise_from_error_knots(
    knots: Sequence[Tuple[float, float]],
    *,
    pressure_axis: Literal['measured', 'target'] = 'measured',
    min_knot_spacing_psi: float = 0.01,
) -> Dict[str, Any]:
    """Build a piecewise-linear error model that interpolates (pressure, error) knots.

    Knots are (axis_pressure_psi, error_psi) sorted by pressure. Consecutive knots
    closer than ``min_knot_spacing_psi`` are averaged. Between knots the modeled
    error is linear, so applying corrected = measured - error hits the knot
    residuals exactly (within float noise).
    """
    if len(knots) < 2:
        raise ValueError('Need at least two error knots for interpolating piecewise')
    ordered = sorted((float(x), float(e)) for x, e in knots)
    merged: List[List[float]] = [[ordered[0][0], ordered[0][1], 1.0]]
    for x, e in ordered[1:]:
        if abs(x - merged[-1][0]) < float(min_knot_spacing_psi):
            n = merged[-1][2] + 1.0
            merged[-1][0] = (merged[-1][0] * merged[-1][2] + x) / n
            merged[-1][1] = (merged[-1][1] * merged[-1][2] + e) / n
            merged[-1][2] = n
        else:
            merged.append([x, e, 1.0])
    if len(merged) < 2:
        raise ValueError('Need at least two distinct knots after merging close pressures')

    segments: List[Dict[str, Any]] = []
    for i in range(len(merged) - 1):
        x0, e0, _ = merged[i]
        x1, e1, _ = merged[i + 1]
        slope = (e1 - e0) / (x1 - x0)
        intercept = e0 - slope * x0
        segments.append(
            {
                'max_psi': float(x1),
                'slope_error_per_psi': float(slope),
                'intercept_error_psi': float(intercept),
            }
        )
    x_last, e_last, _ = merged[-1]
    x_prev, e_prev, _ = merged[-2]
    slope = (e_last - e_prev) / (x_last - x_prev)
    intercept = e_last - slope * x_last
    segments.append(
        {
            'max_psi': None,
            'slope_error_per_psi': float(slope),
            'intercept_error_psi': float(intercept),
        }
    )
    return {
        'type': 'piecewise_linear',
        'segments': segments,
        'pressure_axis': pressure_axis,
        'fit_method': 'interpolating_knots',
    }


def fit_interpolating_piecewise_error_model(
    train_samples: Sequence[CalibrationSample],
    *,
    sensor: SensorKind = SENSOR_TRANSDUCER,
    reference: ReferenceKind = REFERENCE_ALICAT,
    pressure_axis: Literal['measured', 'target'] = 'measured',
    min_knot_spacing_psi: float = 0.01,
    static_only: bool = True,
    min_samples_per_knot: int = 1,
) -> Dict[str, Any]:
    """Fit piecewise-linear error by interpolating mean error at each static target.

    Prefer this for transducers when Quality Cal has stable holds across the
    profile band (``fit_max_psia``). The model only corrects within/near the
    swept span; it does not invent behavior above the highest knot.
    """
    groups: Dict[float, List[Tuple[float, float]]] = {}
    for sample in train_samples:
        if static_only and not _is_static_phase(sample.phase):
            continue
        measured = _sensor_pressure(sample, sensor)
        ref = _reference_pressure(sample, reference)
        if measured is None or ref is None:
            continue
        axis = _pressure_axis_value(
            sample,
            measured=float(measured),
            pressure_axis=pressure_axis,
        )
        if sample.target_abs_psi is not None:
            key = round(float(sample.target_abs_psi), 3)
        else:
            key = round(float(axis), 3)
        groups.setdefault(key, []).append((float(axis), float(measured) - float(ref)))

    knots: List[Tuple[float, float]] = []
    for key in sorted(groups):
        pairs = groups[key]
        if len(pairs) < int(min_samples_per_knot):
            continue
        mean_x = statistics.fmean(x for x, _ in pairs)
        mean_e = statistics.fmean(e for _, e in pairs)
        knots.append((mean_x, mean_e))

    return build_piecewise_from_error_knots(
        knots,
        pressure_axis=pressure_axis,
        min_knot_spacing_psi=min_knot_spacing_psi,
    )


def fit_piecewise_linear_error_model(
    train_samples: Sequence[CalibrationSample],
    *,
    segment_count: int,
    min_segment_size: int = 20,
    sensor: SensorKind = SENSOR_TRANSDUCER,
    reference: ReferenceKind = REFERENCE_ALICAT,
    pressure_axis: Literal['measured', 'target'] = 'measured',
) -> Dict[str, Any]:
    """Fit piecewise-linear model for error vs measured pressure."""
    if segment_count not in {3, 5, 7}:
        raise ValueError('segment_count must be 3, 5, or 7')
    xs, ys = _extract_fit_pairs(
        train_samples,
        sensor=sensor,
        reference=reference,
        pressure_axis=pressure_axis,
    )
    if len(xs) < min_segment_size * segment_count:
        raise ValueError('Not enough training samples for requested segment count')

    breakpoint_quantiles = _percentile_candidates(segment_count)
    best_model: Optional[Dict[str, Any]] = None
    best_mae = float('inf')
    for q_tuple in breakpoint_quantiles:
        breakpoints = [_quantile(xs, q) for q in q_tuple]
        if any(b2 <= b1 for b1, b2 in zip(breakpoints, breakpoints[1:])):
            continue
        lines = _fit_piecewise_for_breakpoints(xs, ys, breakpoints, min_segment_size=min_segment_size)
        if lines is None:
            continue

        segments = []
        for i, (slope, intercept) in enumerate(lines):
            segments.append(
                {
                    'max_psi': (breakpoints[i] if i < len(breakpoints) else None),
                    'slope_error_per_psi': slope,
                    'intercept_error_psi': intercept,
                }
            )
        model = {
            'type': 'piecewise_linear',
            'segments': segments,
            'pressure_axis': pressure_axis,
        }
        true_refs = [x - y for x, y in zip(xs, ys)]
        residuals = [
            abs(apply_error_model(x, model, target_psi=x if pressure_axis == 'target' else None) - ref)
            for x, ref in zip(xs, true_refs)
        ]
        mae = statistics.fmean(residuals)
        if mae < best_mae:
            best_mae = mae
            best_model = model

    if best_model is None:
        raise ValueError('Unable to fit piecewise-linear model with current constraints')
    return best_model


def fit_quadratic_error_model(
    train_samples: Sequence[CalibrationSample],
    *,
    sensor: SensorKind = SENSOR_TRANSDUCER,
    reference: ReferenceKind = REFERENCE_ALICAT,
    pressure_axis: Literal['measured', 'target'] = 'measured',
) -> Dict[str, Any]:
    """Fit quadratic error model for error vs measured pressure."""
    xs, ys = _extract_fit_pairs(
        train_samples,
        sensor=sensor,
        reference=reference,
        pressure_axis=pressure_axis,
    )
    if len(xs) < 3:
        raise ValueError('Need at least 3 samples to fit quadratic model')
    coeff = np.polyfit(np.array(xs, dtype=float), np.array(ys, dtype=float), deg=2)
    a, b, c = coeff
    return {
        'type': 'quadratic',
        'a_error_per_psi2': float(a),
        'b_error_per_psi': float(b),
        'c_error_psi': float(c),
        'pressure_axis': pressure_axis,
    }


def _quantile_abs(values: Sequence[float], q: float) -> float:
    if not values:
        return float('nan')
    return _quantile([abs(v) for v in values], q)


def smooth_error_knots(
    knots: Sequence[Tuple[float, float]],
    lambda_roughness: float,
) -> List[Tuple[float, float]]:
    """Apply ridge smoothing to (pressure, error) knots.

    Finds smoothed errors ê minimising:
        Σ (ê_i - e_i)² + λ Σ ((ê_{i+1} - ê_i) / h_i)²
    where h_i = x_{i+1} - x_i.

    With λ=0 returns the original knots unchanged.  Large λ → linear trendline.
    Solved as (I + λ D^T D) ê = e (numpy linear-algebra, no scipy).
    """
    n = len(knots)
    if n < 2:
        return list(knots)
    xs = np.array([float(k[0]) for k in knots])
    es = np.array([float(k[1]) for k in knots])
    if lambda_roughness <= 0.0:
        return [(float(xs[i]), float(es[i])) for i in range(n)]
    h = np.diff(xs)
    # D is (n-1) × n: row i divides the finite difference by h_i
    D = np.zeros((n - 1, n))
    for i in range(n - 1):
        D[i, i] = -1.0 / float(h[i])
        D[i, i + 1] = 1.0 / float(h[i])
    A = np.eye(n) + lambda_roughness * (D.T @ D)
    e_smooth = np.linalg.solve(A, es)
    return [(float(xs[i]), float(e_smooth[i])) for i in range(n)]


def fit_smoothed_interpolating_error_model(
    train_samples: Sequence[CalibrationSample],
    *,
    sensor: SensorKind = SENSOR_TRANSDUCER,
    reference: ReferenceKind = REFERENCE_ALICAT,
    pressure_axis: Literal['measured', 'target'] = 'measured',
    min_knot_spacing_psi: float = 0.01,
    lambda_roughness: float = 1e-3,
    static_only: bool = True,
    min_samples_per_knot: int = 1,
) -> Dict[str, Any]:
    """Interpolating piecewise fit with ridge-roughness smoothing across knots.

    Same hold-mean grouping as ``fit_interpolating_piecewise_error_model`` but
    the target error at each knot is softened by a Tikhonov penalty on adjacent
    slope differences before building segments.  This prevents steep inter-knot
    segments that amplify transducer jitter.  ``lambda_roughness=0`` reproduces
    the exact interpolator; larger values yield smoother curves at the cost of
    non-zero residual at individual knots.
    """
    groups: Dict[float, List[Tuple[float, float]]] = {}
    for sample in train_samples:
        if static_only and not _is_static_phase(sample.phase):
            continue
        measured = _sensor_pressure(sample, sensor)
        ref = _reference_pressure(sample, reference)
        if measured is None or ref is None:
            continue
        axis = _pressure_axis_value(sample, measured=float(measured), pressure_axis=pressure_axis)
        key = (
            round(float(sample.target_abs_psi), 3)
            if sample.target_abs_psi is not None
            else round(float(axis), 3)
        )
        groups.setdefault(key, []).append((float(axis), float(measured) - float(ref)))

    raw_knots: List[Tuple[float, float]] = []
    for key in sorted(groups):
        pairs = groups[key]
        if len(pairs) < int(min_samples_per_knot):
            continue
        mean_x = statistics.fmean(x for x, _ in pairs)
        mean_e = statistics.fmean(e for _, e in pairs)
        raw_knots.append((mean_x, mean_e))

    smoothed_knots = smooth_error_knots(raw_knots, lambda_roughness)
    model = build_piecewise_from_error_knots(
        smoothed_knots,
        pressure_axis=pressure_axis,
        min_knot_spacing_psi=min_knot_spacing_psi,
    )
    model['fit_method'] = 'smoothed_interpolating_knots'
    model['lambda_roughness'] = float(lambda_roughness)
    return model


def score_band_replay(
    samples: Sequence[CalibrationSample],
    *,
    model: Optional[Dict[str, Any]],
    ema_alpha: float,
    band_center_psi: float,
    band_half_width_psi: float = 0.05,
    sensor: SensorKind = SENSOR_TRANSDUCER,
    reference: ReferenceKind = REFERENCE_ALICAT,
) -> Dict[str, float]:
    """Score a replay only for samples whose target falls within a pressure band.

    Useful for isolating a specific operating point — e.g. the 15 Torr (~0.29 PSIA)
    hold — without re-running the full replay.  EMA is still computed over all
    samples in order; only the error statistics are restricted to the band.
    """
    include_mask = [
        sample.target_abs_psi is not None
        and abs(float(sample.target_abs_psi) - band_center_psi) <= band_half_width_psi
        for sample in samples
    ]
    return score_replay(
        samples,
        model=model,
        ema_alpha=ema_alpha,
        include_mask=include_mask,
        sensor=sensor,
        reference=reference,
    )


def score_error_series_torr(errors_psi: Sequence[float]) -> Dict[str, float]:
    """Compute absolute error metrics in Torr from psi errors."""
    if not errors_psi:
        return {
            'n': 0,
            'mean_abs_torr': float('nan'),
            'p95_abs_torr': float('nan'),
            'p99_abs_torr': float('nan'),
            'max_abs_torr': float('nan'),
        }
    abs_torr = [psi_to_torr(abs(e)) for e in errors_psi]
    return {
        'n': float(len(errors_psi)),
        'mean_abs_torr': float(statistics.fmean(abs_torr)),
        'p95_abs_torr': float(_quantile(abs_torr, 0.95)),
        'p99_abs_torr': float(_quantile(abs_torr, 0.99)),
        'max_abs_torr': float(max(abs_torr)),
    }


def score_replay(
    samples: Sequence[CalibrationSample],
    *,
    model: Optional[Dict[str, Any]],
    ema_alpha: float,
    include_mask: Optional[Sequence[bool]] = None,
    sensor: SensorKind = SENSOR_TRANSDUCER,
    reference: ReferenceKind = REFERENCE_ALICAT,
) -> Dict[str, float]:
    """Replay a model over ordered samples and score selected points vs reference."""
    measured: List[float] = []
    reference_vals: List[float] = []
    for sample in samples:
        m = _sensor_pressure(sample, sensor)
        r = _reference_pressure(sample, reference)
        if m is None or r is None:
            measured.append(float('nan'))
            reference_vals.append(float('nan'))
        else:
            measured.append(float(m))
            reference_vals.append(float(r))

    replayed: List[float] = []
    ema_value: Optional[float] = None
    alpha = float(ema_alpha)
    last_target: Optional[float] = None
    for sample, raw, ref in zip(samples, measured, reference_vals):
        if sample.target_abs_psi is not None and (
            last_target is None or abs(float(sample.target_abs_psi) - last_target) > 1e-6
        ):
            ema_value = None
            last_target = float(sample.target_abs_psi)
        target_psi = float(sample.target_abs_psi) if sample.target_abs_psi is not None else None
        adjusted = (
            apply_error_model(float(raw), model, target_psi=target_psi)
            if math.isfinite(raw)
            else float('nan')
        )
        if not math.isfinite(adjusted):
            replayed.append(float('nan'))
            continue
        if alpha <= 0.0 or alpha >= 1.0:
            ema_value = adjusted
        elif ema_value is None:
            ema_value = adjusted
        else:
            ema_value = alpha * adjusted + (1.0 - alpha) * ema_value
        replayed.append(float(ema_value))
    if include_mask is None:
        include_mask = [True] * len(samples)
    errors = [
        pred - ref
        for pred, ref, include in zip(replayed, reference_vals, include_mask)
        if include and math.isfinite(pred) and math.isfinite(ref)
    ]
    return score_error_series_torr(errors)
