"""Tests for Quality Cal raw reference publishing."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from quality_cal.core.calibration_runner import SWEEP_CSV_COLUMNS
from quality_cal.core.raw_reference_store import (
    get_latest_raw_reference,
    latest_raw_reference_manifest,
    publish_latest_raw_reference,
)


def test_publish_latest_raw_reference(tmp_path: Path, monkeypatch) -> None:
    logs = tmp_path / 'logs'
    logs.mkdir()
    monkeypatch.setattr(
        'quality_cal.core.raw_reference_store.get_quality_cal_logs_dir',
        lambda: logs,
    )

    source = logs / 'quality_cal_sweep_port_b_test.csv'
    with source.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=SWEEP_CSV_COLUMNS)
        writer.writeheader()
        writer.writerow(
            {
                'timestamp': '1.0',
                'port_id': 'port_b',
                'phase': 'static_0.29',
                'target_abs_psi': '0.2900',
                'transducer_abs_psi': '0.3600',
                'transducer_raw_abs_psi': '0.3600',
                'alicat_abs_psi': '0.2950',
                'mensor_abs_psia': '0.2930',
            }
        )

    manifest = publish_latest_raw_reference(
        source,
        port_id='port_b',
        profile_id='mensor_0_30',
    )
    latest = get_latest_raw_reference('port_b')
    assert latest is not None
    assert latest.exists()
    assert latest.name == 'latest_port_b.csv'
    assert manifest['has_mensor'] is True
    assert manifest['static_targets_psia'] == [0.29]
    assert latest_raw_reference_manifest('port_b').exists()
    loaded = json.loads(latest_raw_reference_manifest('port_b').read_text(encoding='utf-8'))
    assert loaded['profile_id'] == 'mensor_0_30'
    assert loaded['row_count'] == 1
