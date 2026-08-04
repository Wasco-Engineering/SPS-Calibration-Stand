#!/usr/bin/env python3
"""Run TestExecutor (cycling + precision) on real hardware without the UI."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import load_config, setup_logging
from app.database.session import close_database, initialize_database
from app.hardware.port import PortId, PortManager, PortReading
from app.services.pressure_domain import resolve_barometric_psi
from app.services.ptp_service import derive_test_setup, load_ptp_from_db
from app.services.test_executor import TestExecutor

logger = logging.getLogger(__name__)


def _row_from_reading(ts: float, port_id: str, reading: Optional[PortReading]) -> dict[str, Any]:
    row = {
        'ts': ts,
        'port': port_id,
        'transducer_psi': reading.transducer.pressure if reading and reading.transducer else None,
        'transducer_ref': reading.transducer.pressure_reference if reading and reading.transducer else None,
        'alicat_psi': reading.alicat.pressure if reading and reading.alicat else None,
        'alicat_setpoint_psi': reading.alicat.setpoint if reading and reading.alicat else None,
        'barometric_psi': reading.alicat.barometric_pressure if reading and reading.alicat else None,
        'switch_no': bool(reading.switch.no_active) if reading and reading.switch else None,
        'switch_nc': bool(reading.switch.nc_active) if reading and reading.switch else None,
    }
    if reading and reading.dio:
        for dio in range(23):
            row[f'dio_{dio}'] = reading.dio.get(dio)
    return row


def run_headless_executor(
    config: dict[str, Any],
    part: str,
    sequence: str,
    port_id: str,
    sample_interval_ms: int,
    alicat_refresh_interval_ms: int,
    max_duration_s: float,
    out_dir: str,
    *,
    cycles_only: bool = False,
) -> int:
    if not initialize_database(config.get('database', {})):
        raise RuntimeError('Database initialization failed')

    params = load_ptp_from_db(part, sequence)
    if not params:
        raise RuntimeError(f'No PTP parameters for {part}/{sequence}')
    setup = derive_test_setup(part, sequence, params)

    logger.info(
        'Headless executor: part=%s seq=%s port=%s units=%s direction=%s reference=%s',
        setup.part_id,
        setup.sequence_id,
        port_id,
        setup.units_label,
        setup.activation_direction,
        setup.pressure_reference,
    )

    pm = PortManager(config)
    pm.initialize_ports()
    pm.connect_all()
    boot_baro = pm.capture_boot_barometric()
    pm.start_polling()

    port = pm.get_port(PortId(port_id))
    if port is None:
        raise RuntimeError(f'Port not available: {port_id}')
    if not port.configure_from_ptp(setup.raw):
        resolution = getattr(port, 'last_switch_resolution', None)
        details = '; '.join(getattr(resolution, 'errors', ()) or ())
        raise RuntimeError(f'PTP switch configuration failed for {port_id}: {details}')

    samples: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    lock = threading.Lock()
    stop = threading.Event()

    def add_event(name: str, **payload: Any) -> None:
        events.append({'ts': time.time(), 'event': name, 'payload': payload})
        logger.info('EVENT %s %s', name, payload)

    last_worker_alicat_s = {'value': 0.0}
    transducer_installed = bool(
        (config.get('hardware', {}).get('labjack', {}).get(port_id, {}) or {}).get(
            'transducer_installed', True
        )
    )

    def get_latest(_pid: str) -> Optional[PortReading]:
        # Alicat-only stands need a live serial refresh; cached read_fast alone
        # freezes pressure during cycle prep / edge waits.
        if not transducer_installed:
            now = time.perf_counter()
            if now - last_worker_alicat_s['value'] >= 0.05:
                port.refresh_alicat()
                last_worker_alicat_s['value'] = now
        return port.read_fast()

    last_baro: dict[str, Optional[float]] = {'value': boot_baro}

    def get_baro(_pid: str) -> float:
        if boot_baro is not None:
            return float(boot_baro)
        if not transducer_installed:
            port.refresh_alicat()
        reading = port.read_fast()
        labjack_cfg = config.get('hardware', {}).get('labjack', {})
        fallback = float(labjack_cfg.get('local_barometric_psi', 14.7))
        baro = resolve_barometric_psi(
            reading,
            fallback=fallback,
            last_value=last_baro['value'],
        )
        last_baro['value'] = baro
        return baro

    def sample_loop() -> None:
        interval_s = max(0.005, sample_interval_ms / 1000.0)
        alicat_interval_s = max(0.02, alicat_refresh_interval_ms / 1000.0)
        last_alicat_s = 0.0
        while not stop.is_set():
            now = time.time()
            if (
                not pm.is_alicat_background_polling()
                and now - last_alicat_s >= alicat_interval_s
            ):
                port.refresh_alicat()
                last_alicat_s = now
            row = _row_from_reading(now, port_id, port.read_fast())
            with lock:
                samples.append(row)
            time.sleep(interval_s)

    sampler = threading.Thread(target=sample_loop, daemon=True)
    sampler.start()

    executor_ref: list[TestExecutor] = []

    def on_cycling_complete() -> None:
        add_event('cycling_complete')
        if cycles_only and executor_ref:
            logger.info('cycles-only: stopping after cycling phase')
            executor_ref[0].request_cancel()

    executor = TestExecutor(
        port_id=port_id,
        port=port,
        test_setup=setup,
        config=config,
        get_latest_reading=get_latest,
        get_barometric_psi=get_baro,
        on_cycling_complete=on_cycling_complete,
        on_substate_update=lambda s: add_event('substate', state=s),
        on_edges_captured=lambda a, d: add_event(
            'edges_captured',
            activation_psi=a,
            deactivation_psi=d,
            pressure_source=(
                executor_ref[0].last_pressure_source_used if executor_ref else None
            ),
        ),
        on_cycle_estimate=lambda a, d, c: add_event(
            'cycle_estimate', activation_psi=a, deactivation_psi=d, count=c
        ),
        on_error=lambda m: add_event('error', message=m),
        on_cancelled=lambda: add_event('cancelled'),
    )
    executor_ref.append(executor)

    add_event('run_start', part=part, sequence=sequence, port=port_id)
    start = time.time()
    timed_out = False
    executor.start()
    while executor.is_running:
        if time.time() - start > max_duration_s:
            timed_out = True
            add_event('timeout', elapsed_s=time.time() - start)
            executor.request_cancel()
            break
        time.sleep(0.1)
    while executor.is_running:
        time.sleep(0.05)

    stop.set()
    sampler.join(timeout=1.0)
    duration = time.time() - start

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    base = f'headless_{part}_{sequence}_{port_id}_{stamp}'
    csv_path = out_path / f'{base}.csv'
    json_path = out_path / f'{base}.json'

    with lock:
        rows = list(samples)
    if rows:
        with csv_path.open('w', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    cycle_edges_ok = any(
        e.get('event') == 'cycle_estimate'
        and e.get('payload', {}).get('activation_psi') is not None
        and e.get('payload', {}).get('deactivation_psi') is not None
        for e in events
    )
    cycling_complete = any(e.get('event') == 'cycling_complete' for e in events)
    if cycles_only:
        success = not timed_out and cycle_edges_ok and cycling_complete
    else:
        success = (
            not timed_out
            and not any(e.get('event') == 'error' for e in events)
            and (
                any(e.get('event') == 'edges_captured' for e in events)
                or cycle_edges_ok
            )
        )
    summary = {
        'part': part,
        'sequence': sequence,
        'port': port_id,
        'duration_s': duration,
        'timed_out': timed_out,
        'success': success,
        'events': events,
        'sample_count': len(rows),
    }
    json_path.write_text(json.dumps(summary, indent=2), encoding='utf-8')

    print()
    print('=' * 70)
    print(f'Headless run {port_id}: success={success} duration={duration:.1f}s')
    for event in events:
        print(f"  - {event['event']}: {event.get('payload')}")
    print(f'  csv     : {csv_path}')
    print(f'  summary : {json_path}')
    print('=' * 70)

    pm.disconnect_all()
    close_database()
    return 0 if success else 1


def main() -> int:
    parser = argparse.ArgumentParser(description='Run cycling + precision TestExecutor headlessly.')
    parser.add_argument('--part', default='17029')
    parser.add_argument('--sequence', default='399')
    parser.add_argument('--port', choices=['port_a', 'port_b'], default='port_b')
    parser.add_argument('--both-ports', action='store_true', help='Run port_a then port_b sequentially.')
    parser.add_argument(
        '--port-b-part',
        default=None,
        help='Part id for port_b when using --both-ports (defaults to --part).',
    )
    parser.add_argument('--sample-interval-ms', type=int, default=20)
    parser.add_argument('--alicat-refresh-interval-ms', type=int, default=100)
    parser.add_argument('--max-duration-s', type=float, default=300.0)
    parser.add_argument('--num-cycles', type=int, default=1, help='Cycling repetitions (default 1 for validation).')
    parser.add_argument(
        '--fast-cycle-rate-psi-per-sec',
        type=float,
        default=None,
        help='Override fast cycling ramp rate for diagnostics.',
    )
    parser.add_argument(
        '--pre-approach-rate-multiplier',
        type=float,
        default=None,
        help='Override pre-approach multiplier applied to fast cycle rate.',
    )
    parser.add_argument(
        '--precision-sweep-rate-torr-per-sec',
        type=float,
        default=None,
        help='Override both normal and near-atmosphere precision sweep rates for diagnostics.',
    )
    parser.add_argument(
        '--cycles-only',
        action='store_true',
        help='Stop after cycling (skip precision); success requires act+deact cycle estimates.',
    )
    parser.add_argument('--out-dir', default='logs/headless_runs')
    parser.add_argument('--log-level', default='INFO')
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format='%(asctime)s %(levelname)s %(name)s - %(message)s',
    )
    config = load_config()
    config.setdefault('control', {}).setdefault('cycling', {})['num_cycles'] = max(1, args.num_cycles)
    ramp_cfg = config.setdefault('control', {}).setdefault('ramps', {})
    if args.fast_cycle_rate_psi_per_sec is not None:
        ramp_cfg['fast_cycle_rate_psi_per_sec'] = max(0.1, args.fast_cycle_rate_psi_per_sec)
    if args.pre_approach_rate_multiplier is not None:
        ramp_cfg['pre_approach_rate_multiplier'] = max(1.0, args.pre_approach_rate_multiplier)
    if args.precision_sweep_rate_torr_per_sec is not None:
        precision_rate = max(0.1, args.precision_sweep_rate_torr_per_sec)
        ramp_cfg['precision_sweep_rate_torr_per_sec'] = precision_rate
        ramp_cfg['low_pressure_precision_sweep_rate_torr_per_sec'] = precision_rate
    setup_logging(config)

    runs = [(args.port, args.part, args.sequence)]
    if args.both_ports:
        port_b_part = args.port_b_part or args.part
        runs = [
            ('port_a', args.part, args.sequence),
            ('port_b', port_b_part, args.sequence),
        ]

    exit_code = 0
    for port_id, part, sequence in runs:
        try:
            code = run_headless_executor(
                config=config,
                part=part,
                sequence=sequence,
                port_id=port_id,
                sample_interval_ms=args.sample_interval_ms,
                alicat_refresh_interval_ms=args.alicat_refresh_interval_ms,
                max_duration_s=args.max_duration_s,
                out_dir=args.out_dir,
                cycles_only=args.cycles_only,
            )
            exit_code = max(exit_code, code)
        except Exception as exc:
            logger.error('Headless run failed for %s: %s', port_id, exc, exc_info=True)
            exit_code = 1
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
