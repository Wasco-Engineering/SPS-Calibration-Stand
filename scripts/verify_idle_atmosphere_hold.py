#!/usr/bin/env python3
"""Verify idle atmosphere holds with DUTs installed (post-vent monitor)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import load_config
from app.hardware.port import PortManager

BARO = 14.7


def main() -> int:
    cfg = load_config()
    pm = PortManager(cfg)
    pm.initialize_ports()
    if not pm.connect_all(safe_idle_on_connect=True):
        return 1

    print('Connected and idle vent applied. Monitoring 30s per port...\n')
    for port_id, port in pm.ports.items():
        print(f'=== {port_id.value} ===')
        for i in range(15):
            r = port.read_all()
            p = r.alicat.pressure if r.alicat else None
            sp = r.alicat.setpoint if r.alicat else None
            raw = getattr(r.alicat, 'raw_response', None) if r.alicat else None
            print(f'  t={i * 2:2d}s  P={p:.3f}  SP={sp}  {raw!r}')
            time.sleep(2.0)
        print()

    pm.disconnect_all(restore_safe_state=True)
    print('Disconnected with safe idle lock.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
