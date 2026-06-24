"""Bench loopback test — prove LabJack DIO reads work independent of the switch.

This splits the problem:
  A) LabJack + pin mapping OK?  (jumper / screwdriver on screw terminals)
  B) DB9 harness + switch OK? (only after A passes)

Usage (venv, repo root):
    python scripts/switch_bench_loopback.py
    python scripts/switch_bench_loopback.py --monitor-s 120

Interactive steps print to the console; apply jumpers when prompted.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from labjack import ljm
except ImportError as exc:
    raise SystemExit(f'labjack.ljm required: {exc}') from exc

PORT_A = {p: p - 1 for p in range(1, 10)}
PORT_B = {p: p + 8 for p in range(1, 10)}


def label(dio: int) -> str:
    for pin, d in PORT_A.items():
        if d == dio:
            return f'PortA DB9-{pin} (DIO{dio}, FIO/EIO terminal per harness doc)'
    for pin, d in PORT_B.items():
        if d == dio:
            return f'PortB DB9-{pin} (DIO{dio})'
    return f'DIO{dio}'


def read_all(handle: int) -> dict[int, int]:
    state = int(ljm.eReadName(handle, 'DIO_STATE'))
    return {i: 1 if state & (1 << i) else 0 for i in range(20)}


def set_input(handle: int, dio: int) -> None:
    ljm.eReadName(handle, f'DIO{dio}')


def set_output(handle: int, dio: int, value: int) -> None:
    ljm.eWriteName(handle, f'DIO{dio}', value)


def monitor(handle: int, duration_s: float, ignore: set[int]) -> dict[int, list[tuple[float, int, int]]]:
    print(f'\nMonitoring DIO0-17 for {duration_s:.0f}s (~200 Hz). Toggle switch or tap jumper now.')
    prev = read_all(handle)
    changes: dict[int, list[tuple[float, int, int]]] = {}
    start = time.perf_counter()
    while time.perf_counter() - start < duration_s:
        state = read_all(handle)
        t = time.perf_counter() - start
        for dio in range(18):
            if dio in ignore:
                continue
            if state[dio] != prev[dio]:
                changes.setdefault(dio, []).append((t, prev[dio], state[dio]))
                print(f'  [{t:7.2f}s] {label(dio)}: {prev[dio]} -> {state[dio]}')
        prev = state
        time.sleep(0.005)
    return changes


def loopback_pair(handle: int, com_dio: int, sense_dio: int) -> bool:
    """Drive COM low; user jumpers COM to sense; we expect sense to read 0."""
    print('\n' + '-' * 70)
    print(f'LOOPBACK: {label(com_dio)} (COM) driven LOW')
    print(f'          Jumper {label(com_dio)} to {label(sense_dio)} (expected NO pin 3) at the LabJack terminals.')
    print('          Press Enter when jumper is ON, or type skip.')
    try:
        ans = input('> ').strip().lower()
    except EOFError:
        ans = 'skip'
    if ans == 'skip':
        print('  Skipped.')
        return False

    for dio in range(20):
        if dio not in (com_dio, 18, 19):
            set_input(handle, dio)
    set_output(handle, com_dio, 0)
    time.sleep(0.05)
    state = read_all(handle)
    set_input(handle, com_dio)

    com_val = 0
    sense_val = state.get(sense_dio, -1)
    ok = sense_val == 0
    print(f'  COM DIO{com_dio}={com_val} (driven), sense DIO{sense_dio}={sense_val} (expect 0 with jumper)')
    if ok:
        print('  PASS — LabJack reads loopback on this pair.')
    else:
        print('  FAIL — sense line did not pull low. Wrong terminal, bad jumper, or pin map mismatch.')
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description='Switch bench loopback / live monitor')
    parser.add_argument('--monitor-s', type=float, default=60.0)
    parser.add_argument('--skip-loopback', action='store_true')
    args = parser.parse_args()

    handle = ljm.openS('T7', 'USB', 'ANY')
    print(f'LabJack serial {ljm.getHandleInfo(handle)[2]}')
    print('\nBaseline (nothing driven):')
    base = read_all(handle)
    for dio in range(18):
        if base[dio] == 0:
            print(f'  LOW: {label(dio)}')

    results: list[bool] = []
    try:
        if not args.skip_loopback:
            print('\n' + '=' * 70)
            print('PART 1: LOOPBACK (proves LabJack DIO path — not the switch)')
            print('=' * 70)
            print('Use a jumper wire at the LabJack screw terminals (DB37 FIO / DB15 EIO).')
            print('See docs/HARDWARE_SPEC.md DB9 pin table for terminal names.')

            # PTP 17022: COM=pin4, NO=pin3
            pairs = [
                (3, 2, 'Port A standard (COM pin4 -> DIO3, NO pin3 -> DIO2)'),
                (12, 11, 'Port B standard (COM pin4 -> DIO12, NO pin3 -> DIO11)'),
            ]
            for com, sense, desc in pairs:
                print(f'\n--- {desc} ---')
                results.append(loopback_pair(handle, com, sense))

        print('\n' + '=' * 70)
        print('PART 2: LIVE MONITOR (proves switch/harness — after wiring verified)')
        print('=' * 70)
        print('Optional: drive both COM lines LOW (PTP pin 4) during monitor.')
        for com in (3, 12):
            for dio in range(20):
                if dio not in (com, 18, 19):
                    set_input(handle, dio)
            set_output(handle, com, 0)
        print('  COM DIO3 and DIO12 driven LOW.')

        changes = monitor(handle, args.monitor_s, ignore={3, 12, 18, 19})
        for com in (3, 12):
            set_input(handle, com)

        print('\n' + '=' * 70)
        print('SUMMARY')
        print('=' * 70)
        if results:
            passed = sum(results)
            print(f'  Loopback pairs passed: {passed}/{len(results)}')
            if passed == 0:
                print('  -> LabJack pin path or terminal ID is wrong. Fix loopback before switch debug.')
            elif passed < len(results):
                print('  -> One port path OK; check the failing port harness/connector.')
        if changes:
            print(f'  Live monitor: {sum(len(v) for v in changes.values())} transitions on:')
            for dio, evts in sorted(changes.items()):
                print(f'    {label(dio)}: {len(evts)}')
        else:
            print('  Live monitor: no transitions.')
            print('  If loopback PASS but monitor quiet: switch not closing on COM, or not on these pins.')
            print('  If loopback FAIL: doc pin map vs actual harness may differ — trace with meter at T7 terminals.')

    finally:
        ljm.close(handle)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
