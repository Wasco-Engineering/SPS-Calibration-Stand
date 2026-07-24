#!/usr/bin/env python3
"""Compare both Alicat pressure controllers with a matched vacuum profile.

The optional valve-offset trial is applied with ``save=0`` and restored before
shutdown. The script always returns both ports to atmospheric routing with EXH
active through ``PortManager.disconnect_all``.
"""

from __future__ import annotations

import argparse
import csv
import logging
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import load_config, setup_logging
from app.hardware.port import Port, PortId, PortManager

logger = logging.getLogger(__name__)


def _parse_diagnostic(port: Port) -> Optional[tuple[float, float, float]]:
    """Return pressure PSI, valve drive %, and loop error PSI."""
    response = port.alicat._send_command('DV 1 2 13 133')
    if not response or response.strip() == '?':
        return None
    try:
        values = [float(value) for value in response.split()]
        if len(values) < 3:
            return None
        return (
            port.alicat._display_to_psi(values[0]),
            values[1],
            port.alicat._display_to_psi(values[2]),
        )
    except ValueError:
        return None


def _capture(
    port: Port,
    rows: list[dict[str, Any]],
    *,
    profile: str,
    phase: str,
    duration_s: float,
) -> None:
    started = time.perf_counter()
    while time.perf_counter() - started < duration_s:
        sample_started = time.perf_counter()
        diagnostic = _parse_diagnostic(port)
        transducer = port.daq.read_transducer()
        if diagnostic is not None:
            pressure, valve_drive, loop_error = diagnostic
            rows.append(
                {
                    'timestamp': time.time(),
                    'profile': profile,
                    'port': port.port_id.value,
                    'alicat_address': port.alicat.address,
                    'phase': phase,
                    'elapsed_s': time.perf_counter() - started,
                    'alicat_pressure_psi': pressure,
                    'valve_drive_pct': valve_drive,
                    'loop_error_psi': loop_error,
                    'transducer_pressure_psi': (
                        transducer.pressure if transducer is not None else None
                    ),
                }
            )
        remaining = 0.02 - (time.perf_counter() - sample_started)
        if remaining > 0:
            time.sleep(remaining)


def _wait_settled(port: Port, target_psi: float, timeout_s: float = 30.0) -> bool:
    deadline = time.perf_counter() + timeout_s
    stable_since: Optional[float] = None
    while time.perf_counter() < deadline:
        diagnostic = _parse_diagnostic(port)
        if diagnostic is None:
            stable_since = None
        elif abs(diagnostic[0] - target_psi) <= 0.03:
            if stable_since is None:
                stable_since = time.perf_counter()
            elif time.perf_counter() - stable_since >= 2.0:
                return True
        else:
            stable_since = None
        time.sleep(0.02)
    return False


def _run_profile(port: Port, profile: str, rows: list[dict[str, Any]]) -> None:
    logger.info('%s: preparing matched response profile %s', port.port_id.value, profile)
    if not port.prepare_vacuum_route_for_test():
        raise RuntimeError(f'{port.port_id.value}: failed to prepare vacuum route')
    if not port.set_ramp_rate(2.0) or not port.set_pressure(8.2):
        raise RuntimeError(f'{port.port_id.value}: failed to command 8.2 PSIA preparation')
    if not _wait_settled(port, 8.2):
        raise RuntimeError(f'{port.port_id.value}: did not settle at 8.2 PSIA')

    if not port.set_ramp_rate(0.0966839) or not port.set_pressure(7.0):
        raise RuntimeError(f'{port.port_id.value}: failed to start downward ramp')
    _capture(port, rows, profile=profile, phase='ramp_down', duration_s=15.5)
    _capture(port, rows, profile=profile, phase='hold_low', duration_s=2.0)

    if not port.set_pressure(10.0):
        raise RuntimeError(f'{port.port_id.value}: failed to start upward ramp')
    _capture(port, rows, profile=profile, phase='ramp_up', duration_s=33.5)
    _capture(port, rows, profile=profile, phase='hold_high', duration_s=2.0)
    port.vent_to_atmosphere()


def _fit(values: list[dict[str, Any]], phase: str) -> tuple[float, float, float]:
    selected = [row for row in values if row['phase'] == phase]
    if len(selected) < 3:
        return float('nan'), float('nan'), float('nan')
    times = [float(row['elapsed_s']) for row in selected]
    pressures = [float(row['alicat_pressure_psi']) for row in selected]
    mean_t = statistics.fmean(times)
    mean_p = statistics.fmean(pressures)
    denominator = sum((value - mean_t) ** 2 for value in times)
    slope = sum(
        (time_value - mean_t) * (pressure - mean_p)
        for time_value, pressure in zip(times, pressures)
    ) / denominator
    intercept = mean_p - slope * mean_t
    residuals = [
        pressure - (slope * time_value + intercept)
        for time_value, pressure in zip(times, pressures)
    ]
    errors = [abs(float(row['loop_error_psi'])) for row in selected]
    return slope, statistics.pstdev(residuals), statistics.fmean(errors)


def _write_outputs(output_dir: Path, rows: list[dict[str, Any]]) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / 'alicat_matched_response.csv'
    fields = list(rows[0]) if rows else []
    with csv_path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    report_path = output_dir / 'ALICAT_RESPONSE_REPORT.md'
    profiles = sorted({str(row['profile']) for row in rows})
    lines = [
        '# Alicat Matched Response Comparison',
        '',
        '| Profile | Phase | Slope (PSI/s) | Residual SD (PSI) | Mean abs loop error (PSI) |',
        '|---|---|---:|---:|---:|',
    ]
    for profile in profiles:
        selected = [row for row in rows if row['profile'] == profile]
        for phase in ('ramp_down', 'ramp_up'):
            slope, residual_sd, mean_error = _fit(selected, phase)
            lines.append(
                f'| {profile} | {phase} | {slope:.6f} | '
                f'{residual_sd:.6f} | {mean_error:.6f} |'
            )
    report_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return csv_path, report_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trial-b-offset', type=float, default=18.31)
    parser.add_argument('--no-offset-trial', action='store_true')
    parser.add_argument('--trial-b-p-gain', type=int)
    parser.add_argument('--b-only', action='store_true')
    parser.add_argument('--warmup-b', action='store_true')
    parser.add_argument('--confirm-restored', action='store_true')
    parser.add_argument('--output-dir')
    args = parser.parse_args()

    config = load_config()
    setup_logging(config)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_dir = Path(args.output_dir or PROJECT_ROOT / 'logs' / f'alicat_response_{stamp}')
    rows: list[dict[str, Any]] = []
    manager = PortManager(config)
    original_b_offset: Optional[float] = None
    original_b_gains: Optional[tuple[int, int, int]] = None

    try:
        manager.initialize_ports()
        if not manager.connect_all():
            raise RuntimeError('One or more ports failed to connect')
        port_a = manager.get_port(PortId.PORT_A)
        port_b = manager.get_port(PortId.PORT_B)
        if port_a is None or port_b is None:
            raise RuntimeError('Both ports are required')

        # Torr gives both controllers the same fine serial resolution. All
        # public pressure commands remain PSI and are converted internally.
        for port in (port_a, port_b):
            if not port.alicat.configure_units_from_ptp('13'):
                raise RuntimeError(f'{port.port_id.value}: failed to select Torr')

        response = port_b.alicat._send_command('LCVO')
        if response:
            numbers = [float(value) for value in response.split()[1:]]
            if numbers:
                original_b_offset = numbers[0]

        response = port_b.alicat._send_command('LCG')
        if response:
            numbers = [int(value) for value in response.split()[1:4]]
            if len(numbers) == 3:
                original_b_gains = (numbers[0], numbers[1], numbers[2])

        if args.warmup_b:
            _run_profile(port_b, 'B_warmup_original', rows)
        if not args.b_only:
            _run_profile(port_a, 'A_original_offset', rows)
        _run_profile(port_b, 'B_original_offset', rows)

        if not args.no_offset_trial:
            if original_b_offset is None:
                raise RuntimeError('Could not read B valve offset before trial')
            command = f'LCVO 0 0 {args.trial_b_offset:.2f} 0.00'
            response = port_b.alicat._send_command(command)
            if not response or response.strip() == '?':
                raise RuntimeError('B rejected volatile valve-offset trial')
            _run_profile(port_b, f'B_trial_offset_{args.trial_b_offset:.2f}', rows)

        if args.trial_b_p_gain is not None:
            if original_b_offset is None or original_b_gains is None:
                raise RuntimeError('Could not read B tuning before gain trial')
            port_b.alicat._send_command(f'LCVO 0 0 {original_b_offset:.2f} 0.00')
            _, i_gain, d_gain = original_b_gains
            command = f'LCG 0 0 {args.trial_b_p_gain} {i_gain} {d_gain}'
            response = port_b.alicat._send_command(command)
            if not response or response.strip() == '?':
                raise RuntimeError('B rejected volatile P-gain trial')
            _run_profile(port_b, f'B_trial_p_{args.trial_b_p_gain}', rows)
            if args.confirm_restored:
                p_gain, i_gain, d_gain = original_b_gains
                port_b.alicat._send_command(f'LCG 0 0 {p_gain} {i_gain} {d_gain}')
                _run_profile(port_b, 'B_restored_original', rows)
    finally:
        port_b = manager.get_port(PortId.PORT_B)
        if port_b is not None and original_b_offset is not None:
            port_b.alicat._send_command(f'LCVO 0 0 {original_b_offset:.2f} 0.00')
        if port_b is not None and original_b_gains is not None:
            p_gain, i_gain, d_gain = original_b_gains
            port_b.alicat._send_command(f'LCG 0 0 {p_gain} {i_gain} {d_gain}')
        for port_id in (PortId.PORT_A, PortId.PORT_B):
            port = manager.get_port(port_id)
            if port is not None:
                port.alicat.configure_units_from_ptp('1')
        manager.disconnect_all()

    csv_path, report_path = _write_outputs(output_dir, rows)
    print(f'CSV: {csv_path}')
    print(f'Report: {report_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
