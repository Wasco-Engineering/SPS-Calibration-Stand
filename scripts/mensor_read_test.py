#!/usr/bin/env python3
"""Read pressure from the Mensor reference (connect, probe, live stream).

Uses settings from quality_cal_config.yaml unless overridden.

Examples:
    python scripts/mensor_read_test.py
    python scripts/mensor_read_test.py --discover
    python scripts/mensor_read_test.py --port COM10 --count 20 --interval 0.5
    python scripts/mensor_read_test.py --both
    python scripts/mensor_read_test.py --channel B
    python scripts/mensor_read_test.py --list-ports
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Set

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from quality_cal.config import get_default_config_path, load_config
from quality_cal.core.hardware_discovery import discover_mensor_port
from quality_cal.core.mensor_reader import MensorReader

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)


def _load_mensor_config(config_path: Optional[Path]) -> Dict[str, Any]:
    path = config_path or get_default_config_path()
    config = load_config(path)
    return config.get('hardware', {}).get('mensor', {}) or {}


def _exclude_alicat_ports(config: Dict[str, Any]) -> Set[str]:
    ali = config.get('hardware', {}).get('alicat', {})
    out: Set[str] = set()
    for key in ('port_a', 'port_b'):
        pc = ali.get(key, {})
        if isinstance(pc, dict):
            com = str(pc.get('com_port', '')).strip()
            if com:
                out.add(com)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description='Mensor serial read test')
    parser.add_argument('--config', type=Path, default=None, help='quality_cal_config.yaml path')
    parser.add_argument('--port', type=str, default=None, help='COM port (e.g. COM10)')
    parser.add_argument('--baudrate', type=int, default=None)
    parser.add_argument('--discover', action='store_true', help='Auto-discover Mensor COM port')
    parser.add_argument('--list-ports', action='store_true', help='List serial ports and exit')
    parser.add_argument('--count', type=int, default=10, help='Number of readings (0 = infinite)')
    parser.add_argument('--interval', type=float, default=1.0, help='Seconds between reads')
    parser.add_argument(
        '--channel',
        type=str,
        default=None,
        help='Mensor transducer channel (A/B/BARO). Overrides config channel.',
    )
    parser.add_argument(
        '--both',
        action='store_true',
        help='Read both A and B channels each sample (EMENSOR 600 dual transducer)',
    )
    args = parser.parse_args()

    if args.list_ports:
        ports = MensorReader.list_available_ports()
        print('Available serial ports:')
        for p in ports:
            print(f'  {p}')
        if not ports:
            print('  (none — is pyserial installed?)')
        return 0

    config_path = args.config or get_default_config_path()
    full_config = load_config(config_path)
    mensor_cfg = dict(_load_mensor_config(config_path))
    if args.port:
        mensor_cfg['port'] = args.port
    if args.baudrate:
        mensor_cfg['baudrate'] = args.baudrate
    if args.channel:
        mensor_cfg['channel'] = args.channel

    if args.discover:
        excluded = _exclude_alicat_ports(full_config)
        found = discover_mensor_port(full_config, exclude_ports=excluded)
        if not found:
            print('Mensor auto-discovery failed. Try --list-ports and --port COMx')
            return 1
        mensor_cfg['port'] = found
        print(f'Auto-discovered Mensor on {found}')

    port = str(mensor_cfg.get('port', '')).strip()
    if not port:
        print('No Mensor port configured. Use --port COMx or --discover')
        return 1

    print('=' * 60)
    print('Mensor read test')
    print(f'  Config:  {config_path}')
    print(f'  Port:    {port} @ {mensor_cfg.get("baudrate", 57600)} baud')
    print(f'  Channel: {mensor_cfg.get("channel") or "active (?)"}')
    if mensor_cfg.get('port_channels'):
        print(f'  Port map:{mensor_cfg.get("port_channels")}')
    print('=' * 60)

    reader = MensorReader(mensor_cfg)
    if not reader.connect():
        print(f'Connect failed: {reader.status}')
        return 1
    print(f'Status: {reader.status}')

    try:
        n = 0
        while args.count == 0 or n < args.count:
            try:
                if args.both:
                    both = reader.read_channels(('A', 'B'))
                    a = both.get('A')
                    b = both.get('B')
                    print(
                        f'  [{n + 1:3d}] A={a.pressure_psia:10.4f} PSIA  '
                        f'B={b.pressure_psia:10.4f} PSIA'
                        if a and b
                        else f'  [{n + 1:3d}] partial={ {k: v.pressure_psia for k, v in both.items()} }'
                    )
                else:
                    r = reader.read_pressure()
                    raw = reader.response_tail[-1] if reader.response_tail else ''
                    ch = r.channel or 'active'
                    print(
                        f'  [{n + 1:3d}] {r.pressure_psia:10.4f} PSIA  '
                        f'ch={ch}  raw={raw!r}'
                    )
            except Exception as exc:
                print(f'  [{n + 1:3d}] READ ERROR: {exc}')
            n += 1
            if args.count == 0 or n < args.count:
                time.sleep(max(0.05, args.interval))
    except KeyboardInterrupt:
        print('\nStopped.')
    finally:
        reader.close()
        print(f'Closed ({reader.status})')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
