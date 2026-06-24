"""Headless 17022 pressure-switch activation test after bench re-wire.

Uses PTP 17022/399 switch resolution, drives COM, sweeps Alicat on each port,
and reports activation/deactivation edges with pressure.

Usage:
    python scripts/switch_activation_test.py
    python scripts/switch_activation_test.py --part 17022 --sequence 399
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import get_port_config, load_config
from app.database.session import close_database, initialize_database
from app.hardware.alicat import AlicatController
from app.hardware.labjack import LabJackController
from app.services.ptp_service import derive_test_setup, load_ptp_from_db
from app.services.ptp_switch_resolver import resolve_ptp_switch_config
from scripts.diagnose_ptp_switch import configure_labjack_for_resolution, format_switch_state

GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
RESET = '\033[0m'
BOLD = '\033[1m'


def print_header(text: str) -> None:
    print(f'\n{BOLD}{"=" * 72}{RESET}')
    print(f'{BOLD}{text}{RESET}')
    print(f'{BOLD}{"=" * 72}{RESET}')


def torr_to_psia(torr: float) -> float:
    return torr * 14.696 / 760.0


def test_port(
    *,
    port_key: str,
    config: dict,
    ptp_params: dict,
    setup,
    start_psi: float,
    end_psi: float,
    rate_psi_s: float,
    to_vacuum: bool,
) -> bool:
    label = port_key.replace('_', ' ').title()
    pc = get_port_config(config, port_key)
    lj_cfg = {**config['hardware']['labjack'], **pc['labjack']}
    al_cfg = {**config['hardware']['alicat'], **pc['alicat']}

    resolution = resolve_ptp_switch_config(
        ptp_params=ptp_params,
        port_id=port_key,
        port_config=pc['labjack'],
    )
    print_header(f'{label} — {resolution.summary}')
    if not resolution.is_valid:
        print(f'{RED}Invalid switch resolution: {resolution.errors}{RESET}')
        return False

    lj = LabJackController(lj_cfg)
    if not lj.configure():
        print(f'{RED}LabJack configure failed: {lj._last_status}{RESET}')
        return False

    configure_labjack_for_resolution(
        lj,
        resolution,
        com_state=int(lj_cfg.get('switch_com_state', 0)),
    )
    lj.set_solenoid(to_vacuum=to_vacuum)

    al = AlicatController(
        {
            **al_cfg,
            'auto_tare_on_connect': False,
            'auto_configure': False,
        }
    )
    if not al.connect():
        print(f'{RED}Alicat connect failed: {al._last_status}{RESET}')
        lj.cleanup()
        return False

    route = 'vacuum' if to_vacuum else 'atmosphere'
    print(f'  PTP target: {setup.activation_target} {setup.units_label} {setup.activation_direction}')
    if setup.activation_target and setup.units_label and 'torr' in setup.units_label.lower():
        print(f'  (~{torr_to_psia(setup.activation_target):.2f} PSIA equivalent)')
    print(f'  Sweep: {start_psi:.1f} -> {end_psi:.1f} PSI @ {rate_psi_s:.1f} PSI/s ({route})')

    edges: list[tuple[str, float, bool]] = []
    last_activated: bool | None = None

    def sample_switch(phase: str, alicat_psi: float) -> None:
        nonlocal last_activated
        sw = lj.read_switch_state()
        if sw is None:
            return
        if last_activated is not None and sw.switch_activated != last_activated:
            direction = 'ACTIVATED' if sw.switch_activated else 'DEACTIVATED'
            edges.append((direction, alicat_psi, sw.switch_activated))
            print(
                f'  {GREEN}[EDGE]{RESET} {direction} @ {alicat_psi:.2f} PSIA '
                f'(NO={sw.no_active} NC={sw.nc_active} valid={sw.is_valid})'
            )
        last_activated = sw.switch_activated

    try:
        sw = lj.read_switch_state()
        print(f'  Initial: {format_switch_state(sw)}')

        al.cancel_hold()
        time.sleep(0.2)
        al.set_ramp_rate(0, time_unit='s')
        time.sleep(0.1)
        al.set_pressure(start_psi)
        time.sleep(6)

        st = al.read_status()
        sample_switch('start', st.pressure if st else start_psi)
        print(f'  At {start_psi:.1f} PSI: alicat={st.pressure:.2f} | {format_switch_state(lj.read_switch_state())}')

        al.set_ramp_rate(rate_psi_s, time_unit='s')
        time.sleep(0.1)
        al.set_pressure(end_psi)

        print('  Sweeping down...' if end_psi < start_psi else '  Sweeping up...')
        t0 = time.perf_counter()
        timeout = abs(start_psi - end_psi) / max(rate_psi_s, 0.1) + 90
        while time.perf_counter() - t0 < timeout:
            st = al.read_status()
            p = st.pressure if st else start_psi
            sample_switch('sweep', p)
            if abs(p - end_psi) < 1.5:
                break
            time.sleep(0.02)

        time.sleep(2)
        st = al.read_status()
        sample_switch('end', st.pressure if st else end_psi)
        print(f'  At {end_psi:.1f} PSI: alicat={st.pressure:.2f} | {format_switch_state(lj.read_switch_state())}')

        print(f'  Returning to {start_psi:.1f} PSI...')
        al.set_pressure(start_psi)
        t1 = time.perf_counter()
        while time.perf_counter() - t1 < timeout:
            st = al.read_status()
            p = st.pressure if st else start_psi
            sample_switch('return', p)
            if abs(p - start_psi) < 2.0:
                break
            time.sleep(0.02)

        time.sleep(2)
        st = al.read_status()
        sample_switch('rest', st.pressure if st else start_psi)
        print(f'  At rest: alicat={st.pressure:.2f} | {format_switch_state(lj.read_switch_state())}')

    finally:
        try:
            al.set_pressure(start_psi)
        except Exception:
            pass
        al.disconnect()
        lj.set_solenoid_safe()
        lj.cleanup()

    print(f'\n  Edges detected: {len(edges)}')
    for direction, psi, activated in edges:
        print(f'    {direction} @ {psi:.2f} PSIA')

    if len(edges) >= 1:
        print(f'  {GREEN}{BOLD}PASS{RESET} — switch responded on {label}')
        return True
    print(f'  {RED}{BOLD}FAIL{RESET} — no activation/deactivation on {label}')
    return False


def baseline_dio_scan() -> None:
    from labjack import ljm

    print_header('Baseline DIO scan (idle)')
    handle = ljm.openS('T7', 'USB', 'ANY')
    try:
        state = int(ljm.eReadName(handle, 'DIO_STATE'))
        for group, start, end in [('PortA DB9', 0, 8), ('PortB DB9', 9, 17)]:
            parts = [f'DIO{i}={1 if state & (1 << i) else 0}' for i in range(start, end + 1)]
            print(f'  {group}: {", ".join(parts)}')
    finally:
        ljm.close(handle)


def main() -> int:
    parser = argparse.ArgumentParser(description='17022 switch activation headless test')
    parser.add_argument('--part', default='17022')
    parser.add_argument('--sequence', default='399')
    parser.add_argument('--rate', type=float, default=1.5)
    args = parser.parse_args()

    config = load_config()
    db_ok = initialize_database(config.get('database', {}))

    try:
        params = load_ptp_from_db(args.part, args.sequence) if db_ok else {}
        if not params:
            print(f'{RED}No PTP for {args.part}/{args.sequence}{RESET}')
            return 2

        setup = derive_test_setup(args.part, args.sequence, params)
        print_header(f'Stinger switch test — {args.part}/{args.sequence}')
        print(
            f'  Direction: {setup.activation_direction}  '
            f'Target: {setup.activation_target} {setup.units_label}  '
            f'Ref: {setup.pressure_reference}'
        )

        baseline_dio_scan()

        # 17022/399 decreasing @ 400 Torr — vacuum pull through ~8 PSIA
        target_psia = torr_to_psia(float(setup.activation_target or 400.0))
        vac_end = max(0.5, target_psia - 3.0)
        vac_start = 14.7

        results: dict[str, bool] = {}
        results['port_a_vacuum'] = test_port(
            port_key='port_a',
            config=config,
            ptp_params=params,
            setup=setup,
            start_psi=vac_start,
            end_psi=vac_end,
            rate_psi_s=args.rate,
            to_vacuum=True,
        )
        results['port_b_vacuum'] = test_port(
            port_key='port_b',
            config=config,
            ptp_params=params,
            setup=setup,
            start_psi=vac_start,
            end_psi=vac_end,
            rate_psi_s=args.rate,
            to_vacuum=True,
        )
        # Also sweep positive in case switch trips high
        results['port_a_positive'] = test_port(
            port_key='port_a',
            config=config,
            ptp_params=params,
            setup=setup,
            start_psi=14.7,
            end_psi=30.0,
            rate_psi_s=args.rate,
            to_vacuum=False,
        )
        results['port_b_positive'] = test_port(
            port_key='port_b',
            config=config,
            ptp_params=params,
            setup=setup,
            start_psi=14.7,
            end_psi=30.0,
            rate_psi_s=args.rate,
            to_vacuum=False,
        )

        print_header('Summary')
        for name, ok in results.items():
            status = f'{GREEN}PASS{RESET}' if ok else f'{RED}FAIL{RESET}'
            print(f'  {name}: [{status}]')

        return 0 if all(results.values()) else 1
    finally:
        if db_ok:
            close_database()


if __name__ == '__main__':
    raise SystemExit(main())
