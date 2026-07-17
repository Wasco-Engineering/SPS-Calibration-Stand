#!/usr/bin/env python3
"""Study how DUT line pressure drifts in candidate idle states (DUTs installed)."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_port_config, load_config
from app.hardware.alicat import AlicatController
from app.hardware.labjack import LabJackController

ATM = 14.7


def _fmt(value: Optional[float]) -> str:
    return f'{value:7.3f}' if value is not None else '    n/a'


def _read_dio(lj: LabJackController) -> Optional[int]:
    dio = lj.solenoid_dio
    if dio is None:
        return None
    try:
        from labjack import ljm

        handle = lj._shared_handle
        if handle is None:
            return None
        return int(ljm.eReadName(handle, f'DIO{dio}'))
    except Exception:
        return None


def _snapshot(al: AlicatController, lj: LabJackController) -> dict[str, Any]:
    reading = al.read_status()
    return {
        'p': reading.pressure if reading else None,
        'sp': reading.setpoint if reading else None,
        'raw': reading.raw_response if reading else None,
        'dio': _read_dio(lj),
    }


def _monitor(al: AlicatController, lj: LabJackController, seconds: float, label: str) -> list[dict[str, Any]]:
    print(f'\n  Monitor {label} ({seconds:.0f}s):', flush=True)
    samples: list[dict[str, Any]] = []
    start = time.perf_counter()
    while time.perf_counter() - start <= seconds:
        snap = _snapshot(al, lj)
        samples.append(snap)
        print(
            f"    t={time.perf_counter() - start:5.1f}s  DIO={snap['dio']}  "
            f"P={_fmt(snap['p'])}  SP={_fmt(snap['sp'])}  {snap['raw']!r}",
            flush=True,
        )
        time.sleep(2.0)
    return samples


def _bleed_to_atm(al: AlicatController, lj: LabJackController, target: float = ATM) -> None:
    print('  Bleed: DIO=1 test route, SP=14.7', flush=True)
    lj.set_solenoid(to_vacuum=True)
    time.sleep(0.5)
    al.cancel_hold()
    al.set_ramp_rate(8.0)
    al.set_pressure(target)
    for _ in range(30):
        time.sleep(1.0)
        snap = _snapshot(al, lj)
        print(f'    bleed t={_ + 1:2d}s P={_fmt(snap["p"])}', flush=True)
        if snap['p'] is not None and snap['p'] >= target - 1.0:
            break


def _apply_state(
    name: str,
    al: AlicatController,
    lj: LabJackController,
    setup: Callable[[AlicatController, LabJackController], None],
    monitor_s: float,
) -> None:
    print(f'\n{"=" * 72}\nSTATE: {name}\n{"=" * 72}', flush=True)
    setup(al, lj)
    time.sleep(1.0)
    snap = _snapshot(al, lj)
    print(
        f'  After setup: DIO={snap["dio"]} P={_fmt(snap["p"])} SP={_fmt(snap["sp"])} {snap["raw"]!r}',
        flush=True,
    )
    samples = _monitor(al, lj, monitor_s, name)
    if samples:
        p0 = samples[0]['p']
        p1 = samples[-1]['p']
        if p0 is not None and p1 is not None:
            print(f'  Delta P over monitor: {p1 - p0:+.3f} psia', flush=True)


def run_port(port_key: str, monitor_s: float) -> int:
    config = load_config()
    pc = get_port_config(config, port_key)
    lj_cfg = {**config['hardware']['labjack'], **pc['labjack']}
    al_cfg = {**config['hardware']['alicat'], **pc['alicat']}
    label = port_key.replace('_', ' ').title()

    print(f'\n{"#" * 72}\n# {label} idle atmosphere study\n{"#" * 72}', flush=True)

    lj = LabJackController(lj_cfg)
    if not lj.configure():
        print(f'ERROR: LabJack configure failed: {lj._last_status}', flush=True)
        return 1
    al = AlicatController({**al_cfg, 'auto_tare_on_connect': False, 'auto_configure': False})
    if not al.connect():
        print(f'ERROR: Alicat connect failed: {al._last_status}', flush=True)
        lj.cleanup()
        return 1

    try:
        print('\nBaseline (as-found):', flush=True)
        _monitor(al, lj, 4.0, 'baseline')

        _bleed_to_atm(al, lj)

        _apply_state(
            'A: DIO=0 + EXH (current software idle)',
            al,
            lj,
            lambda a, l: (l.set_solenoid(False), a.exhaust()),
            monitor_s,
        )
        _bleed_to_atm(al, lj)

        _apply_state(
            'B: DIO=0 + SP=14.7 closed-loop (no EXH)',
            al,
            lj,
            lambda a, l: (l.set_solenoid(False), a.cancel_hold(), a.set_pressure(ATM)),
            monitor_s,
        )
        _bleed_to_atm(al, lj)

        _apply_state(
            'C: DIO=1 + SP=14.7 closed-loop (test route)',
            al,
            lj,
            lambda a, l: (l.set_solenoid(True), a.cancel_hold(), a.set_pressure(ATM)),
            monitor_s,
        )
        _bleed_to_atm(al, lj)

        _apply_state(
            'D: DIO=1 bleed then DIO=0 + HP hold',
            al,
            lj,
            lambda a, l: (l.set_solenoid(False), a.cancel_hold(), a.hold_valve(False)),
            monitor_s,
        )
        _bleed_to_atm(al, lj)

        _apply_state(
            'E: DIO=1 + SP=14.7 + HC hold closed',
            al,
            lj,
            lambda a, l: (l.set_solenoid(True), a.cancel_hold(), a.set_pressure(ATM), a.hold_valve(True)),
            monitor_s,
        )
        _bleed_to_atm(al, lj)

        _apply_state(
            'F: DIO=0 passive only (no Alicat commands)',
            al,
            lj,
            lambda a, l: l.set_solenoid(False),
            monitor_s,
        )

        print('\nRestoring atmosphere idle hold before exit.', flush=True)
        lj.set_solenoid(False)
        al.cancel_hold()
        al.set_pressure(ATM)
        al.hold_valve(closed=True)
        return 0
    finally:
        try:
            lj.set_solenoid(False)
            al.exhaust()
        except Exception:
            pass
        al.disconnect()
        lj.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description='Idle atmosphere drift study with DUT installed.')
    parser.add_argument('--port', default='both', choices=('port_a', 'port_b', 'both'))
    parser.add_argument('--monitor-seconds', type=float, default=20.0)
    args = parser.parse_args()
    ports = ('port_a', 'port_b') if args.port == 'both' else (args.port,)
    rc = 0
    for port_key in ports:
        rc = max(rc, run_port(port_key, args.monitor_seconds))
        if len(ports) > 1:
            time.sleep(2.0)
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
