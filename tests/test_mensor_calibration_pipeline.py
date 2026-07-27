"""End-to-end tests for Mensor-referenced calibration optimizer pipeline."""

from __future__ import annotations

import csv
import tempfile
from pathlib import Path

import pytest

from scripts.optimize_pressure_calibration import _load_samples, _optimize_for_port_sensor
from scripts.triplet_to_alignment_csv import convert


def _write_synthetic_mensor_csv(path: Path, port_id: str = 'port_b', n_targets: int = 21) -> None:
    with path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                'timestamp',
                'port_id',
                'phase',
                'target_abs_psi',
                'transducer_abs_psi',
                'transducer_raw_abs_psi',
                'alicat_abs_psi',
                'mensor_abs_psia',
            ],
        )
        writer.writeheader()
        idx = 0
        for target in range(n_targets):
            mensor = float(target)
            t_err = 0.008 * mensor + 0.04
            a_err = 0.005 * mensor + 0.02
            for _ in range(8):
                writer.writerow(
                    {
                        'timestamp': str(idx),
                        'port_id': port_id,
                        'phase': f'static_{target}',
                        'target_abs_psi': mensor,
                        'transducer_abs_psi': mensor + t_err,
                        'transducer_raw_abs_psi': mensor + t_err,
                        'alicat_abs_psi': mensor + a_err,
                        'mensor_abs_psia': mensor,
                    }
                )
                idx += 1


def test_triplet_to_alignment_conversion() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        triplet = Path(tmp) / 'triplet.csv'
        with triplet.open('w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=['target_psia', 'mensor_psia', 'alicat_psia', 'transducer_psia'],
            )
            writer.writeheader()
            writer.writerow(
                {'target_psia': '10', 'mensor_psia': '10.0', 'alicat_psia': '10.1', 'transducer_psia': '10.2'}
            )
        out = Path(tmp) / 'alignment.csv'
        convert(triplet, out, 'port_b')
        rows = list(csv.DictReader(out.open(encoding='utf-8')))
        assert rows[0]['mensor_abs_psia'] == '10.0'
        assert rows[0]['phase'] == 'static_10'


def test_load_samples_allows_missing_transducer() -> None:
    """Open-fitting / SPS stands omit transducer columns; Alicat+Mensor must still load."""
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / 'alignment.csv'
        with csv_path.open('w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    'timestamp',
                    'port_id',
                    'phase',
                    'target_abs_psi',
                    'transducer_abs_psi',
                    'transducer_raw_abs_psi',
                    'alicat_abs_psi',
                    'mensor_abs_psia',
                ],
            )
            writer.writeheader()
            for i in range(5):
                writer.writerow(
                    {
                        'timestamp': str(i),
                        'port_id': 'port_a',
                        'phase': 'static_8',
                        'target_abs_psi': '8.0',
                        'transducer_abs_psi': '',
                        'transducer_raw_abs_psi': '',
                        'alicat_abs_psi': '7.97',
                        'mensor_abs_psia': '8.00',
                    }
                )
        samples = _load_samples([csv_path], 'port_a')
        assert len(samples) == 5
        assert all(s.transducer_abs_psi is None for s in samples)
        assert all(s.alicat_abs_psi == pytest.approx(7.97) for s in samples)
        assert all(s.mensor_abs_psia == pytest.approx(8.0) for s in samples)


def test_mensor_optimizer_passes_1_torr_on_synthetic_data() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / 'alignment.csv'
        _write_synthetic_mensor_csv(csv_path)
        samples = _load_samples([csv_path], 'port_b')
        for sensor in ('transducer', 'alicat'):
            result = _optimize_for_port_sensor(
                port_id='port_b',
                sensor=sensor,  # type: ignore[arg-type]
                reference='mensor',
                samples=samples,
                tolerance_psi=0.5,
                static_only=True,
                holdout_stride=5,
                alpha_grid=[0.0],
                pass_threshold_torr=1.0,
                min_near_target=30,
            )
            best = result['best']
            assert best.passed, f'{sensor} p99={best.p99_abs_torr} Torr'
