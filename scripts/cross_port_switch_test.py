"""Detect swapped switch wiring by sweeping one Alicat while watching both DB9 DIO banks.

If Port A pressure sweep toggles Port B DB9 lines (DIO9-17), switches are likely swapped.

Usage:
    python scripts/cross_port_switch_test.py
    python scripts/cross_port_switch_test.py --vacuum-only
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import load_config
from app.hardware.alicat import AlicatController

try:
    from labjack import ljm
except ImportError as exc:
    raise SystemExit(f'labjack.ljm required: {exc}') from exc

PORT_A_DB9 = {p: p - 1 for p in range(1, 10)}
PORT_B_DB9 = {p: p + 8 for p in range(1, 10)}
PORT_A_DIOS = set(PORT_A_DB9.values())
PORT_B_DIOS = set(PORT_B_DB9.values())
ALL_DIO = list(range(20))
SWITCH_DIOS = sorted(PORT_A_DIOS | PORT_B_DIOS)


def label(dio: int) -> str:
    for pin, d in PORT_A_DB9.items():
        if d == dio:
            return f'PortA DB9-{pin} DIO{dio}'
    for pin, d in PORT_B_DB9.items():
        if d == dio:
            return f'PortB DB9-{pin} DIO{dio}'
    return f'DIO{dio}'


def bank(dio: int) -> str:
    if dio in PORT_A_DIOS:
        return 'port_a'
    if dio in PORT_B_DIOS:
        return 'port_b'
    return 'other'


def read_dio(handle: int) -> dict[int, int]:
    state = int(ljm.eReadName(handle, 'DIO_STATE'))
    return {i: 1 if state & (1 << i) else 0 for i in ALL_DIO}


def drive_com(handle: int, com_dios: list[int], value: int = 0) -> None:
    for com in com_dios:
        for dio in ALL_DIO:
            if dio not in (com, 18, 19):
                ljm.eReadName(handle, f'DIO{dio}')
        ljm.eWriteName(handle, f'DIO{com}', value)


def restore_com_inputs(handle: int, com_dios: list[int]) -> None:
    for com in com_dios:
        ljm.eReadName(handle, f'DIO{com}')


def run_sweep(
    handle: int,
    *,
    active_port: str,
    config: dict,
    start_psi: float,
    end_psi: float,
    rate_psi_s: float,
    to_vacuum: bool,
    solenoid_mode: str,
    com_mode: str = 'both',
    alicat_address: str | None = None,
) -> list[tuple[int, float, float, int, int, str]]:
    """Return edge list: (dio, elapsed, alicat_psi, old, new, active_port)."""
    lj = config['hardware']['labjack']
    active_cfg = lj[active_port]
    inactive_port = 'port_b' if active_port == 'port_a' else 'port_a'
    inactive_cfg = lj[inactive_port]

    sol_a = active_cfg.get('solenoid_dio') if active_port == 'port_a' else inactive_cfg.get('solenoid_dio')
    sol_b = inactive_cfg.get('solenoid_dio') if active_port == 'port_a' else active_cfg.get('solenoid_dio')

    # PTP 17022 COM on DB9 pin 4
    com_a, com_b = 3, 12
    if com_mode == 'port_a':
        com_dios = [com_a]
    elif com_mode == 'port_b':
        com_dios = [com_b]
    else:
        com_dios = [com_a, com_b]

    al_base = config['hardware']['alicat']
    al_port = al_base[active_port]
    address = alicat_address or al_port['address']
    al_cfg = {
        'com_port': al_port['com_port'],
        'address': address,
        'baudrate': al_base.get('baudrate', 19200),
        'timeout_s': 0.2,
        'auto_tare_on_connect': False,
        'auto_configure': False,
    }

    route = 'vacuum' if to_vacuum else 'atmosphere'
    print('\n' + '=' * 72)
    print(f'ACTIVE ALICAT: {active_port} ({al_port["address"]} on {al_port["com_port"]})')
    print(f'  Sweep {start_psi:.1f} -> {end_psi:.1f} PSI @ {rate_psi_s:.1f} PSI/s ({route})')
    print(f'  Solenoid mode: {solenoid_mode}')
    print(f'  COM mode: {com_mode}  Alicat address: {address}')
    print('  Watching Port A DIO0-8 and Port B DIO9-17 for edges')
    print('=' * 72)

    drive_com(handle, com_dios, 0)

    sol_val = 1 if to_vacuum else 0
    if solenoid_mode == 'active_port':
        if active_port == 'port_a' and sol_a is not None:
            ljm.eWriteName(handle, f'DIO{sol_a}', sol_val)
        if active_port == 'port_b' and sol_b is not None:
            ljm.eWriteName(handle, f'DIO{sol_b}', sol_val)
    elif solenoid_mode == 'both':
        if sol_a is not None:
            ljm.eWriteName(handle, f'DIO{sol_a}', sol_val)
        if sol_b is not None:
            ljm.eWriteName(handle, f'DIO{sol_b}', sol_val)
    elif solenoid_mode == 'swapped':
        # Route vacuum/atmosphere on the *other* port's solenoid
        if active_port == 'port_a' and sol_b is not None:
            ljm.eWriteName(handle, f'DIO{sol_b}', sol_val)
        if active_port == 'port_b' and sol_a is not None:
            ljm.eWriteName(handle, f'DIO{sol_a}', sol_val)

    al = AlicatController(al_cfg)
    if not al.connect():
        print(f'  Alicat connect failed: {al._last_status}')
        return []

    edges: list[tuple[int, float, float, int, int, str]] = []
    prev = read_dio(handle)

    try:
        al.cancel_hold()
        time.sleep(0.2)
        al.set_ramp_rate(0, time_unit='s')
        al.set_pressure(start_psi)
        time.sleep(5)
        al.set_ramp_rate(rate_psi_s, time_unit='s')
        al.set_pressure(end_psi)

        t0 = time.perf_counter()
        timeout = abs(start_psi - end_psi) / max(rate_psi_s, 0.1) + 90
        last_p = start_psi

        while time.perf_counter() - t0 < timeout:
            st = al.read_status()
            if st:
                last_p = st.pressure
            state = read_dio(handle)
            elapsed = time.perf_counter() - t0
            for dio in SWITCH_DIOS:
                if state[dio] != prev[dio]:
                    edges.append((dio, elapsed, last_p, prev[dio], state[dio], active_port))
                    print(
                        f'  [{elapsed:6.2f}s] EDGE {label(dio)} ({bank(dio)}) '
                        f'{prev[dio]}->{state[dio]} while sweeping {active_port} alicat @ {last_p:.2f} PSI'
                    )
            prev = state
            if abs(last_p - end_psi) < 1.5:
                break
            time.sleep(0.02)

        # Return sweep
        al.set_pressure(start_psi)
        t1 = time.perf_counter()
        while time.perf_counter() - t1 < timeout:
            st = al.read_status()
            if st:
                last_p = st.pressure
            state = read_dio(handle)
            elapsed = time.perf_counter() - t0
            for dio in SWITCH_DIOS:
                if state[dio] != prev[dio]:
                    edges.append((dio, elapsed, last_p, prev[dio], state[dio], active_port))
                    print(
                        f'  [{elapsed:6.2f}s] EDGE {label(dio)} ({bank(dio)}) '
                        f'{prev[dio]}->{state[dio]} on RETURN {active_port} alicat @ {last_p:.2f} PSI'
                    )
            prev = state
            if abs(last_p - start_psi) < 2.0:
                break
            time.sleep(0.02)

    finally:
        al.set_pressure(start_psi)
        al.disconnect()
        if sol_a is not None:
            ljm.eWriteName(handle, f'DIO{sol_a}', 0)
        if sol_b is not None:
            ljm.eWriteName(handle, f'DIO{sol_b}', 0)
        restore_com_inputs(handle, com_dios)

    return edges


def summarize(all_edges: dict[str, list]) -> None:
    print('\n' + '=' * 72)
    print('SWAP ANALYSIS SUMMARY')
    print('=' * 72)
    for test_name, edges in all_edges.items():
        if not edges:
            print(f'\n  {test_name}: no DIO edges')
            continue
        a_edges = [e for e in edges if bank(e[0]) == 'port_a']
        b_edges = [e for e in edges if bank(e[0]) == 'port_b']
        print(f'\n  {test_name}: {len(edges)} edge(s)')
        print(f'    Port A DB9 lines: {len(a_edges)}')
        print(f'    Port B DB9 lines: {len(b_edges)}')
        for dio, elapsed, psi, old, new, active in edges:
            swapped_hint = ''
            if active == 'port_a' and bank(dio) == 'port_b':
                swapped_hint = ' ** SWITCH MAY BE ON OTHER PORT (swapped?) **'
            elif active == 'port_b' and bank(dio) == 'port_a':
                swapped_hint = ' ** SWITCH MAY BE ON OTHER PORT (swapped?) **'
            print(
                f'      {label(dio)} @ {psi:.2f} PSI (sweeping {active}){swapped_hint}'
            )


def main() -> int:
    parser = argparse.ArgumentParser(description='Cross-port switch swap detection')
    parser.add_argument('--vacuum-only', action='store_true')
    args = parser.parse_args()

    config = load_config()
    handle = ljm.openS('T7', 'USB', 'ANY')
    print(f'LabJack serial {ljm.getHandleInfo(handle)[2]}')

    all_edges: dict[str, list] = {}
    sweeps = [
        ('port_a_vacuum_active_solenoid', 'port_a', 14.7, 2.0, True, 'active_port', 'both', None),
        ('port_b_vacuum_active_solenoid', 'port_b', 14.7, 2.0, True, 'active_port', 'both', None),
        ('port_a_pos_active_solenoid', 'port_a', 14.7, 30.0, False, 'active_port', 'both', None),
        ('port_b_pos_active_solenoid', 'port_b', 14.7, 30.0, False, 'active_port', 'both', None),
        ('port_a_vacuum_SWAPPED_solenoid', 'port_a', 14.7, 2.0, True, 'swapped', 'both', None),
        ('port_b_vacuum_SWAPPED_solenoid', 'port_b', 14.7, 2.0, True, 'swapped', 'both', None),
        ('port_a_vacuum_COM_on_port_b', 'port_a', 14.7, 2.0, True, 'active_port', 'port_b', None),
        ('port_b_vacuum_COM_on_port_a', 'port_b', 14.7, 2.0, True, 'active_port', 'port_a', None),
        ('port_a_vacuum_wrong_alicat_B', 'port_a', 14.7, 2.0, True, 'active_port', 'both', 'B'),
        ('port_b_vacuum_wrong_alicat_A', 'port_b', 14.7, 2.0, True, 'active_port', 'both', 'A'),
    ]
    if args.vacuum_only:
        sweeps = [s for s in sweeps if 'vacuum' in s[0] or 'vac' in s[0]]

    try:
        for name, port, start, end, vac, sol_mode, com_mode, al_addr in sweeps:
            all_edges[name] = run_sweep(
                handle,
                active_port=port,
                config=config,
                start_psi=start,
                end_psi=end,
                rate_psi_s=1.5,
                to_vacuum=vac,
                solenoid_mode=sol_mode,
                com_mode=com_mode,
                alicat_address=al_addr,
            )
        summarize(all_edges)
    finally:
        ljm.close(handle)

    any_edge = any(all_edges.values())
    if not any_edge:
        print('\nNo switch DIO activity on either port during any sweep.')
        print('Try manual toggle while running:')
        print('  python scripts/dio_switch_diagnostic.py --skip-sweep --monitor-seconds 60')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
