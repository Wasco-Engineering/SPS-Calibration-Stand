"""Pressure sweeps with COM driven HIGH or LOW — watch for switch DIO edges."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import load_config
from app.hardware.alicat import AlicatController

try:
    from labjack import ljm
except ImportError as exc:
    raise SystemExit(str(exc)) from exc

PORT_A = set(range(0, 9))
PORT_B = set(range(9, 18))
COM_DIOS = (3, 12)


def label(dio: int) -> str:
    if dio in PORT_A:
        return f'PortA DIO{dio} (DB9-{dio + 1})'
    if dio in PORT_B:
        return f'PortB DIO{dio} (DB9-{dio - 8})'
    return f'DIO{dio}'


def read_dio(handle: int) -> dict[int, int]:
    state = int(ljm.eReadName(handle, 'DIO_STATE'))
    return {i: 1 if state & (1 << i) else 0 for i in range(20)}


def setup_com(handle: int, com_value: int, solenoid_dio: int, to_vacuum: bool) -> None:
    for com in COM_DIOS:
        for dio in range(20):
            if dio not in (*COM_DIOS, solenoid_dio, 18, 19):
                ljm.eReadName(handle, f'DIO{dio}')
        ljm.eWriteName(handle, f'DIO{com}', com_value)
    ljm.eWriteName(handle, f'DIO{solenoid_dio}', 1 if to_vacuum else 0)


def teardown_com(handle: int, solenoid_dio: int) -> None:
    ljm.eWriteName(handle, f'DIO{solenoid_dio}', 0)
    for com in COM_DIOS:
        ljm.eReadName(handle, f'DIO{com}')


def sweep(
    handle: int,
    config: dict,
    port_key: str,
    com_value: int,
    to_vacuum: bool,
    start_psi: float,
    end_psi: float,
) -> int:
    lj_port = config['hardware']['labjack'][port_key]
    al_base = config['hardware']['alicat']
    al_port = al_base[port_key]
    sol = lj_port['solenoid_dio']
    route = 'vacuum' if to_vacuum else 'atmosphere'
    com_label = 'HIGH' if com_value else 'LOW'

    print('\n' + '=' * 72)
    print(f'{port_key} | COM {com_label} | {route} | {start_psi} -> {end_psi} PSI')
    print('=' * 72)

    setup_com(handle, com_value, sol, to_vacuum)
    base = read_dio(handle)
    print('  DIO snapshot at start:', ' '.join(f'{d}={base[d]}' for d in range(18) if d not in (sol, 18, 19)))

    al = AlicatController(
        {
            'com_port': al_port['com_port'],
            'address': al_port['address'],
            'baudrate': al_base.get('baudrate', 19200),
            'timeout_s': 0.2,
            'auto_tare_on_connect': False,
            'auto_configure': False,
        }
    )
    if not al.connect():
        print(f'  Alicat failed: {al._last_status}')
        teardown_com(handle, sol)
        return 0

    edges = 0
    prev = base
    ignore = {*COM_DIOS, sol, 18, 19}

    def poll(phase: str, target: float) -> None:
        nonlocal edges, prev
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 120:
            st = al.read_status()
            p = st.pressure if st else 0.0
            state = read_dio(handle)
            for dio in range(18):
                if dio in ignore:
                    continue
                if state[dio] != prev[dio]:
                    edges += 1
                    print(
                        f'  [{phase}] {label(dio)} {prev[dio]}->{state[dio]} '
                        f'@ alicat={p:.2f} PSI'
                    )
            prev = state
            if st and abs(st.pressure - target) < 1.5:
                break
            time.sleep(0.015)

    try:
        al.cancel_hold()
        time.sleep(0.2)
        al.set_ramp_rate(0, time_unit='s')
        al.set_pressure(start_psi)
        time.sleep(5)
        al.set_ramp_rate(1.5, time_unit='s')
        al.set_pressure(end_psi)
        poll('DOWN', end_psi)
        al.set_pressure(start_psi)
        poll('UP', start_psi)
    finally:
        al.disconnect()
        teardown_com(handle, sol)

    print(f'  Total edges: {edges}')
    return edges


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--com', choices=('low', 'high', 'both'), default='both')
    args = parser.parse_args()

    config = load_config()
    handle = ljm.openS('T7', 'USB', 'ANY')
    total = 0
    com_values = []
    if args.com in ('low', 'both'):
        com_values.append(0)
    if args.com in ('high', 'both'):
        com_values.append(1)

    try:
        for com_val in com_values:
            for port in ('port_a', 'port_b'):
                total += sweep(handle, config, port, com_val, True, 14.7, 2.0)
                total += sweep(handle, config, port, com_val, False, 14.7, 30.0)
    finally:
        ljm.close(handle)

    print('\n' + '=' * 72)
    print(f'GRAND TOTAL DIO edges: {total}')
    if total == 0:
        print('Still no DIO edges — switch click is mechanical only on these lines,')
        print('or sense is on different pins than DIO0-17.')
    return 0 if total else 1


if __name__ == '__main__':
    raise SystemExit(main())
