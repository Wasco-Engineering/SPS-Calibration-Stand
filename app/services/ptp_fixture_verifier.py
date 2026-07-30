"""Verify whether a stand's configured fixture can observe PTP switch wiring."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.database.models import ProductTestParameters
from app.database.session import get_engine, session_scope
from app.services.ptp_service import validate_ptp_params
from app.services.ptp_switch_resolver import PtpSwitchResolution, resolve_ptp_switch_config

PORT_IDS = ('port_a', 'port_b')
SPS_SEQUENCES = frozenset({'300', '600'})
TOP_LEVEL_SEQUENCE = '399'
PASS_DIRECT = 'PASS_DIRECT'
PASS_DERIVED = 'PASS_DERIVED'
WARN_FALLBACK = 'WARN_FALLBACK'
BLOCKED_SWITCH = 'BLOCKED_SWITCH'
BLOCKED_PTP = 'BLOCKED_PTP'
MISSING_PTP = 'MISSING_PTP'


def normalize_sequence(sequence_id: Any) -> str:
    """Return a stable string representation for a PTP sequence ID."""
    try:
        return str(int(str(sequence_id).strip()))
    except (TypeError, ValueError):
        return str(sequence_id or '').strip()


def discover_sps_applications(
    sequences: Iterable[str] = SPS_SEQUENCES,
) -> list[tuple[str, str]]:
    """Discover live SPS applications in the configured PTP database."""
    return _discover_applications(part_prefix='SPS%', sequences=sequences)


def discover_top_level_applications(
    sequence: str = TOP_LEVEL_SEQUENCE,
) -> list[tuple[str, str]]:
    """Discover live five-digit 17xxx final-test applications."""
    candidates = _discover_applications(part_prefix='17%', sequences=(sequence,))
    return [
        (part_id, sequence_id)
        for part_id, sequence_id in candidates
        if len(part_id) == 5 and part_id.isdigit() and part_id.startswith('17')
    ]


def verify_application_fixture(
    *,
    part_id: str,
    sequence_id: str,
    ptp_params: dict[str, str],
    config: dict[str, Any],
    source: str = 'live_database',
) -> list[dict[str, str]]:
    """Resolve both ports and return one evidence-rich verdict per port."""
    base_row = {
        'part_id': str(part_id).strip(),
        'sequence_id': normalize_sequence(sequence_id),
        'ptp_source': source,
    }
    if not ptp_params:
        return [
            _row(
                base_row,
                port_id,
                status=MISSING_PTP,
                category='missing_ptp',
                message='No PTP parameters returned by the live source',
            )
            for port_id in PORT_IDS
        ]

    is_valid, errors = validate_ptp_params(ptp_params)
    if not is_valid:
        message = '; '.join(errors)
        return [
            _row(
                base_row,
                port_id,
                status=BLOCKED_PTP,
                category='invalid_ptp',
                message=message,
            )
            for port_id in PORT_IDS
        ]

    rows: list[dict[str, str]] = []
    for port_id in PORT_IDS:
        resolution = resolve_ptp_switch_config(
            ptp_params=ptp_params,
            port_id=port_id,
            port_config=_port_config(config, port_id),
        )
        rows.append(_resolution_row(base_row, port_id, resolution))
    return rows


def has_blocked_verdict(rows: Iterable[dict[str, str]]) -> bool:
    """Return whether any verifier row prevents a production mapping."""
    return any(
        row.get('status') in {MISSING_PTP, BLOCKED_PTP, BLOCKED_SWITCH}
        for row in rows
    )


def _discover_applications(
    *,
    part_prefix: str,
    sequences: Iterable[str],
) -> list[tuple[str, str]]:
    if get_engine() is None:
        raise RuntimeError('Database is not initialized')
    wanted_sequences = {normalize_sequence(sequence) for sequence in sequences}
    applications: set[tuple[str, str]] = set()
    with session_scope() as session:
        records = (
            session.query(ProductTestParameters.PartID, ProductTestParameters.SequenceID)
            .filter(ProductTestParameters.PartID.like(part_prefix))
            .distinct()
            .all()
        )
    for raw_part_id, raw_sequence_id in records:
        part_id = str(raw_part_id or '').strip()
        sequence_id = normalize_sequence(raw_sequence_id)
        if part_id and sequence_id in wanted_sequences:
            applications.add((part_id, sequence_id))
    return sorted(applications)


def _port_config(config: dict[str, Any], port_id: str) -> dict[str, Any]:
    labjack = config.get('hardware', {}).get('labjack', {})
    if not isinstance(labjack, dict):
        return {}
    shared = {key: value for key, value in labjack.items() if key not in PORT_IDS}
    port_config = labjack.get(port_id, {})
    return {**shared, **port_config} if isinstance(port_config, dict) else shared


def _resolution_row(
    base_row: dict[str, str],
    port_id: str,
    resolution: PtpSwitchResolution,
) -> dict[str, str]:
    if not resolution.is_valid:
        return _row(
            base_row,
            port_id,
            status=BLOCKED_SWITCH,
            category='terminals_not_observable',
            message='; '.join(resolution.errors),
            resolution=resolution,
        )
    if resolution.warnings:
        status = WARN_FALLBACK
        category = 'observable_with_fallback'
    elif resolution.derivation_mode == 'direct':
        status = PASS_DIRECT
        category = 'observable_direct'
    else:
        status = PASS_DERIVED
        category = 'observable_derived'
    return _row(
        base_row,
        port_id,
        status=status,
        category=category,
        message=resolution.summary,
        resolution=resolution,
    )


def _row(
    base_row: dict[str, str],
    port_id: str,
    *,
    status: str,
    category: str,
    message: str,
    resolution: PtpSwitchResolution | None = None,
) -> dict[str, str]:
    row = {
        **base_row,
        'port_id': port_id,
        'status': status,
        'category': category,
        'message': message,
        'derivation_mode': resolution.derivation_mode if resolution else '',
        'drive_role': resolution.drive_role if resolution else '',
        'drive_dio': _optional_int(resolution.drive_dio) if resolution else '',
        'no_dio': _optional_int(resolution.no_dio) if resolution else '',
        'nc_dio': _optional_int(resolution.nc_dio) if resolution else '',
        'sensed_db9_pins': (
            ','.join(str(pin) for pin in resolution.sensed_db9_pins) if resolution else ''
        ),
        'warnings': '; '.join(resolution.warnings) if resolution else '',
    }
    return row


def _optional_int(value: int | None) -> str:
    return '' if value is None else str(value)
