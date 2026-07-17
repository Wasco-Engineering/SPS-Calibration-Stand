#!/usr/bin/env python3
"""Verify Port A Alicat commands and solenoid (DIO19) routing.

Checks whether the DUT line is connected to the Alicat (test route) vs vented
(atmosphere route), and confirms setpoint / hold commands are acknowledged.

Usage:
    python scripts/port_pressure_route_diagnostic.py
    python scripts/port_pressure_route_diagnostic.py --port port_b
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_port_config, load_config
from app.hardware.alicat import AlicatController
from app.hardware.labjack import LabJackController


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
    except Exception as exc:
        print(f'  DIO read error: {exc}')
        return None


def _fmt(value: Optional[float]) -> str:
    return f'{value:.3f}' if value is not None else 'n/a'


def _snapshot(
    label: str,
    lj: LabJackController,
    al: AlicatController,
    *,
    route_name: str,
) -> dict[str, Any]:
    dio = lj.solenoid_dio
    dio_val = _read_dio(lj)
    trans = lj.read_transducer()
    trans_psi = trans.pressure if trans else None
    reading = al.read_status()
    print(f'\n--- {label} ({route_name}) ---', flush=True)
    print(f'  Solenoid DIO{dio}: {dio_val}  (0=atmosphere/safe, 1=test route)', flush=True)
    if reading:
        print(
            f'  Alicat {al.address}: pressure={_fmt(reading.pressure)}  '
            f'setpoint={_fmt(reading.setpoint)}  gauge={_fmt(reading.gauge_pressure)}',
            flush=True,
        )
        if reading.raw_response:
            print(f'  Raw: {reading.raw_response!r}', flush=True)
    else:
        print(f'  Alicat {al.address}: read failed ({al._last_status})', flush=True)
    print(f'  Transducer: {_fmt(trans_psi)} psia', flush=True)
    return {
        'dio': dio_val,
        'alicat_pressure': reading.pressure if reading else None,
        'alicat_setpoint': reading.setpoint if reading else None,
        'transducer': trans_psi,
        'raw': reading.raw_response if reading else None,
    }


def _send_check(al: AlicatController, name: str, fn) -> bool:
    ok = fn()
    print(f'  {name}: {"ACK" if ok else "FAIL"} ({al._last_status})', flush=True)
    return ok


def run_diagnostic(port_key: str, target_psia: float) -> int:
    config = load_config()
    pc = get_port_config(config, port_key)
    lj_cfg = {**config['hardware']['labjack'], **pc['labjack']}
    al_cfg = {**config['hardware']['alicat'], **pc['alicat']}
    solenoid_dio = lj_cfg.get('solenoid_dio')
    label = port_key.replace('_', ' ').title()

    print('=' * 72, flush=True)
    print(f'{label} pressure-route diagnostic', flush=True)
    print(f'  Alicat address: {al_cfg.get("address")} on {al_cfg.get("com_port")}', flush=True)
    print(f'  Solenoid DIO: {solenoid_dio}', flush=True)
    print(
        '\nOn this stand: DIO=1 is the ACTIVE TEST ROUTE (Alicat line). '
        'DIO=0 is ATMOSPHERE (vented, safe).',
        flush=True,
    )
    print('=' * 72, flush=True)

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
        print('\nStep 1: Baseline at atmosphere (safe)...', flush=True)
        lj.set_solenoid(to_vacuum=False)
        time.sleep(1.0)
        base = _snapshot('Baseline', lj, al, route_name='ATMOSPHERE')

        print('\nStep 2: Toggle to TEST ROUTE — listen for solenoid click...', flush=True)
        if not lj.set_solenoid(to_vacuum=True):
            print('ERROR: Failed to set test route.', flush=True)
            return 1
        time.sleep(1.5)
        test_idle = _snapshot('Test route idle', lj, al, route_name='TEST ROUTE')

        print(f'\nStep 3: Command setpoint {target_psia:.1f} PSIA...', flush=True)
        al.cancel_hold()
        ok_sp = _send_check(al, f'Setpoint S {target_psia:.1f}', lambda: al.set_pressure(target_psia))
        ok_ramp = _send_check(al, 'Ramp rate 5 PSI/s', lambda: al.set_ramp_rate(5.0))
        if not ok_sp:
            return 1

        print('  Ramping (15s)...', flush=True)
        for _ in range(15):
            time.sleep(1.0)
            reading = al.read_status()
            if reading:
                print(
                    f'    t={_:2d}s  P={_fmt(reading.pressure)}  SP={_fmt(reading.setpoint)}',
                    flush=True,
                )

        ramped = _snapshot('After ramp on TEST ROUTE', lj, al, route_name='TEST ROUTE')

        print('\nStep 4: Hold valve (HP)...', flush=True)
        ok_hold = _send_check(al, 'Hold position HP', al.hold_valve)
        time.sleep(2.0)
        held = _snapshot('After hold', lj, al, route_name='TEST ROUTE + HOLD')

        print('\nStep 5: Return to atmosphere and vent...', flush=True)
        al.cancel_hold()
        lj.set_solenoid(to_vacuum=False)
        al.set_pressure(0.2)
        time.sleep(2.0)
        _snapshot('Safe state', lj, al, route_name='ATMOSPHERE')

        print('\n' + '=' * 72, flush=True)
        print('INTERPRETATION', flush=True)
        print('=' * 72, flush=True)

        dio_changed = base.get('dio') != test_idle.get('dio')
        print(f'  Solenoid DIO changed atmosphere->test: {dio_changed}', flush=True)
        if not dio_changed:
            print('  >>> PROBLEM: DIO did not toggle — wiring or DIO pin may be wrong.', flush=True)

        sp_match = ramped.get('alicat_setpoint')
        if sp_match is not None and abs(sp_match - target_psia) < 1.0:
            print(f'  Setpoint readback OK (~{target_psia:.0f} PSIA).', flush=True)
        else:
            print(f'  >>> PROBLEM: Setpoint readback {sp_match} != commanded {target_psia}.', flush=True)

        p_test = ramped.get('alicat_pressure')
        if p_test is not None and p_test > 25.0:
            print(f'  Alicat pressure rose to {_fmt(p_test)} on TEST ROUTE — regulator path OK.', flush=True)
        else:
            print(
                f'  >>> Alicat stayed low ({_fmt(p_test)}) — check supply gas, compressor, '
                f'and that TEST ROUTE (DIO=1) is used for pressurization.',
                flush=True,
            )

        if not ok_hold:
            print('  >>> PROBLEM: Hold command (HP) was not acknowledged.', flush=True)
        elif held.get('alicat_pressure') is not None and p_test is not None:
            drift = abs(held['alicat_pressure'] - p_test)
            print(f'  Hold engaged; pressure drift after 2s = {drift:.3f} psia.', flush=True)

        print(
            '\nNOTE: The earlier decay test used ATMOSPHERE route (DIO=0) for pressurization. '
            'Production Stinger uses TEST ROUTE (DIO=1) via connect_test_route().',
            flush=True,
        )
        return 0
    finally:
        try:
            lj.set_solenoid(to_vacuum=False)
            al.cancel_hold()
            al.set_pressure(0.2)
        except Exception:
            pass
        al.disconnect()
        lj.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description='Port Alicat + solenoid routing diagnostic.')
    parser.add_argument('--port', default='port_a', choices=('port_a', 'port_b'))
    parser.add_argument('--target', type=float, default=50.0, help='Test setpoint PSIA (default 50).')
    return run_diagnostic(parser.parse_args().port, parser.parse_args().target)


if __name__ == '__main__':
    raise SystemExit(main())
