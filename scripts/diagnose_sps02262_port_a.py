#!/usr/bin/env python3
"""Deep Port A diagnostic for SPS02262-02 seq 600 — all DIOs + vacuum sweep.

Reads DIO0-19 continuously while sweeping through the seq-600 pressure window.
Applies PTP-resolved switch wiring and also logs raw pin states for every DB9 line.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import load_config
from app.database.session import close_database, initialize_database
from app.hardware.port import PortId, PortManager
from app.services.ptp_service import derive_test_setup, load_ptp_from_db
from app.services.ptp_switch_resolver import resolve_ptp_switch_config
from app.services.pressure_domain import convert_pressure

try:
    from labjack import ljm

    LJM_AVAILABLE = True
except ImportError:
    LJM_AVAILABLE = False
    ljm = None  # type: ignore[assignment,misc]

PORT_DB9_BY_PORT = {
    'port_a': {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6, 8: 7, 9: 8},
    'port_b': {1: 9, 2: 10, 3: 11, 4: 12, 5: 13, 6: 14, 7: 15, 8: 16, 9: 17},
}
ALL_DIO = list(range(20))


def db9_map(port_id: str) -> dict[int, int]:
    return PORT_DB9_BY_PORT[str(port_id).strip().lower()]


def dio_label(dio: int, port_id: str = 'port_a') -> str:
    db9 = db9_map(port_id)
    for pin, mapped in db9.items():
        if mapped == dio:
            return f'DIO{dio}(DB9-{pin})'
    if dio == 18:
        return 'DIO18(solenoid-B)'
    if dio == 19:
        return 'DIO19(solenoid-A)'
    return f'DIO{dio}'


def read_all_dio(port: Any) -> dict[int, int]:
    values = port.daq.read_dio_values(max_dio=19) or {}
    return {i: int(values.get(i, 0)) for i in ALL_DIO}


def com_drive_scan(port: Any, port_id: str) -> None:
    """Toggle each Port A DB9 pin as COM output; report which lines respond."""
    handle = port.daq._shared_handle  # diagnostic only
    if handle is None or ljm is None:
        print('  COM-drive scan skipped (no LabJack handle)')
        return

    print('\n' + '=' * 70)
    print(f'COM-DRIVE SCAN ({port_id} DB9 pins 1-9 as COM, all DIOs watched)')
    print('=' * 70)
    db9 = db9_map(port_id)
    candidates = sorted(db9.values())
    found = False
    for com_dio in candidates:
        write_dio_out(handle, com_dio, 0)
        time.sleep(0.03)
        low = read_all_dio(port)
        write_dio_out(handle, com_dio, 1)
        time.sleep(0.03)
        high = read_all_dio(port)
        release_dio(handle, com_dio)
        changed = [
            dio
            for dio in ALL_DIO
            if dio != com_dio and dio not in (18, 19) and low[dio] != high[dio]
        ]
        if changed:
            found = True
            print(f'  COM={dio_label(com_dio, port_id)} responsive lines:')
            for dio in changed:
                print(f'    {dio_label(dio, port_id)}: LOW_com={low[dio]} HIGH_com={high[dio]}')
    if not found:
        print('  No responsive NO/NC lines found for any DB9 COM candidate.')


def write_dio_out(handle: int, dio: int, value: int) -> None:
    ljm.eWriteName(handle, f'DIO{dio}', value)


def release_dio(handle: int, dio: int) -> None:
    ljm.eReadName(handle, f'DIO{dio}')


def apply_ptp_switch(port: Any, resolution: Any) -> None:
    port.daq.switch_nc_derived_from_no = resolution.derive_nc_from_no
    port.daq.switch_no_derived_from_nc = resolution.derive_no_from_nc
    port.daq.configure_di_pins(
        resolution.no_dio,
        resolution.nc_dio,
        resolution.drive_dio,
        com_state=int(port.daq.switch_com_state),
    )


def deep_vacuum_sweep(
    port: Any,
    *,
    targets_psi: list[float],
    rate_psi_s: float,
    dwell_s: float,
    out_rows: list[dict[str, Any]],
) -> None:
    print('\n' + '=' * 70)
    print('DEEP VACUUM SWEEP (all DIO0-19 logged)')
    print('=' * 70)
    print('  Targets (PSI abs):', ', '.join(f'{t:.2f}' for t in targets_psi))

    port.vent_to_atmosphere()
    time.sleep(1.5)
    if not port.prepare_vacuum_route_for_test():
        print('  WARNING: prepare_vacuum_route_for_test failed')
    port.alicat.set_ramp_rate(rate_psi_s)

    prev_dio = read_all_dio(port)
    prev_switch: Optional[bool] = None

    for step_idx, target in enumerate(targets_psi):
        print(f'\n  Step {step_idx + 1}/{len(targets_psi)}: ramp to {target:.2f} PSI abs')
        port.alicat.set_pressure(target)
        port.alicat.cancel_hold()
        step_start = time.perf_counter()
        last_log = 0.0
        while time.perf_counter() - step_start < dwell_s:
            now = time.perf_counter()
            reading = port.read_fast()
            dio = read_all_dio(port)
            trans = reading.transducer.pressure if reading.transducer else None
            alicat = reading.alicat.pressure if reading.alicat else None
            sw = reading.switch
            switch_act = sw.switch_activated if sw else None
            no_act = sw.no_active if sw else None
            nc_act = sw.nc_active if sw else None

            changed_dios = [i for i in ALL_DIO if dio[i] != prev_dio[i]]
            if changed_dios or (switch_act is not None and switch_act != prev_switch):
                parts = [f'    EDGE step={step_idx + 1} tgt={target:.1f}']
                if trans is not None:
                    parts.append(f'trans={trans:.3f}')
                if alicat is not None:
                    parts.append(f'alicat={alicat:.3f}')
                if switch_act is not None:
                    parts.append(f'act={switch_act} no={no_act} nc={nc_act}')
                for dio_num in changed_dios:
                    parts.append(f'{dio_label(dio_num)}:{prev_dio[dio_num]}->{dio[dio_num]}')
                print(' '.join(parts))

            if now - last_log >= 0.1:
                row: dict[str, Any] = {
                    'ts': time.time(),
                    'step': step_idx + 1,
                    'target_psi': target,
                    'trans_psi': trans,
                    'alicat_psi': alicat,
                    'switch_activated': switch_act,
                    'switch_no': no_act,
                    'switch_nc': nc_act,
                }
                for i in ALL_DIO:
                    row[f'dio_{i}'] = dio[i]
                out_rows.append(row)
                last_log = now

            prev_dio = dio
            prev_switch = switch_act
            time.sleep(0.02)


def analyze_rows(rows: list[dict[str, Any]], port_id: str = 'port_a') -> None:
    print('\n' + '=' * 70)
    print('ANALYSIS')
    print('=' * 70)
    if not rows:
        print('  No samples.')
        return

    toggled: dict[int, list[tuple[float, float, int, int]]] = {}
    for idx in range(1, len(rows)):
        prev, cur = rows[idx - 1], rows[idx]
        for i in ALL_DIO:
            before = prev.get(f'dio_{i}')
            after = cur.get(f'dio_{i}')
            if before is None or after is None or before == after:
                continue
            toggled.setdefault(i, []).append(
                (
                    cur.get('alicat_psi') or cur.get('trans_psi') or 0.0,
                    cur.get('target_psi') or 0.0,
                    before,
                    after,
                )
            )

    if toggled:
        print('\n  DIO lines that toggled during sweep:')
        for dio in sorted(toggled):
            print(f'    {dio_label(dio, port_id)}: {len(toggled[dio])} transition(s)')
            for pressure, target, before, after in toggled[dio][:8]:
                print(f'      {before}->{after} at ~{pressure:.2f} PSI (step target {target:.2f})')
    else:
        print('\n  No DIO0-19 transitions detected during sweep.')

    switch_changes = sum(
        1
        for idx in range(1, len(rows))
        if rows[idx].get('switch_activated') != rows[idx - 1].get('switch_activated')
    )
    print(f'\n  PTP-resolved switch_activated transitions: {switch_changes}')

    # Snapshot at band edges (seq 600: 590-610 Torr ~ 11.4-11.8 PSI abs)
    band_low = convert_pressure(450.0, 'Torr', 'PSI')
    band_mid = convert_pressure(600.0, 'Torr', 'PSI')
    band_high = convert_pressure(610.0, 'Torr', 'PSI')
    for label_name, setpoint in (
        ('deep_vac ~450T', band_low),
        ('activation ~600T', band_mid),
        ('upper_band ~610T', band_high),
        ('atmosphere', 14.7),
    ):
        closest = min(
            rows,
            key=lambda r: abs((r.get('alicat_psi') or r.get('trans_psi') or 0) - setpoint),
        )
        p = closest.get('alicat_psi') or closest.get('trans_psi')
        p_text = f'{p:.2f}' if p is not None else '--'
        db9_pins = db9_map(port_id)
        db9 = ' '.join(
            f'p{pin}={closest.get(f"dio_{db9_pins[pin]}", "?")}'
            for pin in (3, 4, 5, 6)
        )
        print(
            f'\n  Near {label_name} ({setpoint:.2f} PSI target): '
            f'measured={p_text} act={closest.get("switch_activated")} {db9}'
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--part', default='SPS02262-02')
    parser.add_argument('--sequence', default='600')
    parser.add_argument('--rate', type=float, default=3.0, help='Ramp rate PSI/s')
    parser.add_argument('--dwell', type=float, default=25.0, help='Seconds per sweep step')
    parser.add_argument('--skip-com-scan', action='store_true')
    parser.add_argument('--port', choices=['port_a', 'port_b'], default='port_a')
    parser.add_argument('--out-dir', default='logs/diagnostics')
    args = parser.parse_args()

    port_id = args.port
    port_enum = PortId.PORT_A if port_id == 'port_a' else PortId.PORT_B

    if not LJM_AVAILABLE:
        print('labjack.ljm not available')
        return 1

    config = load_config()
    initialize_database(config.get('database', {}))
    params = load_ptp_from_db(args.part, args.sequence)
    if not params:
        print(f'No PTP for {args.part}/{args.sequence}')
        return 1
    setup = derive_test_setup(args.part, args.sequence, params)
    resolution = resolve_ptp_switch_config(
        ptp_params=params,
        port_id=port_id,
        port_config=config['hardware']['labjack'][port_id],
    )

    print('=' * 70)
    print(f'DEEP DIAGNOSTIC: {args.part} seq {args.sequence} on {port_id}')
    print(f'  Direction: {setup.activation_direction}  Target: {setup.activation_target} {setup.units_label}')
    print(f'  PTP valid: {resolution.is_valid}')
    print(f'  Resolution: {resolution.summary}')
    if resolution.warnings:
        print(f'  Warnings: {"; ".join(resolution.warnings)}')
    if not resolution.is_valid:
        print(f'  Errors: {"; ".join(resolution.errors)}')
        return 1

    pm = PortManager(config)
    pm.initialize_ports()
    port = pm.get_port(port_enum)
    if port is None:
        print(f'{port_id} unavailable')
        close_database()
        return 1
    if not port.connect():
        print(
            f'ERROR: Could not connect {port_id} — close Stinger/other apps using LabJack (USB) and COM3'
        )
        pm.disconnect_all()
        close_database()
        return 1
    port.configure_from_ptp(params)
    apply_ptp_switch(port, resolution)

    try:
        if not args.skip_com_scan:
            com_drive_scan(port, port_id)
            apply_ptp_switch(port, resolution)

        # Seq 600 increasing vacuum band: pull deep, sweep up through activation
        atm = 14.7
        deep = convert_pressure(400.0, 'Torr', 'PSI')
        band_lo = convert_pressure(450.0, 'Torr', 'PSI')
        band_mid = convert_pressure(600.0, 'Torr', 'PSI')
        band_hi = convert_pressure(650.0, 'Torr', 'PSI')

        targets = [
            atm,
            deep,
            band_mid,
            band_hi,
            atm,
        ]

        rows: list[dict[str, Any]] = []
        deep_vacuum_sweep(
            port,
            targets_psi=targets,
            rate_psi_s=args.rate,
            dwell_s=args.dwell,
            out_rows=rows,
        )
        analyze_rows(rows, port_id)

        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        csv_path = out_dir / f'sps02262_{port_id}_deep_{stamp}.csv'
        if rows:
            fields = list(rows[0].keys())
            with csv_path.open('w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            print(f'\n  CSV: {csv_path} ({len(rows)} rows)')
    finally:
        try:
            port.vent_to_atmosphere()
        except Exception:
            pass
        pm.disconnect_all()
        close_database()

    print('\n' + '=' * 70)
    print('DONE')
    print('=' * 70)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
