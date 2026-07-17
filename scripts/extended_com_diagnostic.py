"""Extended COM / DIO wiring probe — all DB9 pins as COM, dual-COM, pull-down matrix."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import load_config

try:
    from labjack import ljm
except ImportError as exc:
    raise SystemExit(f'labjack.ljm required: {exc}') from exc

PORT_A_DB9 = {p: p - 1 for p in range(1, 10)}
PORT_B_DB9 = {p: p + 8 for p in range(1, 10)}
ALL_DIO = list(range(20))
RELAY_DIOS = {18, 19}

LABELS: dict[int, str] = {}
for pin, dio in PORT_A_DB9.items():
    LABELS[dio] = f'PortA DB9-{pin} (DIO{dio})'
for pin, dio in PORT_B_DB9.items():
    LABELS[dio] = f'PortB DB9-{pin} (DIO{dio})'


def label(dio: int) -> str:
    return LABELS.get(dio, f'DIO{dio}')


def read_mask(handle: int) -> dict[int, int]:
    state = int(ljm.eReadName(handle, 'DIO_STATE'))
    return {i: 1 if state & (1 << i) else 0 for i in ALL_DIO}


def read_individual(handle: int) -> dict[int, int]:
    names = [f'DIO{i}' for i in ALL_DIO]
    values = ljm.eReadNames(handle, len(names), names)
    return {i: int(v) for i, v in zip(ALL_DIO, values)}


def set_input(handle: int, dio: int) -> None:
    ljm.eReadName(handle, f'DIO{dio}')


def set_output(handle: int, dio: int, value: int) -> None:
    ljm.eWriteName(handle, f'DIO{dio}', value)


def all_inputs_except(handle: int, exclude: set[int]) -> None:
    for dio in ALL_DIO:
        if dio not in exclude and dio not in RELAY_DIOS:
            set_input(handle, dio)


def com_matrix(handle: int) -> list[tuple[int, list[tuple[int, int, int, int, int]]]]:
    print('\n' + '=' * 70)
    print('COM MATRIX: toggle each DB9 DIO as COM (LOW vs HIGH)')
    print('=' * 70)
    candidates = sorted(set(PORT_A_DB9.values()) | set(PORT_B_DB9.values()))
    responsive: list[tuple[int, list[tuple[int, int, int, int, int]]]] = []

    for com_dio in candidates:
        all_inputs_except(handle, {com_dio})
        set_output(handle, com_dio, 0)
        time.sleep(0.05)
        lo_m = read_mask(handle)
        lo_i = read_individual(handle)
        set_output(handle, com_dio, 1)
        time.sleep(0.05)
        hi_m = read_mask(handle)
        hi_i = read_individual(handle)
        set_input(handle, com_dio)

        changed = []
        for dio in ALL_DIO:
            if dio in (com_dio, *RELAY_DIOS):
                continue
            if lo_m[dio] != hi_m[dio] or lo_i[dio] != hi_i[dio]:
                changed.append((dio, lo_m[dio], hi_m[dio], lo_i[dio], hi_i[dio]))

        if changed:
            responsive.append((com_dio, changed))
            print(f'\n  COM {label(com_dio)}:')
            for dio, lm, hm, li, hi in changed:
                print(f'    {label(dio)}: mask {lm}->{hm}, individual {li}->{hi}')

    if not responsive:
        print('\n  No responsive lines for any DB9 COM candidate.')
    return responsive


def dual_com_baseline(handle: int, com_dios: list[int]) -> None:
    print('\n' + '=' * 70)
    print(f'DUAL COM BASELINE: drive {", ".join(label(d) for d in com_dios)} LOW')
    print('=' * 70)
    exclude = set(com_dios) | RELAY_DIOS
    all_inputs_except(handle, exclude)
    for com in com_dios:
        set_output(handle, com, 0)
    time.sleep(0.05)
    state = read_individual(handle)
    print_dio_table(state, 'With COM(s) driven LOW')
    low = [label(i) for i, v in state.items() if v == 0 and i not in exclude]
    print(f'  Other lines reading LOW: {low or "(none)"}')
    for com in com_dios:
        set_input(handle, com)


def pull_down_matrix(handle: int) -> None:
    print('\n' + '=' * 70)
    print('PULL-DOWN MATRIX: each DIO0-17 as output LOW, scan others')
    print('=' * 70)
    found = False
    for out_dio in range(18):
        if out_dio in RELAY_DIOS:
            continue
        all_inputs_except(handle, {out_dio})
        set_output(handle, out_dio, 0)
        time.sleep(0.02)
        state = read_individual(handle)
        set_input(handle, out_dio)
        others_low = [d for d in range(18) if d != out_dio and d not in RELAY_DIOS and state[d] == 0]
        if others_low:
            found = True
            print(f'  Drive {label(out_dio)} LOW -> also LOW: {[label(d) for d in others_low]}')
    if not found:
        print('  No coupled pull-downs detected.')


def com_sweep_monitor(
    handle: int,
    com_dios: list[int],
    duration_s: float,
    com_value: int = 0,
) -> None:
    print('\n' + '=' * 70)
    print(
        f'COM + MONITOR ({duration_s:.0f}s): COM held '
        f'{"LOW" if com_value == 0 else "HIGH"}, watch all DIO'
    )
    print('=' * 70)
    exclude = set(com_dios) | RELAY_DIOS
    all_inputs_except(handle, exclude)
    for com in com_dios:
        set_output(handle, com, com_value)
        print(f'  Driving {label(com)} {"LOW" if com_value == 0 else "HIGH"}')

    prev = read_mask(handle)
    changes: dict[int, list[tuple[float, int, int]]] = {}
    start = time.perf_counter()
    while time.perf_counter() - start < duration_s:
        state = read_mask(handle)
        elapsed = time.perf_counter() - start
        for dio in ALL_DIO:
            if dio in exclude:
                continue
            if state[dio] != prev[dio]:
                changes.setdefault(dio, []).append((elapsed, prev[dio], state[dio]))
                print(f'  [{elapsed:7.3f}s] {label(dio)}: {prev[dio]} -> {state[dio]}')
        prev = state
        time.sleep(0.005)

    if not changes:
        print(f'  No DIO changes in {duration_s:.0f}s with COM held LOW.')
    else:
        print('  Summary:')
        for dio, evts in sorted(changes.items()):
            print(f'    {label(dio)}: {len(evts)} transitions')

    for com in com_dios:
        set_input(handle, com)


def print_dio_table(values: dict[int, int], title: str = '') -> None:
    if title:
        print(f'\n  {title}')
    print('  ' + '-' * 60)
    for group_name, dio_range in [
        ('FIO DIO0-7 (PortA DB9 1-8)', range(0, 8)),
        ('EIO DIO8-15 (PortB DB9 1-8)', range(8, 16)),
        ('CIO DIO16-19', range(16, 20)),
    ]:
        parts = [f'{d}={values.get(d, -1)}' for d in dio_range]
        print(f'  {group_name}: {", ".join(parts)}')
    print('  ' + '-' * 60)


def sweep_with_com(
    handle: int,
    port_key: str,
    com_dio: int,
    start_psi: float,
    end_psi: float,
    to_vacuum: bool,
) -> None:
    from app.hardware.alicat import AlicatController

    config = load_config()
    lj_port = config['hardware']['labjack'][port_key]
    al_base = config['hardware']['alicat']
    al_port = al_base[port_key]
    solenoid = lj_port.get('solenoid_dio')

    print('\n' + '=' * 70)
    route = 'vacuum' if to_vacuum else 'atmosphere'
    print(f'SWEEP WITH COM: {port_key} {start_psi}->{end_psi} PSI ({route})')
    print(f'  COM={label(com_dio)} LOW, solenoid DIO{solenoid}')
    print('=' * 70)

    all_inputs_except(handle, {com_dio, solenoid} if solenoid else {com_dio})
    set_output(handle, com_dio, 0)
    if solenoid is not None:
        set_output(handle, solenoid, 1 if to_vacuum else 0)

    al_cfg = {
        'com_port': al_port['com_port'],
        'address': al_port['address'],
        'baudrate': al_base.get('baudrate', 19200),
        'timeout_s': 0.2,
        'auto_tare_on_connect': False,
        'auto_configure': False,
    }
    al = AlicatController(al_cfg)
    if not al.connect():
        print(f'  Alicat connect failed: {al._last_status}')
        return

    prev = read_mask(handle)
    edges: list[str] = []
    try:
        al.cancel_hold()
        time.sleep(0.2)
        al.set_ramp_rate(0, time_unit='s')
        al.set_pressure(start_psi)
        time.sleep(5)
        al.set_ramp_rate(1.5, time_unit='s')
        al.set_pressure(end_psi)
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 120:
            st = al.read_status()
            p = st.pressure if st else 0.0
            state = read_mask(handle)
            for dio in ALL_DIO:
                if dio in (com_dio, solenoid):
                    continue
                if state[dio] != prev[dio]:
                    msg = f'  EDGE {label(dio)} {prev[dio]}->{state[dio]} @ alicat={p:.2f} PSI'
                    print(msg)
                    edges.append(msg)
            prev = state
            if st and abs(st.pressure - end_psi) < 1.5:
                break
            time.sleep(0.02)
        print(f'  Total DIO edges during sweep: {len(edges)}')
    finally:
        al.set_pressure(start_psi)
        al.disconnect()
        if solenoid is not None:
            set_output(handle, solenoid, 0)
        set_input(handle, com_dio)


def com_high_low_scan(handle: int, com_dios: list[int]) -> None:
    print('\n' + '=' * 70)
    print('COM HIGH/LOW SCAN: look for any line pulled LOW')
    print('=' * 70)
    for com in com_dios:
        for value, name in ((0, 'LOW'), (1, 'HIGH')):
            all_inputs_except(handle, {com})
            set_output(handle, com, value)
            time.sleep(0.05)
            state = read_individual(handle)
            set_input(handle, com)
            lows = [
                label(i)
                for i, v in state.items()
                if v == 0 and i not in ({com} | RELAY_DIOS)
            ]
            print(f'  {label(com)} driven {name}: other lines LOW = {lows or "(none)"}')


def monitor_with_com_state(
    handle: int,
    com_dios: list[int],
    com_value: int,
    duration_s: float,
) -> None:
    print('\n' + '=' * 70)
    print(
        f'MONITOR {duration_s:.0f}s with COM(s) at {com_value} '
        f'({", ".join(label(d) for d in com_dios)})'
    )
    print('=' * 70)
    exclude = set(com_dios) | RELAY_DIOS
    all_inputs_except(handle, exclude)
    for com in com_dios:
        set_output(handle, com, com_value)
    com_sweep_monitor(handle, com_dios, duration_s, com_value=com_value)


def main() -> int:
    parser = argparse.ArgumentParser(description='Extended COM/DIO wiring diagnostic')
    parser.add_argument('--monitor-s', type=float, default=15.0)
    parser.add_argument('--skip-sweeps', action='store_true')
    parser.add_argument('--com-high-monitor-s', type=float, default=20.0)
    args = parser.parse_args()

    handle = ljm.openS('T7', 'USB', 'ANY')
    info = ljm.getHandleInfo(handle)
    print(f'LabJack serial={info[2]}')

    try:
        print_dio_table(read_individual(handle), 'Idle baseline')
        com_matrix(handle)
        pull_down_matrix(handle)

        likely_coms = [3, 12]
        alt_coms = [2, 11]  # DB9 pin 3 as COM hypothesis
        com_high_low_scan(handle, likely_coms + alt_coms)

        dual_com_baseline(handle, likely_coms)
        com_sweep_monitor(handle, likely_coms, args.monitor_s)
        monitor_with_com_state(handle, likely_coms, 1, args.com_high_monitor_s)

        # PTP 17022 NC=pin1, NO=pin3 — try pin 1 as COM too
        dual_com_baseline(handle, [0, 9])
        com_sweep_monitor(handle, [0, 9], 10.0)

        if not args.skip_sweeps:
            sweep_with_com(handle, 'port_a', 3, 14.7, 35.0, to_vacuum=False)
            sweep_with_com(handle, 'port_a', 3, 14.7, 2.0, to_vacuum=True)
            sweep_with_com(handle, 'port_b', 12, 14.7, 35.0, to_vacuum=False)
            sweep_with_com(handle, 'port_b', 12, 14.7, 2.0, to_vacuum=True)

            # Also try pin 3 as COM (some benches wire COM on pin 3)
            print('\n--- Alternate COM hypothesis: DB9 pin 3 (DIO2 / DIO11) ---')
            sweep_with_com(handle, 'port_a', 2, 14.7, 35.0, to_vacuum=False)
            sweep_with_com(handle, 'port_b', 11, 14.7, 35.0, to_vacuum=False)
    finally:
        ljm.close(handle)

    print('\nExtended COM diagnostic complete.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
