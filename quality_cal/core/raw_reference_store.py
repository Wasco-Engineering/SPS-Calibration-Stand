"""Persist latest Alicat / Mensor / transducer raw sweep data for future fits."""

from __future__ import annotations

import csv
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.core.paths import get_hostname, get_stand_id

logger = logging.getLogger(__name__)

RAW_REFERENCE_DIRNAME = 'raw_reference'


def get_quality_cal_logs_dir() -> Path:
    """Directory where Quality Cal dated sweep CSVs are written."""
    from quality_cal.config import get_default_config_path

    path = get_default_config_path().parent / 'logs'
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_raw_reference_dir() -> Path:
    """Stable folder for always-current per-port raw triplet references."""
    path = get_quality_cal_logs_dir() / RAW_REFERENCE_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def latest_raw_reference_csv(port_id: str) -> Path:
    return get_raw_reference_dir() / f'latest_{port_id}.csv'


def latest_raw_reference_manifest(port_id: str) -> Path:
    return get_raw_reference_dir() / f'latest_{port_id}.json'


def get_latest_raw_reference(port_id: str) -> Optional[Path]:
    """Return path to the latest published raw CSV for ``port_id``, if present."""
    path = latest_raw_reference_csv(port_id)
    return path if path.exists() else None


def _summarize_sweep_csv(csv_path: Path) -> dict[str, Any]:
    row_count = 0
    mensor_rows = 0
    raw_rows = 0
    targets: set[float] = set()
    with csv_path.open('r', newline='', encoding='utf-8') as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            row_count += 1
            if (row.get('mensor_abs_psia') or '').strip():
                mensor_rows += 1
            if (row.get('transducer_raw_abs_psi') or '').strip():
                raw_rows += 1
            target = (row.get('target_abs_psi') or '').strip()
            phase = (row.get('phase') or '').strip()
            if target and phase.startswith('static'):
                try:
                    targets.add(round(float(target), 3))
                except ValueError:
                    pass
    return {
        'row_count': row_count,
        'mensor_row_count': mensor_rows,
        'transducer_raw_row_count': raw_rows,
        'has_mensor': mensor_rows > 0,
        'has_transducer_raw': raw_rows > 0,
        'static_targets_psia': sorted(targets),
    }


def publish_latest_raw_reference(
    sweep_csv_path: Path | str,
    *,
    port_id: str,
    profile_id: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Copy a completed sweep CSV into the always-current raw reference slot.

    Keeps the original dated ``quality_cal_sweep_*.csv`` and also writes:

    - ``logs/raw_reference/latest_<port>.csv``
    - ``logs/raw_reference/latest_<port>.json`` (manifest + quick summary)
    """
    source = Path(sweep_csv_path)
    if not source.exists():
        raise FileNotFoundError(f'Sweep CSV not found: {source}')

    dest_csv = latest_raw_reference_csv(port_id)
    dest_json = latest_raw_reference_manifest(port_id)
    shutil.copy2(source, dest_csv)

    summary = _summarize_sweep_csv(dest_csv)
    manifest: dict[str, Any] = {
        'port_id': port_id,
        'updated_at': datetime.now(timezone.utc).isoformat(),
        'stand_id': get_stand_id(),
        'hostname': get_hostname(),
        'profile_id': profile_id,
        'source_csv': str(source.resolve()),
        'latest_csv': str(dest_csv.resolve()),
        'columns': [
            'timestamp',
            'port_id',
            'phase',
            'target_abs_psi',
            'transducer_abs_psi',
            'transducer_raw_abs_psi',
            'alicat_abs_psi',
            'mensor_abs_psia',
        ],
        **summary,
    }
    if extra:
        manifest['extra'] = dict(extra)
    dest_json.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    logger.info(
        'Published raw reference for %s -> %s (rows=%s mensor=%s)',
        port_id,
        dest_csv,
        summary['row_count'],
        summary['has_mensor'],
    )
    return manifest
