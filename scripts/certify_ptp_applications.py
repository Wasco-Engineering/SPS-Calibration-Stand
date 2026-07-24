"""Certify Stinger can interpret PTP applications before a build or release.

This is a software gate, not a substitute for bench verification. It proves that
each selected PartID/SequenceID has complete PTP, resolves to observable switch
wiring on both ports, produces sane pressure visualization data, and has cycle
targets inside the configured hardware pressure range.
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import load_config
from app.database.models import ProductTestParameters
from app.database.session import close_database, get_engine, initialize_database, session_scope
from app.services.ptp_service import (
    UNITS_MAP,
    TestSetup,
    build_pressure_visualization,
    convert_pressure,
    derive_test_setup,
    load_ptp_from_db,
    load_ptp_from_dump,
    validate_ptp_params,
)
from app.services.ptp_switch_resolver import resolve_ptp_switch_config
from app.services.sweep_utils import (
    ptp_limits_use_psia_scale,
    resolve_cycle_ramp_targets,
    resolve_sweep_bounds,
    resolve_sweep_mode,
)


DEFAULT_OUTPUT = PROJECT_ROOT / 'logs' / 'application_certification.csv'
STATUS_PASS = 'PASS'
STATUS_BLOCKED_PTP = 'BLOCKED_PTP'
STATUS_BLOCKED_SWITCH = 'BLOCKED_SWITCH'
STATUS_FAIL_SOFTWARE = 'FAIL_SOFTWARE'
HEADERS = (
    'part_id',
    'sequence_id',
    'status',
    'category',
    'message',
    'ptp_source',
    'units_label',
    'pressure_reference',
    'target_activation_direction',
    'sweep_mode',
    'display_units',
    'port_a_derivation_mode',
    'port_b_derivation_mode',
)
UNIT_CODE_BY_LABEL = {label.upper(): code for code, label in UNITS_MAP.items()}


@dataclass(frozen=True)
class ApplicationInput:
    part_id: str
    sequence_id: str
    params: dict[str, str]
    source: str


def parse_application(value: str) -> tuple[str, str]:
    if ':' in value:
        part, sequence = value.split(':', 1)
    elif '/' in value:
        part, sequence = value.split('/', 1)
    else:
        raise argparse.ArgumentTypeError('Use PART:SEQUENCE, for example SPS01496-02:300')
    part = part.strip()
    sequence = normalize_sequence(sequence)
    if not part or not sequence:
        raise argparse.ArgumentTypeError('Application requires both part and sequence')
    return part, sequence


def normalize_sequence(sequence_id: Any) -> str:
    try:
        return str(int(str(sequence_id).strip()))
    except (TypeError, ValueError):
        return str(sequence_id or '').strip()


def certify_application(app: ApplicationInput, config: dict[str, Any]) -> dict[str, str]:
    row = {header: '' for header in HEADERS}
    row.update(
        {
            'part_id': app.part_id,
            'sequence_id': normalize_sequence(app.sequence_id),
            'ptp_source': app.source,
        }
    )

    if not app.params:
        return _blocked(row, STATUS_BLOCKED_PTP, 'missing_ptp', 'No PTP parameters found')

    is_valid, errors = validate_ptp_params(app.params)
    if not is_valid:
        category = _ptp_error_category(errors)
        return _blocked(row, STATUS_BLOCKED_PTP, category, '; '.join(errors))

    try:
        setup = derive_test_setup(app.part_id, app.sequence_id, app.params)
    except Exception as exc:
        return _blocked(row, STATUS_BLOCKED_PTP, 'derive_setup_failed', str(exc))

    row.update(
        {
            'units_label': str(setup.units_label or ''),
            'pressure_reference': str(setup.pressure_reference or ''),
            'target_activation_direction': str(setup.activation_direction or ''),
        }
    )

    switch_errors = _certify_switch_resolution(row, app.params, config)
    if switch_errors:
        return _blocked(
            row,
            STATUS_BLOCKED_SWITCH,
            'switch_resolution',
            '; '.join(switch_errors),
        )

    software_errors = []
    software_errors.extend(_certify_pressure_model(row, setup, config))
    software_errors.extend(_certify_cycle_targets(setup, config))
    if software_errors:
        return _blocked(
            row,
            STATUS_FAIL_SOFTWARE,
            'software_model',
            '; '.join(software_errors),
        )

    row.update(
        {
            'status': STATUS_PASS,
            'category': 'ok',
            'message': 'PTP, switch resolution, pressure display, and cycle targets are sane',
        }
    )
    return row


def _certify_switch_resolution(
    row: dict[str, str],
    params: dict[str, str],
    config: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    for port_id in ('port_a', 'port_b'):
        port_config = _port_config(config, port_id)
        resolution = resolve_ptp_switch_config(
            ptp_params=params,
            port_id=port_id,
            port_config=port_config,
        )
        row[f'{port_id}_derivation_mode'] = resolution.derivation_mode
        if not resolution.is_valid:
            errors.append(f'{port_id}: ' + '; '.join(resolution.errors))
    return errors


def _certify_pressure_model(
    row: dict[str, str],
    setup: TestSetup,
    config: dict[str, Any],
    barometric_psi: float = 14.7,
) -> list[str]:
    errors: list[str] = []
    display_units = _display_units_for_setup(setup, barometric_psi)
    row['display_units'] = display_units
    try:
        viz = build_pressure_visualization(
            setup,
            config.get('ui', {}),
            atmosphere_override=barometric_psi,
            display_units_override=display_units,
        )
    except Exception as exc:
        return [f'pressure visualization failed: {exc}']

    min_display = _finite(viz.get('min_psi'))
    max_display = _finite(viz.get('max_psi'))
    if min_display is None or max_display is None or min_display >= max_display:
        errors.append(
            f'invalid visualization scale min={viz.get("min_psi")} max={viz.get("max_psi")}'
        )

    for band_name in ('activation_band', 'deactivation_band'):
        band = viz.get(band_name)
        if band is None:
            continue
        try:
            low = float(band[0])
            high = float(band[1])
        except (TypeError, ValueError, IndexError):
            errors.append(f'{band_name} is not numeric: {band}')
            continue
        if not (math.isfinite(low) and math.isfinite(high) and low <= high):
            errors.append(f'{band_name} is invalid: {band}')
        if min_display is not None and max_display is not None:
            if low < min_display - 1e-6 or high > max_display + 1e-6:
                errors.append(f'{band_name} lies outside display scale: {band}')

    if (
        str(setup.pressure_reference or '').strip().lower() == 'gauge'
        and not ptp_limits_use_psia_scale(setup, {}, barometric_psi)
    ):
        atmosphere = _finite(viz.get('atmosphere_psi'))
        if atmosphere is None or abs(atmosphere) > 1e-6:
            errors.append(f'gauge visualization atmosphere must be 0, got {viz.get("atmosphere_psi")}')

    row['sweep_mode'] = resolve_sweep_mode(setup, atmosphere_psi=barometric_psi)
    return errors


def _certify_cycle_targets(
    setup: TestSetup,
    config: dict[str, Any],
    barometric_psi: float = 14.7,
) -> list[str]:
    errors: list[str] = []
    min_psi, max_psi = resolve_sweep_bounds(setup, {})
    if not (
        math.isfinite(min_psi)
        and math.isfinite(max_psi)
        and min_psi < max_psi
    ):
        return [f'invalid sweep bounds min={min_psi} max={max_psi}']

    direction = _activation_direction_value(setup.activation_direction)
    if direction is None:
        return [f'invalid activation direction {setup.activation_direction!r}']

    sweep_mode = resolve_sweep_mode(setup, atmosphere_psi=barometric_psi)
    overshoot_pct = _control_float(config, 'overshoot_beyond_limit_percent', 10.0)
    overshoot = max((max_psi - min_psi) * (overshoot_pct / 100.0), 0.5)
    for port_id in ('port_a', 'port_b'):
        hw_min, hw_max = _hardware_limits_test_reference(
            setup,
            _port_config(config, port_id),
            barometric_psi,
        )
        target_activation, target_deactivation = resolve_cycle_ramp_targets(
            sweep_mode=sweep_mode,
            activation_direction=direction,
            min_psi=min_psi,
            max_psi=max_psi,
            overshoot=overshoot,
            barometric_psi=barometric_psi,
            hw_min_psi=hw_min,
            hw_max_psi=hw_max,
            pressure_reference=setup.pressure_reference,
        )
        for label, value in (
            ('activation', target_activation),
            ('deactivation', target_deactivation),
        ):
            if not math.isfinite(value):
                errors.append(f'{port_id} {label} cycle target is not finite: {value}')
            elif value < hw_min - 1e-6 or value > hw_max + 1e-6:
                errors.append(
                    f'{port_id} {label} cycle target {value:.4f} outside hardware '
                    f'range {hw_min:.4f}..{hw_max:.4f}'
                )
        if direction > 0 and target_activation < target_deactivation:
            errors.append(
                f'{port_id} increasing cycle targets inverted: '
                f'activation={target_activation:.4f} deactivation={target_deactivation:.4f}'
            )
        if direction < 0 and target_activation > target_deactivation:
            errors.append(
                f'{port_id} decreasing cycle targets inverted: '
                f'activation={target_activation:.4f} deactivation={target_deactivation:.4f}'
            )
    return errors


def _display_units_for_setup(setup: TestSetup, barometric_psi: float) -> str:
    units_label = setup.units_label or 'PSI'
    pressure_ref = str(setup.pressure_reference or '').strip().lower()
    if ptp_limits_use_psia_scale(setup, {}, barometric_psi):
        return 'PSIA' if units_label.upper() == 'PSI' else units_label
    if units_label.upper() == 'PSI' and pressure_ref == 'gauge':
        return 'PSIG'
    if units_label.upper() == 'PSI' and pressure_ref == 'absolute':
        return 'PSIA'
    return units_label


def _hardware_limits_test_reference(
    setup: TestSetup,
    port_config: dict[str, Any],
    barometric_psi: float,
) -> tuple[float, float]:
    min_abs = float(port_config.get('transducer_pressure_min', 0.0))
    max_abs = float(port_config.get('transducer_pressure_max', 115.0))
    if ptp_limits_use_psia_scale(setup, {}, barometric_psi):
        return (min_abs, max_abs) if min_abs <= max_abs else (max_abs, min_abs)
    pressure_ref = str(setup.pressure_reference or '').strip().lower()
    if pressure_ref == 'gauge':
        min_ref = min_abs - barometric_psi
        max_ref = max_abs - barometric_psi
    else:
        min_ref = min_abs
        max_ref = max_abs
    return (min_ref, max_ref) if min_ref <= max_ref else (max_ref, min_ref)


def _port_config(config: dict[str, Any], port_id: str) -> dict[str, Any]:
    labjack = config.get('hardware', {}).get('labjack', {})
    base = {key: value for key, value in labjack.items() if key not in {'port_a', 'port_b'}}
    return {**base, **labjack.get(port_id, {})}


def _control_float(config: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(config.get('control', {}).get('edge_detection', {}).get(key, default))
    except (TypeError, ValueError):
        return default


def _activation_direction_value(direction: Optional[str]) -> Optional[int]:
    text = str(direction or '').strip().lower()
    if text.startswith('increas'):
        return 1
    if text.startswith('decreas'):
        return -1
    return None


def _finite(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _ptp_error_category(errors: list[str]) -> str:
    joined = '; '.join(errors)
    if 'Missing' in joined:
        return 'incomplete_ptp'
    if 'TargetActivationDirection' in joined:
        return 'invalid_activation_direction'
    if 'UnitsOfMeasure' in joined:
        return 'unsupported_units'
    return 'invalid_ptp'


def _blocked(
    row: dict[str, str],
    status: str,
    category: str,
    message: str,
) -> dict[str, str]:
    row.update({'status': status, 'category': category, 'message': message})
    return row


def read_matrix_inputs(path: Path) -> list[ApplicationInput]:
    with path.open('r', encoding='utf-8', newline='') as handle:
        reader = csv.DictReader(handle)
        return [
            ApplicationInput(
                part_id=str(row.get('part_id', '')).strip(),
                sequence_id=normalize_sequence(row.get('sequence_id', '')),
                params=_params_from_matrix_row(row),
                source=str(row.get('ptp_source', '') or 'matrix'),
            )
            for row in reader
            if str(row.get('part_id', '')).strip()
        ]


def _params_from_matrix_row(row: dict[str, str]) -> dict[str, str]:
    units_label = str(row.get('units_label', '') or '').strip()
    units_code = UNIT_CODE_BY_LABEL.get(units_label.upper(), units_label)
    return {
        'ActivationTarget': str(row.get('activation_target', '') or ''),
        'IncreasingLowerLimit': str(row.get('increasing_lower', '') or ''),
        'IncreasingUpperLimit': str(row.get('increasing_upper', '') or ''),
        'DecreasingLowerLimit': str(row.get('decreasing_lower', '') or ''),
        'DecreasingUpperLimit': str(row.get('decreasing_upper', '') or ''),
        'ResetBandLowerLimit': str(row.get('reset_lower', '') or ''),
        'ResetBandUpperLimit': str(row.get('reset_upper', '') or ''),
        'TargetActivationDirection': str(row.get('target_activation_direction', '') or ''),
        'UnitsOfMeasure': units_code,
        'PressureReference': str(row.get('pressure_reference', '') or ''),
        'CommonTerminal': str(row.get('common_terminal', '') or ''),
        'NormallyOpenTerminal': str(row.get('normally_open_terminal', '') or ''),
        'NormallyClosedTerminal': str(row.get('normally_closed_terminal', '') or ''),
    }


def load_application_inputs(
    applications: Iterable[tuple[str, str]],
) -> list[ApplicationInput]:
    inputs = []
    for part_id, sequence_id in sorted({(p.strip(), normalize_sequence(s)) for p, s in applications}):
        params = load_ptp_from_db(part_id, sequence_id) if get_engine() is not None else {}
        source = 'database' if params else 'missing'
        if not params:
            params = load_ptp_from_dump(part_id, sequence_id)
            source = 'dump' if params else 'missing'
        inputs.append(ApplicationInput(part_id, sequence_id, params, source))
    return inputs


def discover_sps_applications() -> list[tuple[str, str]]:
    if get_engine() is None:
        raise RuntimeError('Database is not initialized; --all-sps requires database access')
    applications: set[tuple[str, str]] = set()
    with session_scope() as session:
        records = (
            session.query(ProductTestParameters.PartID, ProductTestParameters.SequenceID)
            .filter(ProductTestParameters.PartID.like('SPS%'))
            .distinct()
            .all()
        )
        for part_id, sequence_id in records:
            part = str(part_id or '').strip()
            sequence = normalize_sequence(sequence_id)
            if part and sequence:
                applications.add((part, sequence))
    return sorted(applications)


def write_report(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(HEADERS), lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)


def print_summary(rows: list[dict[str, str]], output: Path) -> None:
    status_counts = Counter(row['status'] for row in rows)
    category_counts = Counter(row['category'] for row in rows if row['status'] != STATUS_PASS)
    print(f'Certified {len(rows)} applications -> {output}')
    print('Status:', ', '.join(f'{key}={value}' for key, value in sorted(status_counts.items())))
    if category_counts:
        print('Blocked/failure categories:', ', '.join(
            f'{key}={value}' for key, value in sorted(category_counts.items())
        ))
        print('First blocked/failing rows:')
        shown = 0
        for row in rows:
            if row['status'] == STATUS_PASS:
                continue
            print(
                f"  {row['part_id']}/{row['sequence_id']} "
                f"{row['status']} {row['category']}: {row['message']}"
            )
            shown += 1
            if shown >= 12:
                break


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--application',
        action='append',
        type=parse_application,
        help='Application to certify as PART:SEQUENCE. May be repeated.',
    )
    parser.add_argument('--all-sps', action='store_true', help='Certify every SPS application in PTP.')
    parser.add_argument(
        '--matrix',
        type=Path,
        help='Certify rows already present in an application verification matrix CSV.',
    )
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        '--fail-on-blocked',
        action='store_true',
        help='Return a non-zero exit code for blocked PTP/switch rows as well as software failures.',
    )
    args = parser.parse_args(argv)

    if not (args.application or args.all_sps or args.matrix):
        args.matrix = PROJECT_ROOT / 'docs' / 'application_verification_matrix.csv'

    config = load_config()
    db_initialized = False
    if args.all_sps or args.application:
        db_initialized = initialize_database(config.get('database', {}))
        if args.all_sps and not db_initialized:
            parser.error('--all-sps requires a database connection')

    try:
        inputs: list[ApplicationInput] = []
        if args.matrix:
            inputs.extend(read_matrix_inputs(args.matrix))
        applications = list(args.application or [])
        if args.all_sps:
            applications.extend(discover_sps_applications())
        if applications:
            inputs.extend(load_application_inputs(applications))
        rows = [certify_application(app, config) for app in inputs]
        write_report(args.output, rows)
        print_summary(rows, args.output)
    finally:
        if db_initialized:
            close_database()

    has_software_failure = any(row['status'] == STATUS_FAIL_SOFTWARE for row in rows)
    has_blocked = any(row['status'] != STATUS_PASS for row in rows)
    if has_software_failure or (args.fail_on_blocked and has_blocked):
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
