"""Shared hardware readiness checks for the quality calibration app."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.core.config import is_transducer_installed
from app.hardware.port import Port


@dataclass(slots=True)
class LabJackCheckResult:
    ok: bool
    detail: str
    transducer_psia: Optional[float] = None


def _ensure_labjack_configured(port: Port) -> tuple[dict[str, Any], Optional[float]]:
    """Return (status, transducer_reading), configuring the DAQ once if needed."""
    labjack_status = port.daq.get_status()
    transducer_reading = port.daq.read_transducer()
    driver_loaded = bool(labjack_status.get('driver_loaded', False))
    if (
        driver_loaded
        and transducer_reading is None
        and not bool(labjack_status.get('configured', False))
    ):
        port.daq.configure()
        labjack_status = port.daq.get_status()
        transducer_reading = port.daq.read_transducer()
    return labjack_status, (
        float(transducer_reading.pressure)
        if transducer_reading is not None and transducer_reading.pressure is not None
        else None
    )


def evaluate_labjack_port_check(
    *,
    port_id: str,
    port: Port,
    config: dict[str, Any],
    probe_detail: str = '',
) -> LabJackCheckResult:
    """Check LabJack readiness; skip transducer when ``transducer_installed: false``."""
    labjack_status, transducer_psia = _ensure_labjack_configured(port)
    driver_loaded = bool(labjack_status.get('driver_loaded', False))
    simulated = bool(labjack_status.get('simulated', False))
    configured = bool(labjack_status.get('configured', False))
    transducer_expected = is_transducer_installed(config, port_id)
    status_text = str(labjack_status.get('status', 'Unknown'))

    if not driver_loaded:
        return LabJackCheckResult(
            ok=False,
            detail=(
                f'{status_text} | '
                'LabJack driver missing: install the LabJack LJM driver.'
            ),
        )
    if simulated:
        return LabJackCheckResult(
            ok=False,
            detail=(
                f'{status_text} | '
                'Simulated only — allow_simulated_hardware is not valid for production cal.'
            ),
        )

    if transducer_expected:
        ok = transducer_psia is not None
        if not ok:
            detail = f'{status_text} | {probe_detail or "Transducer read failed."}'
        else:
            detail = f'{status_text} | Transducer={transducer_psia:.3f} psia'
        return LabJackCheckResult(ok=ok, detail=detail, transducer_psia=transducer_psia)

    ok = configured
    if ok:
        detail = (
            f'{status_text} | Transducer not installed — '
            'LabJack OK for solenoid/switch (Alicat-only mode).'
        )
    else:
        detail = f'{status_text} | {probe_detail or "LabJack not configured."}'
    return LabJackCheckResult(ok=ok, detail=detail, transducer_psia=transducer_psia)
