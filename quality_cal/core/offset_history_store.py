"""Append-only versioned archive of Quality Cal error models (offset history)."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.core.paths import get_hostname, get_logs_dir, get_stand_id

logger = logging.getLogger(__name__)

OFFSET_HISTORY_DIRNAME = 'offset_history'
ENTRIES_DIRNAME = 'entries'
INDEX_FILENAME = 'index.jsonl'

_PORT_ID_RE = re.compile(r'^[A-Za-z0-9_\-]+$')


def get_quality_cal_logs_dir() -> Path:
    """Directory where Quality Cal dated artifacts are written."""
    path = get_logs_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_offset_history_dir() -> Path:
    """Root folder for versioned offset history."""
    path = get_quality_cal_logs_dir() / OFFSET_HISTORY_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_offset_history_entries_dir() -> Path:
    path = get_offset_history_dir() / ENTRIES_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def offset_history_index_path() -> Path:
    return get_offset_history_dir() / INDEX_FILENAME


def latest_offset_history_path(port_id: str) -> Path:
    return get_offset_history_dir() / f'latest_{port_id}.json'


def extract_port_models_from_stinger(
    config: dict[str, Any],
    port_id: str,
) -> dict[str, Any]:
    """Return current error models for ``port_id`` from a stinger config dict."""
    hardware = config.get('hardware', {}) if isinstance(config, dict) else {}
    if not isinstance(hardware, dict):
        hardware = {}
    labjack = hardware.get('labjack', {})
    alicat = hardware.get('alicat', {})
    if not isinstance(labjack, dict):
        labjack = {}
    if not isinstance(alicat, dict):
        alicat = {}
    lj_port = labjack.get(port_id, {})
    ali_port = alicat.get(port_id, {})
    if not isinstance(lj_port, dict):
        lj_port = {}
    if not isinstance(ali_port, dict):
        ali_port = {}
    return {
        'transducer_error_model': lj_port.get('transducer_error_model'),
        'alicat_error_model': ali_port.get('alicat_error_model'),
    }


def _sensor_fit_payload(fit_sensor: Any) -> Optional[dict[str, Any]]:
    if fit_sensor is None:
        return None
    return {
        'p99_abs_torr': float(fit_sensor.p99_abs_torr),
        'mean_abs_torr': float(fit_sensor.mean_abs_torr),
        'max_abs_torr': float(fit_sensor.max_abs_torr),
        'passed': bool(fit_sensor.passed),
        'ema_alpha': float(getattr(fit_sensor, 'ema_alpha', 0.0) or 0.0),
        'model': fit_sensor.model,
    }


def _new_models_from_fit(fit: Any) -> dict[str, Any]:
    transducer = fit.transducer.model if getattr(fit, 'transducer', None) is not None else None
    alicat = fit.alicat.model if getattr(fit, 'alicat', None) is not None else None
    return {
        'transducer_error_model': transducer,
        'alicat_error_model': alicat,
    }


def _safe_port_id(port_id: str) -> str:
    cleaned = str(port_id).strip()
    if not cleaned or not _PORT_ID_RE.match(cleaned):
        raise ValueError(f'Invalid port_id for offset history: {port_id!r}')
    return cleaned


def record_offset_history(
    *,
    port_id: str,
    fit: Any,
    previous_models: Optional[dict[str, Any]] = None,
    applied: bool,
    require_passed: bool = True,
    apply_skipped_reason: Optional[str] = None,
    profile_id: Optional[str] = None,
    profile_label: Optional[str] = None,
    stinger_config_path: Optional[Path | str] = None,
    sweep_csv_path: Optional[Path | str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> Path:
    """Write an immutable offset-history entry, append index, update latest.

    Returns the path to the dated entry JSON file.
    """
    port = _safe_port_id(port_id)
    now = datetime.now(timezone.utc)
    stamp = now.strftime('%Y%m%d_%H%M%S')
    entries_dir = get_offset_history_entries_dir()
    entry_path = entries_dir / f'{stamp}_{port}.json'
    # Avoid collisions if two writes land in the same second.
    if entry_path.exists():
        stamp = now.strftime('%Y%m%d_%H%M%S_%f')
        entry_path = entries_dir / f'{stamp}_{port}.json'

    sweep_str: Optional[str] = None
    if sweep_csv_path is not None:
        sweep_str = str(Path(sweep_csv_path))
    elif getattr(fit, 'sweep_csv_path', None) is not None:
        sweep_str = str(Path(fit.sweep_csv_path))

    stinger_str: Optional[str] = None
    if stinger_config_path is not None:
        stinger_str = str(Path(stinger_config_path))

    prev = previous_models if previous_models is not None else {
        'transducer_error_model': None,
        'alicat_error_model': None,
    }

    record: dict[str, Any] = {
        'recorded_at': now.isoformat(),
        'stand_id': get_stand_id(),
        'hostname': get_hostname(),
        'port_id': port,
        'profile_id': profile_id,
        'profile_label': profile_label,
        'stinger_config_path': stinger_str,
        'sweep_csv_path': sweep_str,
        'applied': bool(applied),
        'require_passed': bool(require_passed),
        'apply_skipped_reason': apply_skipped_reason,
        'fit_error_message': getattr(fit, 'error_message', None),
        'transducer_fit': _sensor_fit_payload(getattr(fit, 'transducer', None)),
        'alicat_fit': _sensor_fit_payload(getattr(fit, 'alicat', None)),
        'previous_models': {
            'transducer_error_model': prev.get('transducer_error_model'),
            'alicat_error_model': prev.get('alicat_error_model'),
        },
        'new_models': _new_models_from_fit(fit),
    }
    if extra:
        record['extra'] = dict(extra)

    entry_path.write_text(json.dumps(record, indent=2), encoding='utf-8')

    index_row = {
        'recorded_at': record['recorded_at'],
        'port_id': port,
        'applied': bool(applied),
        'transducer_p99_abs_torr': (
            record['transducer_fit']['p99_abs_torr'] if record['transducer_fit'] else None
        ),
        'alicat_p99_abs_torr': (
            record['alicat_fit']['p99_abs_torr'] if record['alicat_fit'] else None
        ),
        'entry_path': str(entry_path),
        'sweep_csv_path': sweep_str,
        'profile_id': profile_id,
    }
    index_path = offset_history_index_path()
    with index_path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(index_row) + '\n')

    latest_path = latest_offset_history_path(port)
    latest_path.write_text(json.dumps(record, indent=2), encoding='utf-8')

    logger.info(
        'Recorded offset history for %s applied=%s -> %s',
        port,
        applied,
        entry_path,
    )
    return entry_path


def list_offset_history(
    port_id: Optional[str] = None,
    *,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Return index rows newest-first, optionally filtered by port."""
    index_path = offset_history_index_path()
    if not index_path.exists():
        return []

    rows: list[dict[str, Any]] = []
    with index_path.open('r', encoding='utf-8') as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError:
                logger.warning('Skipping corrupt offset history index line')
                continue
            if not isinstance(row, dict):
                continue
            if port_id is not None and row.get('port_id') != port_id:
                continue
            rows.append(row)

    rows.reverse()  # file is append-only oldest→newest
    if limit is not None:
        rows = rows[: max(0, int(limit))]
    return rows
