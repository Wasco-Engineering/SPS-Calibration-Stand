#!/usr/bin/env python3
"""Rudimentary Alicat + transducer + Mensor correlation (one Stinger port).

Safety: Mensor is limited to 30 PSIA on this stand (broken / reduced range).
Targets above --max-psia are skipped. For 115 PSIA work, power off Mensor and
remove a transducer per your procedure before going higher.

Examples:
    python scripts/mensor_triplet_correlation.py --discover-mensor
    python scripts/mensor_triplet_correlation.py --port port_b --static 5 10 15 20 25 30
    python scripts/mensor_triplet_correlation.py --port port_a --hold 10 --rate 2
"""

from __future__ import annotations

import argparse
import csv
import logging
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import load_config as load_stinger_config
from app.hardware.alicat import AlicatController, AlicatController as AC
from app.hardware.labjack import LabJackController
from quality_cal.config import get_default_config_path, load_config as load_qc_config
from quality_cal.core.hardware_discovery import discover_mensor_port
from quality_cal.core.mensor_reader import MensorReader

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)

SAFE_SP = 0.2
PORT_SIDE = {'port_a': 'left', 'port_b': 'right'}
DEFAULT_STATIC = [5.0, 10.0, 14.0, 18.0, 22.0, 26.0, 30.0]
MENSOR_MAX_PSIA_DEFAULT = 30.0
VACUUM_BELOW_PSI = 12.0


@dataclass
class TripletSample:
    elapsed_s: float
    target_psia: float
    mensor_psia: Optional[float]
    alicat_psia: Optional[float]
    transducer_psia: Optional[float]
    transducer_v: Optional[float]
    mensor_raw: str


def _setup_stinger(
    port_key: str,
) -> Tuple[LabJackController, LabJackController, AlicatController, AlicatController, Any, LabJackController]:
    config = load_stinger_config()
    lj_cfg = config['hardware']['labjack']
    ali_cfg = config['hardware']['alicat']
    ca = LabJackController({**lj_cfg, **lj_cfg['port_a']})
    cb = LabJackController({**lj_cfg, **lj_cfg['port_b']})
    if not ca.configure() or not cb.configure():
        raise RuntimeError('LabJack configure failed')
    ca.set_solenoid(False)
    cb.set_solenoid(False)

    AC._shared_serials.clear()
    shared = None
    ctrls: Dict[str, AlicatController] = {}
    for key in ['port_a', 'port_b']:
        pc = ali_cfg[key]
        c = AlicatController({**ali_cfg, **pc, 'auto_tare_on_connect': False, 'auto_configure': False})
        if shared:
            c.set_shared_serial(shared)
        elif not c.connect():
            raise RuntimeError(f'Alicat connect failed for {key}: {c._last_status}')
        elif c._serial and c._owns_serial:
            shared = c._serial
        c.cancel_hold()
        ctrls[key] = c

    other_key = 'port_b' if port_key == 'port_a' else 'port_a'
    ctrl = ctrls[port_key]
    other = ctrls[other_key]
    other.set_pressure(SAFE_SP)

    lj = ca if port_key == 'port_a' else cb
    return ca, cb, ctrl, other, shared, lj


def _read_triplet(
    mensor: MensorReader,
    lj: LabJackController,
    ctrl: AlicatController,
    target: float,
    elapsed: float,
    port_key: str,
) -> TripletSample:
    mensor_psia = None
    mensor_raw = ''
    try:
        r = mensor.read_pressure(port_id=port_key)
        mensor_psia = r.pressure_psia
        ch = r.channel or 'active'
        raw = mensor.response_tail[-1] if mensor.response_tail else ''
        mensor_raw = f'{ch}:{raw}'
    except Exception as exc:
        mensor_raw = str(exc)

    alicat_psia = None
    status = ctrl.read_status()
    if status and status.pressure is not None:
        alicat_psia = status.pressure

    tr = lj.read_transducer()
    transducer_psia = tr.pressure if tr else None
    transducer_v = tr.voltage if tr else None

    return TripletSample(
        elapsed_s=elapsed,
        target_psia=target,
        mensor_psia=mensor_psia,
        alicat_psia=alicat_psia,
        transducer_psia=transducer_psia,
        transducer_v=transducer_v,
        mensor_raw=mensor_raw,
    )


def _filter_targets(targets: List[float], max_psia: float) -> List[float]:
    allowed = [t for t in targets if t <= max_psia + 1e-6]
    skipped = [t for t in targets if t > max_psia + 1e-6]
    if skipped:
        print(f'  Skipping targets above Mensor limit ({max_psia} PSIA): {skipped}')
    return allowed


def _wait_near_target(
    ctrl: AlicatController,
    lj: LabJackController,
    target: float,
    tolerance: float,
    timeout_s: float,
) -> tuple[bool, Optional[float]]:
    """Wait until Alicat absolute pressure is within tolerance of target."""
    t0 = time.perf_counter()
    last_p: Optional[float] = None
    while time.perf_counter() - t0 < timeout_s:
        status = ctrl.read_status()
        last_p = status.pressure if status else None
        if last_p is not None and abs(last_p - target) <= tolerance:
            return True, last_p
        time.sleep(0.5)
    return False, last_p


def run_static(
    port_key: str,
    targets: List[float],
    hold_s: float,
    rate_hz: float,
    mensor: MensorReader,
    ca: LabJackController,
    cb: LabJackController,
    ctrl: AlicatController,
    lj: LabJackController,
    other: AlicatController,
    max_psia: float,
) -> List[TripletSample]:
    addr = load_stinger_config()['hardware']['alicat'][port_key]['address']
    out: List[TripletSample] = []
    print(f'\n=== STATIC {port_key} ({PORT_SIDE.get(port_key, "?")}, Alicat {addr}) + Mensor (max {max_psia} PSIA) ===')
    print(f'{"Target":>7} {"Mensor":>9} {"Alicat":>9} {"Trans":>9} {"T-M":>8} {"T-A":>8} {"A-M":>8} {"Settled":>8}')

    for target in targets:
        use_vacuum = target < VACUUM_BELOW_PSI
        lj.set_solenoid(to_vacuum=use_vacuum)
        other.set_pressure(SAFE_SP)
        ctrl.cancel_hold()
        ctrl.set_ramp_rate(2.0)
        ctrl.set_pressure(target)
        settle_timeout = 90.0 if use_vacuum else 60.0
        ok, _ = _wait_near_target(ctrl, lj, target, tolerance=0.8, timeout_s=settle_timeout)
        time.sleep(2.0)

        batch: List[TripletSample] = []
        t0 = time.perf_counter()
        interval = 1.0 / max(rate_hz, 0.1)
        while time.perf_counter() - t0 < hold_s:
            batch.append(_read_triplet(mensor, lj, ctrl, target, time.perf_counter() - t0, port_key))
            time.sleep(interval)
        out.extend(batch)

        def mean_attr(name: str) -> Optional[float]:
            vals = [getattr(s, name) for s in batch if getattr(s, name) is not None]
            return statistics.mean(vals) if vals else None

        m = mean_attr('mensor_psia')
        a = mean_attr('alicat_psia')
        t = mean_attr('transducer_psia')
        tm = (t - m) if (t is not None and m is not None) else None
        ta = (t - a) if (t is not None and a is not None) else None
        am = (a - m) if (a is not None and m is not None) else None

        def fmt(v: Optional[float]) -> str:
            return f'{v:9.3f}' if v is not None else '      n/a'

        print(
            f'{target:7.1f} {fmt(m)} {fmt(a)} {fmt(t)} '
            f'{fmt(tm)} {fmt(ta)} {fmt(am)} {"yes" if ok else "no":>8}'
        )

    lj.set_solenoid(False)
    ctrl.set_pressure(SAFE_SP)
    return out


def write_csv(path: Path, samples: List[TripletSample], port_key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow([
            'port', 'elapsed_s', 'target_psia', 'mensor_psia', 'alicat_psia',
            'transducer_psia', 'transducer_v', 'mensor_raw',
        ])
        for s in samples:
            w.writerow([
                port_key,
                f'{s.elapsed_s:.3f}',
                f'{s.target_psia:.2f}',
                '' if s.mensor_psia is None else f'{s.mensor_psia:.4f}',
                '' if s.alicat_psia is None else f'{s.alicat_psia:.4f}',
                '' if s.transducer_psia is None else f'{s.transducer_psia:.4f}',
                '' if s.transducer_v is None else f'{s.transducer_v:.4f}',
                s.mensor_raw,
            ])


def cleanup(
    ca: LabJackController,
    cb: LabJackController,
    ctrl: AlicatController,
    other: AlicatController,
    shared: Any,
    mensor: MensorReader,
) -> None:
    ca.set_solenoid(False)
    cb.set_solenoid(False)
    try:
        ctrl.set_pressure(SAFE_SP)
        other.set_pressure(SAFE_SP)
    except Exception:
        pass
    ca.cleanup()
    cb.cleanup()
    mensor.close()
    if shared:
        AC._shared_serials.clear()
        try:
            shared.close()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description='Mensor + Alicat + transducer correlation')
    parser.add_argument('--port', choices=['port_a', 'port_b'], default='port_b',
                        help='Stinger port under test (default port_b = right)')
    parser.add_argument('--static', type=float, nargs='*', default=None,
                        help='Static setpoints PSIA (default: 5 10 14 18 22 26 30)')
    parser.add_argument('--hold', type=float, default=8.0, help='Hold seconds per point')
    parser.add_argument('--rate', type=float, default=2.0, help='Sample rate Hz during hold')
    parser.add_argument('--max-psia', type=float, default=MENSOR_MAX_PSIA_DEFAULT,
                        help='Do not command above this (Mensor safety limit)')
    parser.add_argument('--mensor-port', type=str, default=None)
    parser.add_argument(
        '--mensor-channel',
        type=str,
        default=None,
        help='Force Mensor channel A/B (overrides config channel / port_channels)',
    )
    parser.add_argument('--discover-mensor', action='store_true')
    parser.add_argument('--qc-config', type=Path, default=None, help='quality_cal_config.yaml')
    args = parser.parse_args()

    qc_path = args.qc_config or get_default_config_path()
    qc_config = load_qc_config(qc_path)
    mensor_cfg = dict(qc_config.get('hardware', {}).get('mensor', {}) or {})

    if args.discover_mensor:
        ali_ports = set()
        for key in ('port_a', 'port_b'):
            com = str(qc_config['hardware']['alicat'].get(key, {}).get('com_port', '')).strip()
            if not com:
                st = load_stinger_config()
                com = str(st['hardware']['alicat'].get(key, {}).get('com_port', '')).strip()
            if com:
                ali_ports.add(com)
        found = discover_mensor_port(qc_config, exclude_ports=ali_ports)
        if not found:
            print('Mensor discovery failed. Run: python scripts/mensor_read_test.py --list-ports')
            return 1
        mensor_cfg['port'] = found
        print(f'Mensor on {found}')

    if args.mensor_port:
        mensor_cfg['port'] = args.mensor_port
    if args.mensor_channel:
        mensor_cfg['channel'] = args.mensor_channel
        # Force this channel even when port_channels would map otherwise.
        mensor_cfg['port_channels'] = {
            'port_a': args.mensor_channel,
            'port_b': args.mensor_channel,
        }

    targets = args.static if args.static is not None else DEFAULT_STATIC
    targets = _filter_targets(sorted(targets), args.max_psia)
    if not targets:
        print('No targets within Mensor limit.')
        return 1

    resolved_ch = (
        args.mensor_channel
        or (mensor_cfg.get('port_channels') or {}).get(args.port)
        or mensor_cfg.get('channel')
        or 'active'
    )
    print('=' * 72)
    print('Triplet correlation (Mensor reference)')
    print(f'  Stinger port: {args.port} ({PORT_SIDE.get(args.port, "unknown")} physical)')
    print(f'  Mensor COM:   {mensor_cfg.get("port")} @ {mensor_cfg.get("baudrate", 57600)}')
    print(f'  Mensor chan:  {resolved_ch}')
    print(f'  Max PSIA:     {args.max_psia} (Mensor safety cap)')
    print('=' * 72)

    mensor = MensorReader(mensor_cfg)
    if not mensor.connect():
        print(f'Mensor connect failed: {mensor.status}')
        return 1

    ca, cb, ctrl, other, shared, lj = _setup_stinger(args.port)
    samples: List[TripletSample] = []
    try:
        samples = run_static(
            args.port, targets, args.hold, args.rate,
            mensor, ca, cb, ctrl, lj, other, args.max_psia,
        )
    finally:
        cleanup(ca, cb, ctrl, other, shared, mensor)

    ts = time.strftime('%Y%m%d_%H%M%S')
    out = PROJECT_ROOT / 'scripts' / 'data' / f'mensor_triplet_{args.port}_{ts}.csv'
    write_csv(out, samples, args.port)
    print(f'\nCSV: {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
