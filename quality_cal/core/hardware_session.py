"""Connect quality-cal hardware without the main UI shell."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from app.hardware.port import Port, PortManager
from quality_cal.core.hardware_checks import evaluate_labjack_port_check
from quality_cal.core.hardware_discovery import (
    discover_alicat_assignments,
    discover_labjack_target,
    discover_mensor_port,
)
from quality_cal.core.mensor_reader import MensorReader

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class HardwareCheck:
    name: str
    ok: bool
    detail: str


@dataclass(slots=True)
class HardwareSession:
    """Initialized ports + Mensor for calibration runners."""

    config: dict[str, Any]
    port_manager: PortManager
    mensor_reader: MensorReader
    discovery_note: str
    checks: tuple[HardwareCheck, ...]

    @property
    def overall_ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def get_port(self, port_id: str) -> Optional[Port]:
        return self.port_manager.get_port(port_id)

    def cleanup(self) -> None:
        try:
            self.mensor_reader.close()
        except Exception as exc:
            logger.warning('Mensor close failed: %s', exc)
        try:
            self.port_manager.disconnect_all()
        except Exception as exc:
            logger.warning('Port disconnect failed: %s', exc)


def _serial_auto_discovery_enabled(config: dict[str, Any]) -> bool:
    quality_cfg = config.get('quality', {}) or {}
    discovery_cfg = quality_cfg.get('hardware_discovery', {}) or {}
    return bool(discovery_cfg.get('enable_serial_auto_discovery', True))


def apply_discovered_hardware_assignments(config: dict[str, Any]) -> str:
    """Update config in-place from LabJack / serial discovery. Returns probe detail text."""
    hardware_cfg = config.setdefault('hardware', {})
    labjack_cfg = hardware_cfg.setdefault('labjack', {})
    alicat_cfg = hardware_cfg.setdefault('alicat', {})
    port_a_cfg = alicat_cfg.setdefault('port_a', {})
    port_b_cfg = alicat_cfg.setdefault('port_b', {})
    mensor_cfg = hardware_cfg.setdefault('mensor', {})

    labjack_probe = discover_labjack_target(config)
    detail = str(labjack_probe.get('detail', 'LabJack discovery unavailable.'))
    if bool(labjack_probe.get('found', False)):
        labjack_cfg['device_type'] = str(
            labjack_probe.get('device_type', labjack_cfg.get('device_type', 'T7'))
        )
        labjack_cfg['connection_type'] = str(
            labjack_probe.get('connection_type', labjack_cfg.get('connection_type', 'USB'))
        )
        labjack_cfg['identifier'] = str(
            labjack_probe.get('identifier', labjack_cfg.get('identifier', 'ANY'))
        )

    if _serial_auto_discovery_enabled(config):
        for logical_port, discovered_port in discover_alicat_assignments(config).items():
            target_cfg = port_a_cfg if logical_port == 'port_a' else port_b_cfg
            target_cfg['com_port'] = discovered_port

        discovered_mensor = discover_mensor_port(
            config,
            exclude_ports={
                str(port_a_cfg.get('com_port', '')).strip(),
                str(port_b_cfg.get('com_port', '')).strip(),
            },
        )
        if discovered_mensor:
            mensor_cfg['port'] = discovered_mensor

    return detail


def connect_hardware_session(config: dict[str, Any]) -> HardwareSession:
    """Discover assignments, connect ports and Mensor, and verify readings."""
    discovery_note = apply_discovered_hardware_assignments(config)

    port_manager = PortManager(config)
    port_manager.initialize_ports()
    port_manager.connect_all()

    mensor_cfg = config.get('hardware', {}).get('mensor', {}) or {}
    mensor_reader = MensorReader(mensor_cfg)
    if not mensor_reader.connect():
        logger.warning('Mensor connect returned false: %s', mensor_reader.status)

    checks: list[HardwareCheck] = []
    for port_id, label in (('port_a', 'Left'), ('port_b', 'Right')):
        port = port_manager.get_port(port_id)
        if port is None:
            checks.append(
                HardwareCheck(f'{port_id}_hardware', False, 'Port is not configured.'),
            )
            continue

        labjack_result = evaluate_labjack_port_check(
            port_id=port_id,
            port=port,
            config=config,
            probe_detail=discovery_note,
        )
        checks.append(
            HardwareCheck(f'{port_id}_labjack', labjack_result.ok, labjack_result.detail),
        )

        alicat_status = port.alicat.get_status()
        alicat_reading = port.alicat.read_status()
        if alicat_reading is None and not bool(alicat_status.get('connected', False)):
            port.alicat.connect()
            alicat_status = port.alicat.get_status()
            alicat_reading = port.alicat.read_status()
        alicat_ok = alicat_reading is not None
        if alicat_reading is None:
            al_detail = (
                f"{alicat_status.get('status', 'Unknown')} | "
                f"Port={alicat_status.get('port')} Address={alicat_status.get('address')}"
            )
        else:
            al_detail = (
                f"{alicat_status.get('status', 'Unknown')} | "
                f'Pressure={alicat_reading.pressure:.3f} psia '
                f'Setpoint={alicat_reading.setpoint:.3f}'
            )
        checks.append(HardwareCheck(f'{port_id}_alicat', alicat_ok, al_detail))

    mensor_ok = False
    mensor_detail = mensor_reader.status
    if mensor_reader.status == 'Connected (simulated)':
        mensor_detail = 'Simulated Mensor — install pyserial and connect hardware'
    elif mensor_reader.status == 'Connected':
        try:
            reading = mensor_reader.read_pressure()
            mensor_ok = True
            mensor_detail = f'Connected | Pressure={reading.pressure_psia:.3f} psia'
        except Exception as exc:
            mensor_detail = f'Connected | Read failed: {exc}'
    checks.append(HardwareCheck('mensor', mensor_ok, mensor_detail))

    session = HardwareSession(
        config=config,
        port_manager=port_manager,
        mensor_reader=mensor_reader,
        discovery_note=discovery_note,
        checks=tuple(checks),
    )
    for check in checks:
        level = logging.INFO if check.ok else logging.ERROR
        logger.log(level, '%s: %s — %s', check.name, 'OK' if check.ok else 'FAIL', check.detail)
    return session
