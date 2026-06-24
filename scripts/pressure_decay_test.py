#!/usr/bin/env python3
"""Pressurize both ports, prompt to disconnect supply, then log decay for 1 hour.

Usage:
    python scripts/pressure_decay_test.py
    python scripts/pressure_decay_test.py --target 100 --duration 3600 --interval 30
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import load_config
from app.hardware.port import PortManager
from quality_cal.core.hardware_helpers import (
    alicat_abs_psia,
    alicat_in_exhaust_mode,
    command_target_pressure,
    infer_barometric_psia,
    leave_alicat_exhaust,
    settle_tolerance_for_target,
    settle_timeout_for_target,
    transducer_abs_psia,
    wait_until_near_target,
)


def _connect_test_route(port) -> bool:
    """Connect DUT to Alicat line (DIO=1 on this stand)."""
    connect = getattr(port, 'connect_test_route', None)
    if callable(connect):
        return bool(connect())
    return bool(port.set_solenoid(to_vacuum=True))


def _read_port(port) -> dict[str, Optional[float]]:
    reading = port.read_all()
    baro = infer_barometric_psia(reading)
    return {
        'alicat_psia': alicat_abs_psia(reading, baro),
        'transducer_psia': transducer_abs_psia(reading, baro),
        'barometric_psia': baro,
    }


def _print_header() -> None:
    print(
        f'\n{"timestamp_utc":<26}  {"port_a_alicat":>12}  {"port_a_trans":>12}  '
        f'{"port_b_alicat":>12}  {"port_b_trans":>12}',
        flush=True,
    )
    print('-' * 78, flush=True)


def _print_row(ports, log_path: Optional[Path] = None) -> None:
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    row: dict[str, Optional[float]] = {}
    for port in ports:
        key = port.port_id.value
        vals = _read_port(port)
        row[f'{key}_alicat'] = vals.get('alicat_psia')
        row[f'{key}_trans'] = vals.get('transducer_psia')
        if log_path:
            with log_path.open('a', encoding='utf-8') as fh:
                fh.write(
                    f'{ts},{key},{vals.get("alicat_psia")},{vals.get("transducer_psia")}\n'
                )

    def cell(prefix: str, field: str) -> str:
        value = row.get(f'{prefix}_{field}')
        return f'{value:12.3f}' if value is not None else f'{"n/a":>12}'

    print(
        f'{ts:<26}  {cell("port_a", "alicat")}  {cell("port_a", "trans")}  '
        f'{cell("port_b", "alicat")}  {cell("port_b", "trans")}',
        flush=True,
    )


def _pressurize_ports(
    ports,
    *,
    target_psia: float,
    ramp_rate_psi_per_s: float,
    settle_tolerance_psia: float,
    settle_hold_s: float,
    settle_timeout_s: float,
    sample_hz: float,
    cancel_event: threading.Event,
) -> bool:
    print(
        f'\nStep 1: Connect test route (Alicat line) and ramp to {target_psia:.1f} PSIA...',
        flush=True,
    )
    for port in ports:
        if cancel_event.is_set():
            return False
        if alicat_in_exhaust_mode(port):
            leave_alicat_exhaust(port)
        if not _connect_test_route(port):
            print(f'ERROR: {port.port_id.value} failed to connect test route.', flush=True)
            return False
        print(f'  {port.port_id.value}: test route ON (DIO energized)', flush=True)

    for port in ports:
        command_target_pressure(
            port,
            target_psia=target_psia,
            ramp_rate_psi_per_s=ramp_rate_psi_per_s,
            configure_units=False,
        )

    tolerance = settle_tolerance_for_target(target_psia, settle_tolerance_psia)
    timeout_s = settle_timeout_for_target(target_psia, settle_timeout_s)

    print(
        f'\nStep 2: Waiting for stabilization '
        f'(tol={tolerance:.2f} psia, hold={settle_hold_s:.0f}s, timeout={timeout_s:.0f}s)...',
        flush=True,
    )

    for port in ports:
        key = port.port_id.value
        last_print = 0.0

        def progress(msg: str, alicat: Optional[float], transducer: Optional[float], _key=key) -> None:
            nonlocal last_print
            now = time.perf_counter()
            if now - last_print < 2.0:
                return
            last_print = now
            al = f'{alicat:.2f}' if alicat is not None else 'n/a'
            tr = f'{transducer:.2f}' if transducer is not None else 'n/a'
            print(f'  {_key}: {msg} | alicat={al} transducer={tr} psia', flush=True)

        try:
            stabilized = wait_until_near_target(
                port=port,
                target_psia=target_psia,
                tolerance_psia=tolerance,
                hold_s=settle_hold_s,
                timeout_s=timeout_s,
                sample_hz=sample_hz,
                cancel_event=cancel_event,
                progress_callback=progress,
                route='pressure',
            )
        except TimeoutError as exc:
            print(f'ERROR: {key} did not stabilize: {exc}', flush=True)
            return False

        print(
            f'  {key}: STABLE — alicat={stabilized.alicat_psia:.3f} psia, '
            f'transducer={stabilized.transducer_psia:.3f} psia '
            f'({stabilized.elapsed_s:.0f}s)',
            flush=True,
        )

    return True


def _countdown_disconnect(seconds: float) -> None:
    print('\n' + '=' * 72, flush=True)
    print('>>> CLOSE INLET SUPPLY VALVES NOW (isolate A and B) <<<', flush=True)
    print('    Shut off compressed-air inlet valves so each port traps its volume.', flush=True)
    print('    Do not vent — Alicat hold is engaged.', flush=True)
    print('=' * 72, flush=True)
    remaining = int(seconds)
    while remaining > 0:
        print(f'  Decay logging starts in {remaining:3d}s...', flush=True)
        time.sleep(1.0)
        remaining -= 1
    print('  Starting decay monitoring.\n', flush=True)


def run_test(args: argparse.Namespace) -> int:
    config = load_config()
    manager = PortManager(config)
    manager.initialize_ports()
    if not manager.connect_all():
        print('ERROR: Could not connect hardware.', flush=True)
        return 1

    ports = [manager.get_port(k) for k in ('port_a', 'port_b')]
    ports = [p for p in ports if p is not None]
    if not ports:
        print('ERROR: No ports available.', flush=True)
        manager.disconnect_all(restore_safe_state=True)
        return 1

    log_path: Optional[Path] = Path(args.log) if args.log else None
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if not log_path.exists():
            log_path.write_text('timestamp_utc,port,alicat_psia,transducer_psia\n', encoding='utf-8')

    cancel_event = threading.Event()
    restore_on_exit = bool(args.restore)
    decay_start = time.perf_counter()

    try:
        ok = _pressurize_ports(
            ports,
            target_psia=args.target,
            ramp_rate_psi_per_s=args.ramp_rate,
            settle_tolerance_psia=args.settle_tolerance,
            settle_hold_s=args.settle_hold,
            settle_timeout_s=args.settle_timeout,
            sample_hz=args.sample_hz,
            cancel_event=cancel_event,
        )
        if not ok:
            manager.disconnect_all(restore_safe_state=True)
            return 1

        for port in ports:
            try:
                port.alicat.hold_valve()
            except Exception:
                pass

        snapshot: dict[str, Any] = {}
        for port in ports:
            snapshot[port.port_id.value] = _read_port(port)
        print('\n--- PRESSURE STABILIZED ---', flush=True)
        for key, vals in snapshot.items():
            print(
                f'  {key}: alicat={vals.get("alicat_psia"):.3f} '
                f'transducer={vals.get("transducer_psia")} PSIA',
                flush=True,
            )

        _countdown_disconnect(args.disconnect_grace)

        print(
            f'Logging every {args.interval:.0f}s for {args.duration:.0f}s '
            f'({args.duration / 3600:.1f} hr). Ctrl+C to stop early.\n',
            flush=True,
        )
        _print_header()
        _print_row(ports, log_path)

        while True:
            time.sleep(max(1.0, args.interval))
            _print_row(ports, log_path)
            if time.perf_counter() - decay_start >= args.duration:
                break
    except KeyboardInterrupt:
        print('\nStopped early.', flush=True)
    finally:
        if restore_on_exit:
            print('\nRestoring atmosphere...', flush=True)
            for port in ports:
                port.vent_to_atmosphere()
                port.alicat.cancel_hold()
                port.set_pressure(0.2)
        # Keep solenoids on test route so trapped pressure can decay after supply disconnect.
        manager.disconnect_all(restore_safe_state=False if not restore_on_exit else True)

    if log_path:
        print(f'\nLog saved: {log_path}', flush=True)
    if not restore_on_exit:
        print('Left pressurized (Alicat hold). Use --restore to vent on exit.', flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Pressurize both ports, disconnect supply prompt, log pressure decay.',
    )
    parser.add_argument(
        '--target',
        type=float,
        default=100.0,
        help='Target absolute pressure in PSIA (default: 100).',
    )
    parser.add_argument(
        '--ramp-rate',
        type=float,
        default=8.0,
        help='Alicat ramp rate in PSI/s (default: 8).',
    )
    parser.add_argument('--settle-tolerance', type=float, default=0.25, help='Settle band PSIA.')
    parser.add_argument('--settle-hold', type=float, default=2.0, help='Seconds within band before stable.')
    parser.add_argument('--settle-timeout', type=float, default=180.0, help='Max seconds to reach target.')
    parser.add_argument('--sample-hz', type=float, default=4.0, help='Settle polling rate.')
    parser.add_argument(
        '--disconnect-grace',
        type=float,
        default=60.0,
        help='Seconds to disconnect supply after stable message (default: 60).',
    )
    parser.add_argument(
        '--duration',
        type=float,
        default=3600.0,
        help='Decay monitoring duration in seconds (default: 3600 = 1 hour).',
    )
    parser.add_argument(
        '--interval',
        type=float,
        default=30.0,
        help='Log interval in seconds (default: 30).',
    )
    parser.add_argument(
        '--log',
        default='logs/pressure_decay_hold.csv',
        help='CSV log path (default: logs/pressure_decay_hold.csv).',
    )
    parser.add_argument(
        '--restore',
        action='store_true',
        help='Vent to atmosphere when the test ends.',
    )
    return run_test(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
