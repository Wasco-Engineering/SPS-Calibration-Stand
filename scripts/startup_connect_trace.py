#!/usr/bin/env python3
"""Trace Stinger hardware activity during startup (mirrors app connect_all).

Run with no DUTs installed and ports already static at atmosphere. This script
logs every solenoid / Alicat command so we can see what disturbs the lines.

Examples:
  python scripts/startup_connect_trace.py
  python scripts/startup_connect_trace.py --skip-vent
  python scripts/startup_connect_trace.py --connect-only
  python scripts/startup_connect_trace.py --read-baseline
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_port_config, load_config
from app.hardware.alicat import AlicatController, AlicatReading
from app.hardware.labjack import LabJackController
from app.hardware.port import Port, PortId, PortManager, _IDLE_ATMOSPHERE_TOLERANCE_PSIA

ATM = 14.7


@dataclass
class CommandLog:
    entries: list[str] = field(default_factory=list)

    def record(self, port: str, action: str, detail: str = '') -> None:
        line = f'{time.strftime("%H:%M:%S")} [{port}] {action}'
        if detail:
            line += f' — {detail}'
        self.entries.append(line)
        print(line)


def _read_dio(lj: LabJackController) -> Optional[int]:
    dio = lj.solenoid_dio
    if dio is None:
        return None
    values = lj.read_dio_values(max_dio=22)
    if values is None:
        return None
    return int(values.get(dio, -1))


def _fmt_alicat(reading: Optional[AlicatReading]) -> str:
    if reading is None:
        return 'no reading'
    raw = (reading.raw_response or '').strip()
    exh = 'EXH' in raw.upper()
    hld = 'HLD' in raw.upper()
    flags = []
    if exh:
        flags.append('EXH')
    if hld:
        flags.append('HLD')
    flag_txt = f' [{",".join(flags)}]' if flags else ''
    return (
        f'P={reading.pressure:.3f} SP={reading.setpoint:.3f} '
        f'baro={reading.barometric_pressure!s}{flag_txt} raw={raw!r}'
    )


def _idle_assessment(
    reading: Optional[AlicatReading],
    baro: float,
) -> str:
    if reading is None or reading.pressure is None:
        return 'NOT IDLE (no pressure)'
    p = float(reading.pressure)
    low = baro - _IDLE_ATMOSPHERE_TOLERANCE_PSIA
    high = baro + 2.0
    raw = (reading.raw_response or '').upper()
    if 'EXH' in raw:
        return f'NOT IDLE (EXH mode, P={p:.3f})'
    if p < low:
        return f'NOT IDLE (low P={p:.3f} < {low:.3f}) -> would BLEED + command SP'
    if p > high:
        return f'NOT IDLE (high P={p:.3f} > {high:.3f})'
    return f'IDLE OK (P={p:.3f} in [{low:.3f}, {high:.3f}])'


def _wrap_port(port: Port, log: CommandLog) -> None:
    pid = port.port_id.value
    alicat = port.alicat
    daq = port.daq

    orig_set_solenoid = daq.set_solenoid

    def traced_set_solenoid(to_vacuum: bool) -> bool:
        log.record(pid, 'DIO solenoid', 'vacuum/test route (DIO=1)' if to_vacuum else 'atmosphere (DIO=0)')
        return orig_set_solenoid(to_vacuum)

    daq.set_solenoid = traced_set_solenoid  # type: ignore[method-assign]

    for name in ('set_solenoid_safe',):
        orig = getattr(daq, name)

        def make_safe(orig_fn: Callable[..., bool], label: str) -> Callable[..., bool]:
            def wrapped(*args: Any, **kwargs: Any) -> bool:
                log.record(pid, label)
                return orig_fn(*args, **kwargs)
            return wrapped

        setattr(daq, name, make_safe(orig, 'DIO solenoid safe (atmosphere)'))

    for cmd_name, label in (
        ('set_pressure', 'Alicat set_pressure'),
        ('cancel_hold', 'Alicat cancel_hold'),
        ('hold_valve', 'Alicat hold_valve'),
        ('exhaust', 'Alicat EXH (exhaust)'),
        ('tare', 'Alicat tare (PC)'),
    ):
        orig = getattr(alicat, cmd_name)

        def make_al(orig_fn: Callable[..., bool], action: str) -> Callable[..., bool]:
            def wrapped(*args: Any, **kwargs: Any) -> bool:
                detail = ''
                if args:
                    detail = str(args[0])
                if kwargs.get('closed') is not None:
                    detail = f'closed={kwargs["closed"]}'
                log.record(pid, action, detail)
                return orig_fn(*args, **kwargs)
            return wrapped

        setattr(alicat, cmd_name, make_al(orig, label))

    orig_connect_test = port.connect_test_route

    def traced_test_route() -> bool:
        log.record(pid, 'connect_test_route', 'DIO=1 for Alicat bleed/control')
        return orig_connect_test()

    port.connect_test_route = traced_test_route  # type: ignore[method-assign]


def read_baseline(config: dict[str, Any]) -> None:
    print('\n=== BASELINE (read-only, no app connect) ===')
    for port_key, address in (('port_a', 'A'), ('port_b', 'B')):
        lj_cfg = get_port_config(config, port_key)
        al_cfg = dict(config.get('hardware', {}).get('alicat', {}))
        al_cfg.update(config.get('hardware', {}).get('alicat', {}).get(port_key, {}))
        al_cfg['address'] = address
        al_cfg['auto_configure'] = False
        al_cfg['auto_tare_on_connect'] = False

        lj = LabJackController(lj_cfg)
        if not lj.configure():
            print(f'{port_key}: LabJack configure failed')
            continue
        dio = _read_dio(lj)
        print(f'{port_key}: DIO{lj.solenoid_dio}={dio} (0=atmosphere, 1=vacuum/test route)')

        al = AlicatController(al_cfg)
        if not al.connect():
            print(f'{port_key}: Alicat connect failed')
            lj.cleanup()
            continue
        reading = al.read_status()
        baro = float(reading.barometric_pressure) if reading and reading.barometric_pressure else ATM
        print(f'{port_key}: {_fmt_alicat(reading)}')
        print(f'{port_key}: {_idle_assessment(reading, baro)}')
        al.disconnect()
        lj.cleanup(preserve_solenoid_state=True)


def run_trace(*, skip_vent: bool, connect_only: bool) -> None:
    config = load_config()
    log = CommandLog()
    manager = PortManager(config)

    print('\n=== APP STARTUP TRACE ===')
    print('Close SPS Calibration Stand.exe before running this script.\n')

    if not manager.initialize_ports():
        print('Failed to initialize ports')
        return

    for port in manager.ports.values():
        _wrap_port(port, log)

    print('\n--- Step 1: connect (LabJack + Alicat post-config) ---')
    ok = manager.connect_all(safe_idle_on_connect=False)
    print(f'connect_all success={ok}')

    for port_id, port in manager.ports.items():
        reading = port.read_all().alicat
        baro = float(reading.barometric_pressure) if reading and reading.barometric_pressure else ATM
        print(f'\n{port_id.value} after connect:')
        print(f'  {_fmt_alicat(reading)}')
        print(f'  DIO={_read_dio(port.daq)}')
        print(f'  is_at_atmospheric_idle()={port.is_at_atmospheric_idle()}')
        print(f'  {_idle_assessment(reading, baro)}')

    if connect_only:
        print('\n--connect-only: skipping vent phase--')
    elif skip_vent:
        print('\n--skip-vent: would run safe_idle here but skipped--')
    else:
        print('\n--- Step 2: safe idle (same as app connect_all tail) ---')
        for port_id, port in manager.ports.items():
            port.refresh_alicat()
            if port.is_at_atmospheric_idle():
                print(f'{port_id.value}: skip vent (already at atmospheric idle)')
                continue
            print(f'{port_id.value}: calling vent_to_atmosphere()...')
            port.vent_to_atmosphere()

    print('\n--- Final state ---')
    for port_id, port in manager.ports.items():
        reading = port.read_all().alicat
        print(f'{port_id.value}: DIO={_read_dio(port.daq)}  {_fmt_alicat(reading)}')

    print('\n--- Command log (hardware writes only) ---')
    if not log.entries:
        print('(none — good for quiet startup)')
    else:
        for entry in log.entries:
            print(entry)

    manager.disconnect_all(restore_safe_state=False)


def main() -> None:
    parser = argparse.ArgumentParser(description='Trace Stinger startup hardware commands')
    parser.add_argument(
        '--read-baseline',
        action='store_true',
        help='Read DIO + Alicat only; do not run connect_all',
    )
    parser.add_argument(
        '--connect-only',
        action='store_true',
        help='Connect hardware but skip vent_to_atmosphere (isolate Alicat auto-config)',
    )
    parser.add_argument(
        '--skip-vent',
        action='store_true',
        help='Same as --connect-only',
    )
    args = parser.parse_args()
    config = load_config()

    if args.read_baseline:
        read_baseline(config)
        return

    run_trace(
        skip_vent=args.skip_vent or args.connect_only,
        connect_only=args.connect_only or args.skip_vent,
    )


if __name__ == '__main__':
    main()
