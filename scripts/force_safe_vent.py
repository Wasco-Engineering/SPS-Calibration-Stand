#!/usr/bin/env python3
"""Force both ports to atmosphere (bleed DUT if installed) + Alicat exhaust."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import load_config
from app.hardware.labjack import _solenoid_state_path
from app.hardware.port import PortManager

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def main() -> int:
    state_path = _solenoid_state_path()
    if state_path.exists():
        logger.info('Persisted solenoid state before: %s', state_path.read_text(encoding='utf-8').strip())

    cfg = load_config()
    pm = PortManager(cfg)
    pm.initialize_ports()
    if not pm.connect_all(safe_idle_on_connect=True):
        logger.error('Hardware connect failed')
        return 1

    for port_id, port in pm.ports.items():
        reading = port.read_all()
        alicat = reading.alicat
        pressure = alicat.pressure if alicat else None
        setpoint = alicat.setpoint if alicat else None
        raw = getattr(alicat, 'raw_response', None) if alicat else None
        dio = port.daq.solenoid_dio
        logger.info(
            '%s: idle DIO%s=atmosphere P=%s SP=%s raw=%s',
            port_id.value,
            dio,
            pressure,
            setpoint,
            raw,
        )

    pm.disconnect_all(restore_safe_state=True)

    if state_path.exists():
        logger.info('Persisted solenoid state after: %s', state_path.read_text(encoding='utf-8').strip())
    logger.info('Safe vent complete.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
