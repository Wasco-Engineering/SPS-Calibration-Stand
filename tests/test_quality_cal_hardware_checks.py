"""Tests for quality-cal hardware checks with optional transducers."""

from __future__ import annotations

from unittest.mock import MagicMock

from quality_cal.core.hardware_checks import evaluate_labjack_port_check


def _port(*, configured: bool = True, transducer_psia: float | None = 14.7) -> MagicMock:
    port = MagicMock()
    reading = None
    if transducer_psia is not None:
        reading = MagicMock()
        reading.pressure = transducer_psia
    port.daq.get_status.return_value = {
        'driver_loaded': True,
        'simulated': False,
        'configured': configured,
        'status': 'Configured',
    }
    port.daq.read_transducer.return_value = reading
    return port


def test_labjack_check_requires_transducer_when_installed() -> None:
    config = {
        'hardware': {
            'labjack': {
                'port_a': {'transducer_installed': True},
            },
        },
    }
    result = evaluate_labjack_port_check(
        port_id='port_a',
        port=_port(transducer_psia=None),
        config=config,
    )
    assert result.ok is False


def test_labjack_check_passes_without_transducer_when_not_installed() -> None:
    config = {
        'hardware': {
            'labjack': {
                'port_a': {'transducer_installed': False},
            },
        },
    }
    result = evaluate_labjack_port_check(
        port_id='port_a',
        port=_port(transducer_psia=None),
        config=config,
    )
    assert result.ok is True
    assert 'Alicat-only' in result.detail
