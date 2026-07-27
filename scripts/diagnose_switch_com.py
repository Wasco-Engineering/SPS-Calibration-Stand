#!/usr/bin/env python3
"""Diagnose PTP switch COM drive + sense pin response for a part/sequence."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import load_config
from app.database.session import close_database, initialize_database
from app.hardware.port import PortId, PortManager
from app.services.ptp_service import load_ptp_from_db
from app.services.ptp_switch_resolver import db9_pin_to_dio, resolve_ptp_switch_config

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def _read_raw(port, dio: int) -> int | None:
    values = port.daq.read_dio_values(max_dio=22)
    if not values:
        return None
    return int(values.get(dio, -1))


def diagnose_port(pm: PortManager, port_id: str, ptp: dict) -> int:
    port = pm.get_port(PortId(port_id))
    if port is None:
        logger.error('%s: port missing', port_id)
        return 1

    resolution = resolve_ptp_switch_config(
        ptp_params=ptp,
        port_id=port_id,
        port_config=port._labjack_config,
    )
    logger.info('%s: resolution %s', port_id, resolution.summary)
    if resolution.warnings:
        for warning in resolution.warnings:
            logger.warning('%s: %s', port_id, warning)
    if not resolution.is_valid:
        logger.error('%s: invalid resolution: %s', port_id, '; '.join(resolution.errors))
        return 1

    if not port.configure_from_ptp(ptp):
        logger.error('%s: configure_from_ptp failed', port_id)
        return 1

    com_dio = resolution.drive_dio
    sense_dio = resolution.no_dio if resolution.derive_nc_from_no else resolution.nc_dio
    configured_com_state = int(port.daq.switch_com_state)
    logger.info(
        '%s: drive_role=%s drive_dio=%s configured_com_state=%s sense_dio=%s '
        'active_low=%s derive_nc_from_no=%s derive_no_from_nc=%s',
        port_id,
        resolution.drive_role,
        com_dio,
        configured_com_state,
        sense_dio,
        port.daq.switch_active_low,
        port.daq.switch_nc_derived_from_no,
        port.daq.switch_no_derived_from_nc,
    )

    if com_dio is None or sense_dio is None:
        logger.error('%s: missing COM/sense DIO', port_id)
        return 1

    # Confirm COM is an output at the configured level.
    port.daq.set_dio_direction(com_dio, True, configured_com_state)
    time.sleep(0.05)
    com_raw = _read_raw(port, com_dio)
    sense_raw = _read_raw(port, sense_dio)
    switch = port.daq.read_switch_state()
    logger.info(
        '%s: after configure COM_raw=%s sense_raw=%s NO=%s NC=%s activated=%s',
        port_id,
        com_raw,
        sense_raw,
        None if switch is None else switch.no_active,
        None if switch is None else switch.nc_active,
        None if switch is None else switch.switch_activated,
    )
    if com_raw is not None and com_raw != configured_com_state:
        logger.warning(
            '%s: COM DIO%s readback %s != configured drive %s '
            '(may indicate short, wiring, or pin not actually driven)',
            port_id,
            com_dio,
            com_raw,
            configured_com_state,
        )

    # Toggle COM and watch sense ΓÇö with a switch present the sense line should move.
    responses: list[tuple[int, int | None, int | None]] = []
    for state in (0, 1, 0, 1, configured_com_state):
        port.daq.set_dio_direction(com_dio, True, state)
        time.sleep(0.08)
        responses.append((state, _read_raw(port, com_dio), _read_raw(port, sense_dio)))
    logger.info('%s: COM toggle responses (drive, com_raw, sense_raw)=%s', port_id, responses)

    sense_values = {sense for _drive, _com, sense in responses if sense is not None}
    if len(sense_values) <= 1:
        logger.warning(
            '%s: sense DIO%s did not change while toggling COM ΓÇö '
            'no closed path through DUT, or sense not wired to this pin',
            port_id,
            sense_dio,
        )
        rc = 2
    else:
        logger.info('%s: sense responded to COM toggle (good continuity path)', port_id)
        rc = 0

    # Restore configured COM state.
    port.daq.set_dio_direction(com_dio, True, configured_com_state)
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--part', default='17021')
    parser.add_argument('--sequence', default='399')
    parser.add_argument('--port', choices=['port_a', 'port_b', 'both'], default='both')
    args = parser.parse_args()

    config = load_config()
    if not initialize_database(config.get('database', {})):
        return 1
    ptp = load_ptp_from_db(args.part, args.sequence)
    if not ptp:
        logger.error('No PTP for %s/%s', args.part, args.sequence)
        return 1

    logger.info(
        'PTP terminals COM=%s NO=%s NC=%s',
        ptp.get('CommonTerminal'),
        ptp.get('NormallyOpenTerminal'),
        ptp.get('NormallyClosedTerminal'),
    )
    for port_key in ('port_a', 'port_b'):
        pin = 4
        logger.info('%s DB9 pin %s -> DIO%s', port_key, pin, db9_pin_to_dio(port_key, pin))

    pm = PortManager(config)
    pm.initialize_ports()
    if not pm.connect_all(safe_idle_on_connect=True):
        logger.error('Hardware connect failed')
        return 1

    ports = ['port_a', 'port_b'] if args.port == 'both' else [args.port]
    worst = 0
    try:
        for port_id in ports:
            worst = max(worst, diagnose_port(pm, port_id, ptp))
    finally:
        pm.disconnect_all(restore_safe_state=True)
        close_database()
    return worst


if __name__ == '__main__':
    raise SystemExit(main())
