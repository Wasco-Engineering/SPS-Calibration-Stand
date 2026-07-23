"""Locked barometric pressure for gauge test runs."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.hardware.alicat import AlicatReading
from app.hardware.port import PortReading
from app.services.ptp_service import TestSetup
from app.services.test_executor import TestExecutor


def _setup() -> TestSetup:
    return TestSetup(
        part_id='SPS01438-02',
        sequence_id='300',
        units_code='19',
        units_label='mmHg @ 0 C',
        activation_direction='Decreasing',
        activation_target=75.0,
        pressure_reference='gauge',
        terminals={},
        bands={
            'decreasing': {'lower': 73.0, 'upper': 77.0},
            'increasing': {'lower': float('-inf'), 'upper': 145.0},
            'reset': {'lower': float('-inf'), 'upper': float('inf')},
        },
        raw={},
    )


def test_executor_locks_baro_to_site_config_for_gauge_run() -> None:
    live_values = {'baro': 13.20}

    def _live_baro(_port_id: str) -> float:
        return float(live_values['baro'])

    port = MagicMock()
    port.alicat = MagicMock()
    port.alicat.configure_units_from_ptp.return_value = True
    port.daq = MagicMock()
    port.daq.switch_nc_derived_from_no = True
    port.daq.switch_com_state = 0
    port.daq.switch_active_low = True

    executor = TestExecutor(
        port_id='port_a',
        port=port,
        test_setup=_setup(),
        config={
            'hardware': {
                'labjack': {'local_barometric_psi': 13.53, 'port_a': {}},
                'alicat': {'port_a': {}},
            },
            'control': {},
        },
        get_latest_reading=lambda _pid: PortReading(
            alicat=AlicatReading(pressure=13.98, setpoint=13.98, timestamp=1.0),
            timestamp=1.0,
        ),
        get_barometric_psi=_live_baro,
    )

    executor._lock_run_barometric_psi()
    assert executor._get_barometric_psi('port_a') == 13.53

    # Live baro / residual line pressure must not move the lock.
    live_values['baro'] = 13.05
    assert executor._get_barometric_psi('port_a') == 13.53
    assert executor._absolute_to_test_reference(14.98) == pytest.approx(1.45, abs=1e-6)
