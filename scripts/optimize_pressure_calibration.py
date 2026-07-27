"""Offline optimizer for transducer/Alicat correction models vs Alicat or Mensor reference.

Usage:
  python scripts/optimize_pressure_calibration.py \
    --input-csv scripts/data/alignment_with_mensor.csv \
    --output-dir scripts/data/mensor_opt \
    --reference mensor --sensor both --fit-max-psi 20 --pass-threshold-torr 1.0

Input schema (required columns):
  - timestamp, port_id, phase, target_abs_psi, alicat_abs_psi, transducer_abs_psi

For --reference mensor:
  - mensor_abs_psia (or mensor_abs_psi) column required

Optional:
  - transducer_raw_abs_psi (preferred raw transducer for fitting)
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.services.pressure_calibration import (  # noqa: E402
    REFERENCE_ALICAT,
    REFERENCE_MENSOR,
    SENSOR_ALICAT,
    SENSOR_TRANSDUCER,
    CalibrationSample,
    REQUIRED_ALIGNMENT_COLUMNS,
    ReferenceKind,
    SensorKind,
    filter_samples_pressure_band,
    fit_piecewise_linear_error_model,
    fit_quadratic_error_model,
    score_replay,
    select_near_target_samples,
    split_train_validation,
)


@dataclass
class CandidateResult:
    port_id: str
    sensor: str
    family: str
    candidate_name: str
    ema_alpha: float
    parameter_count: int
    p99_abs_torr: float
    mean_abs_torr: float
    max_abs_torr: float
    p95_abs_torr: float
    n_validation: int
    passed: bool
    model: Dict[str, Any]


def _parse_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_mensor(row: Dict[str, Any]) -> Optional[float]:
    for key in ('mensor_abs_psia', 'mensor_abs_psi', 'mensor_psia'):
        value = _parse_float(row.get(key))
        if value is not None:
            return value
    return None


def _load_samples(paths: Sequence[Path], port_id: str) -> List[CalibrationSample]:
    samples: List[CalibrationSample] = []
    idx = 0
    for path in paths:
        with path.open('r', newline='', encoding='utf-8') as handle:
            reader = csv.DictReader(handle)
            columns = set(reader.fieldnames or [])
            missing = sorted(REQUIRED_ALIGNMENT_COLUMNS - columns)
            if missing:
                raise ValueError(f'{path} missing required columns: {missing}')
            for row in reader:
                if str(row.get('port_id', '')).strip().lower() != port_id:
                    continue
                trans_raw = _parse_float(row.get('transducer_raw_abs_psi'))
                trans_measured = trans_raw if trans_raw is not None else _parse_float(row.get('transducer_abs_psi'))
                sample = CalibrationSample(
                    index=idx,
                    timestamp=_parse_float(row.get('timestamp')) or 0.0,
                    port_id=port_id,
                    phase=str(row.get('phase', '')).strip(),
                    target_abs_psi=_parse_float(row.get('target_abs_psi')),
                    transducer_abs_psi=trans_measured,
                    alicat_abs_psi=_parse_float(row.get('alicat_abs_psi')),
                    mensor_abs_psia=_parse_mensor(row),
                )
                idx += 1
                # Transducer is optional (open-fitting / SPS stands omit it).
                # Keep rows that have Alicat data so Alicat-vs-Mensor fits still run.
                if sample.alicat_abs_psi is None:
                    continue
                samples.append(sample)
    if not samples:
        raise ValueError(f'No samples loaded for {port_id}.')
    return samples


def _parameter_count(model: Dict[str, Any]) -> int:
    model_type = str(model.get('type', '')).strip().lower()
    if model_type == 'quadratic':
        return 3
    if model_type == 'piecewise_linear':
        segments = model.get('segments', [])
        if not isinstance(segments, list):
            return 0
        finite_breakpoints = sum(1 for s in segments if s.get('max_psi') is not None)
        return len(segments) * 2 + finite_breakpoints
    return 0


def _score_candidate(
    *,
    port_id: str,
    sensor: SensorKind,
    reference: ReferenceKind,
    family: str,
    candidate_name: str,
    model: Dict[str, Any],
    alpha: float,
    samples: Sequence[CalibrationSample],
    validation_mask: Sequence[bool],
    pass_threshold_torr: float,
) -> CandidateResult:
    score = score_replay(
        samples,
        model=model,
        ema_alpha=alpha,
        include_mask=validation_mask,
        sensor=sensor,
        reference=reference,
    )
    n_validation = int(score['n'])
    p99 = float(score['p99_abs_torr'])
    return CandidateResult(
        port_id=port_id,
        sensor=sensor,
        family=family,
        candidate_name=candidate_name,
        ema_alpha=float(alpha),
        parameter_count=_parameter_count(model),
        p99_abs_torr=p99,
        mean_abs_torr=float(score['mean_abs_torr']),
        max_abs_torr=float(score['max_abs_torr']),
        p95_abs_torr=float(score['p95_abs_torr']),
        n_validation=n_validation,
        passed=bool(n_validation > 0 and p99 <= pass_threshold_torr),
        model=model,
    )


def _as_dict(result: CandidateResult) -> Dict[str, Any]:
    return {
        'port_id': result.port_id,
        'sensor': result.sensor,
        'family': result.family,
        'candidate_name': result.candidate_name,
        'ema_alpha': result.ema_alpha,
        'parameter_count': result.parameter_count,
        'p99_abs_torr': result.p99_abs_torr,
        'mean_abs_torr': result.mean_abs_torr,
        'p95_abs_torr': result.p95_abs_torr,
        'max_abs_torr': result.max_abs_torr,
        'n_validation': result.n_validation,
        'passed': result.passed,
        'model': result.model,
    }


def _rank_results(results: List[CandidateResult]) -> List[CandidateResult]:
    return sorted(
        results,
        key=lambda r: (r.p99_abs_torr, r.mean_abs_torr, r.parameter_count, r.max_abs_torr),
    )


def _write_ranking_csv(path: Path, ranked: Sequence[CandidateResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                'port_id',
                'sensor',
                'family',
                'candidate_name',
                'ema_alpha',
                'parameter_count',
                'p99_abs_torr',
                'mean_abs_torr',
                'p95_abs_torr',
                'max_abs_torr',
                'n_validation',
                'passed',
            ],
        )
        writer.writeheader()
        for result in ranked:
            writer.writerow(
                {
                    'port_id': result.port_id,
                    'sensor': result.sensor,
                    'family': result.family,
                    'candidate_name': result.candidate_name,
                    'ema_alpha': f'{result.ema_alpha:.4f}',
                    'parameter_count': result.parameter_count,
                    'p99_abs_torr': f'{result.p99_abs_torr:.6f}',
                    'mean_abs_torr': f'{result.mean_abs_torr:.6f}',
                    'p95_abs_torr': f'{result.p95_abs_torr:.6f}',
                    'max_abs_torr': f'{result.max_abs_torr:.6f}',
                    'n_validation': result.n_validation,
                    'passed': str(result.passed).lower(),
                }
            )


def _format_top(results: Sequence[CandidateResult], top_n: int) -> List[Dict[str, Any]]:
    return [_as_dict(item) for item in results[:top_n]]


def _unique_alpha_grid(alpha_grid_text: str) -> List[float]:
    values = []
    for chunk in alpha_grid_text.split(','):
        text = chunk.strip()
        if not text:
            continue
        values.append(float(text))
    if not values:
        values = [0.0]
    return sorted(set(max(0.0, min(1.0, v)) for v in values))


def _parse_sensor_list(sensor_arg: str) -> List[SensorKind]:
    text = sensor_arg.strip().lower()
    if text == 'both':
        return [SENSOR_TRANSDUCER, SENSOR_ALICAT]
    if text in {SENSOR_TRANSDUCER, SENSOR_ALICAT}:
        return [text]  # type: ignore[list-item]
    raise ValueError(f'Invalid --sensor {sensor_arg!r}; use transducer, alicat, or both')


def _min_segment_size_for_count(n: int, segment_count: int) -> int:
    """Relax segment minimum when dataset is small (e.g. sparse static holds)."""
    desired = max(5, n // (segment_count * 3))
    return min(20, desired)


def _prune_training_outliers(
    train: Sequence[CalibrationSample],
    *,
    sensor: SensorKind,
    reference: ReferenceKind,
    model: Dict[str, Any],
    max_residual_torr: float,
) -> List[CalibrationSample]:
    """Drop training samples with large corrected residual before a refit pass."""
    from app.services.pressure_calibration import apply_error_model, psi_to_torr

    kept: List[CalibrationSample] = []
    for sample in train:
        measured = sample.transducer_abs_psi if sensor == SENSOR_TRANSDUCER else sample.alicat_abs_psi
        ref = sample.mensor_abs_psia if reference == REFERENCE_MENSOR else sample.alicat_abs_psi
        if measured is None or ref is None:
            continue
        corrected = apply_error_model(float(measured), model)
        if psi_to_torr(abs(corrected - float(ref))) <= max_residual_torr:
            kept.append(sample)
    return kept


def _optimize_for_port_sensor(
    *,
    port_id: str,
    sensor: SensorKind,
    reference: ReferenceKind,
    samples: Sequence[CalibrationSample],
    tolerance_psi: float,
    static_only: bool,
    holdout_stride: int,
    alpha_grid: Sequence[float],
    pass_threshold_torr: float,
    min_near_target: int,
    pressure_axis: str = 'measured',
    robust_refit_torr: Optional[float] = None,
    segment_counts: Sequence[int] = (3, 5),
) -> Dict[str, Any]:
    selected = select_near_target_samples(
        samples,
        tolerance_psi=tolerance_psi,
        static_only=static_only,
        reference=reference,
    )
    if reference == REFERENCE_MENSOR:
        selected = [s for s in selected if s.mensor_abs_psia is not None]
    if len(selected) < min_near_target:
        raise ValueError(
            f'{port_id}/{sensor}: not enough near-target samples ({len(selected)}); '
            f'need >= {min_near_target}. Capture denser data or increase tolerance.'
        )
    train, validation = split_train_validation(selected, holdout_stride=holdout_stride)
    validation_index_set = {s.index for s in validation}
    validation_mask = [s.index in validation_index_set for s in selected]

    axis = pressure_axis if pressure_axis in ('measured', 'target') else 'measured'
    piecewise_models: List[tuple[Dict[str, Any], str, str]] = []
    for segment_count in segment_counts:
        if segment_count not in {3, 5}:
            continue
        min_seg = _min_segment_size_for_count(len(train), segment_count=segment_count)
        try:
            model = fit_piecewise_linear_error_model(
                train,
                segment_count=segment_count,
                min_segment_size=min_seg,
                sensor=sensor,
                reference=reference,
                pressure_axis=axis,  # type: ignore[arg-type]
            )
        except ValueError:
            continue
        piecewise_models.append(
            (model, f'piecewise{segment_count}_no_filter', f'piecewise{segment_count}_a0'),
        )
    quadratic = fit_quadratic_error_model(
        train,
        sensor=sensor,
        reference=reference,
        pressure_axis=axis,  # type: ignore[arg-type]
    )

    if robust_refit_torr is not None and piecewise_models:
        best_seed = piecewise_models[0][0]
        pruned = _prune_training_outliers(
            train,
            sensor=sensor,
            reference=reference,
            model=best_seed,
            max_residual_torr=float(robust_refit_torr),
        )
        if len(pruned) >= min_near_target // 2:
            train = pruned
            piecewise_models = []
            for segment_count in segment_counts:
                if segment_count not in {3, 5}:
                    continue
                min_seg = _min_segment_size_for_count(len(train), segment_count=segment_count)
                try:
                    model = fit_piecewise_linear_error_model(
                        train,
                        segment_count=segment_count,
                        min_segment_size=min_seg,
                        sensor=sensor,
                        reference=reference,
                        pressure_axis=axis,  # type: ignore[arg-type]
                    )
                except ValueError:
                    continue
                piecewise_models.append(
                    (model, f'piecewise{segment_count}_robust', f'piecewise{segment_count}_robust_a0'),
                )
            quadratic = fit_quadratic_error_model(
                train,
                sensor=sensor,
                reference=reference,
                pressure_axis=axis,  # type: ignore[arg-type]
            )

    candidates: List[CandidateResult] = []
    for model, family, name in piecewise_models:
        candidates.append(
            _score_candidate(
                port_id=port_id,
                sensor=sensor,
                reference=reference,
                family=family,
                candidate_name=name,
                model=model,
                alpha=0.0,
                samples=selected,
                validation_mask=validation_mask,
                pass_threshold_torr=pass_threshold_torr,
            )
        )
    for alpha in alpha_grid:
        for model, family, _ in piecewise_models:
            seg_name = family.replace('_no_filter', '').replace('_robust', '')
            candidates.append(
                _score_candidate(
                    port_id=port_id,
                    sensor=sensor,
                    reference=reference,
                    family='piecewise_plus_filter',
                    candidate_name=f'{seg_name}_a{alpha:.3f}',
                    model=model,
                    alpha=alpha,
                    samples=selected,
                    validation_mask=validation_mask,
                    pass_threshold_torr=pass_threshold_torr,
                )
            )
        candidates.append(
            _score_candidate(
                port_id=port_id,
                sensor=sensor,
                reference=reference,
                family='poly2_plus_filter',
                candidate_name=f'quadratic_a{alpha:.3f}',
                model=quadratic,
                alpha=alpha,
                samples=selected,
                validation_mask=validation_mask,
                pass_threshold_torr=pass_threshold_torr,
            )
        )

    ranked = _rank_results(candidates)
    return {
        'port_id': port_id,
        'sensor': sensor,
        'sample_counts': {
            'raw_total': len(samples),
            'near_target_total': len(selected),
            'near_target_train': len(train),
            'near_target_validation': len(validation),
        },
        'ranked': ranked,
        'best': ranked[0],
    }


def _build_config_snippet(
    best_by_port_sensor: Dict[str, Dict[str, CandidateResult]],
) -> Dict[str, Any]:
    alpha_values = {
        round(result.ema_alpha, 6)
        for per_sensor in best_by_port_sensor.values()
        for result in per_sensor.values()
    }
    common_alpha = float(next(iter(alpha_values))) if len(alpha_values) == 1 else 0.0

    labjack: Dict[str, Any] = {'pressure_filter_alpha': common_alpha}
    alicat_ports: Dict[str, Any] = {}

    for port_id, per_sensor in best_by_port_sensor.items():
        port_labjack: Dict[str, Any] = {}
        port_alicat: Dict[str, Any] = {}
        if SENSOR_TRANSDUCER in per_sensor:
            port_labjack['transducer_error_model'] = per_sensor[SENSOR_TRANSDUCER].model
        if SENSOR_ALICAT in per_sensor:
            port_alicat['alicat_error_model'] = per_sensor[SENSOR_ALICAT].model
        if port_labjack:
            labjack[port_id] = port_labjack
        if port_alicat:
            alicat_ports[port_id] = port_alicat

    hardware: Dict[str, Any] = {'labjack': labjack}
    if alicat_ports:
        hardware['alicat'] = alicat_ports
    return {'hardware': hardware}


def main() -> int:
    parser = argparse.ArgumentParser(description='Offline optimizer for pressure calibration models.')
    parser.add_argument('--input-csv', action='append', required=True, help='Alignment CSV path; repeat to add more files.')
    parser.add_argument('--ports', default='port_a,port_b', help='Comma-separated port ids (default: port_a,port_b).')
    parser.add_argument('--output-dir', required=True, help='Output directory for ranking/report files.')
    parser.add_argument('--reference', choices=[REFERENCE_ALICAT, REFERENCE_MENSOR], default=REFERENCE_MENSOR)
    parser.add_argument('--sensor', default='both', help='transducer, alicat, or both (default: both).')
    parser.add_argument('--near-target-tolerance-psi', type=float, default=0.2)
    parser.add_argument('--min-near-target-samples', type=int, default=50)
    parser.add_argument('--include-dynamic', action='store_true', help='Include dynamic phases in scoring.')
    parser.add_argument('--holdout-stride', type=int, default=5, help='Every Nth near-target sample goes to validation.')
    parser.add_argument(
        '--alpha-grid',
        default='0.0,0.05,0.1,0.2,0.3,0.4,0.6,0.8',
        help='Comma-separated EMA alpha values to evaluate.',
    )
    parser.add_argument('--pass-threshold-torr', type=float, default=1.0)
    parser.add_argument('--fit-min-psi', type=float, default=0.0)
    parser.add_argument('--fit-max-psi', type=float, default=20.0)
    parser.add_argument('--top-n', type=int, default=3)
    args = parser.parse_args()

    reference: ReferenceKind = args.reference  # type: ignore[assignment]
    sensors = _parse_sensor_list(args.sensor)

    started = time.time()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    input_paths = [Path(p) for p in args.input_csv]
    ports = [p.strip().lower() for p in args.ports.split(',') if p.strip()]
    alpha_grid = _unique_alpha_grid(args.alpha_grid)

    report: Dict[str, Any] = {
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'inputs': [str(p) for p in input_paths],
        'reference': reference,
        'sensors': sensors,
        'fit_band_psi': [args.fit_min_psi, args.fit_max_psi],
        'ports': {},
        'schema': {
            'required_columns': sorted(REQUIRED_ALIGNMENT_COLUMNS),
            'mensor_columns': ['mensor_abs_psia', 'mensor_abs_psi', 'mensor_psia'],
            'optional_preferred_columns': ['transducer_raw_abs_psi'],
            'near_target_rule': {
                'tolerance_psi': args.near_target_tolerance_psi,
                'static_only': not args.include_dynamic,
                'reference': reference,
            },
        },
        'ranking_rule': ['p99_abs_torr', 'mean_abs_torr', 'parameter_count', 'max_abs_torr'],
        'pass_threshold_torr': args.pass_threshold_torr,
    }

    best_by_port_sensor: Dict[str, Dict[str, CandidateResult]] = {}

    for port_id in ports:
        samples = _load_samples(input_paths, port_id)
        if reference == REFERENCE_MENSOR and not any(s.mensor_abs_psia is not None for s in samples):
            raise ValueError(f'{port_id}: no Mensor column data; add mensor_abs_psia to CSV.')
        samples = filter_samples_pressure_band(
            samples,
            min_psi=args.fit_min_psi,
            max_psi=args.fit_max_psi,
            reference=reference,
        )
        port_report: Dict[str, Any] = {'sensors': {}}
        best_by_port_sensor[port_id] = {}

        for sensor in sensors:
            result = _optimize_for_port_sensor(
                port_id=port_id,
                sensor=sensor,
                reference=reference,
                samples=samples,
                tolerance_psi=args.near_target_tolerance_psi,
                static_only=not args.include_dynamic,
                holdout_stride=args.holdout_stride,
                alpha_grid=alpha_grid,
                pass_threshold_torr=args.pass_threshold_torr,
                min_near_target=args.min_near_target_samples,
            )
            ranked = result['ranked']
            best: CandidateResult = result['best']
            best_by_port_sensor[port_id][sensor] = best

            csv_path = output_dir / f'ranking_{port_id}_{sensor}.csv'
            _write_ranking_csv(csv_path, ranked)
            port_report['sensors'][sensor] = {
                'sample_counts': result['sample_counts'],
                'best': _as_dict(best),
                'top': _format_top(ranked, args.top_n),
                'ranking_csv': str(csv_path),
            }

        report['ports'][port_id] = port_report

    config_snippet = _build_config_snippet(best_by_port_sensor)
    report['recommended_config_snippet'] = config_snippet
    report['elapsed_s'] = round(time.time() - started, 3)

    summary_json = output_dir / 'optimization_summary.json'
    summary_json.write_text(json.dumps(report, indent=2), encoding='utf-8')
    snippet_yaml = output_dir / 'recommended_calibration.yaml'
    snippet_yaml.write_text(yaml.safe_dump(config_snippet, sort_keys=False), encoding='utf-8')

    print(json.dumps(report, indent=2))
    print(f'\nWrote summary: {summary_json}')
    print(f'Wrote config snippet: {snippet_yaml}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
