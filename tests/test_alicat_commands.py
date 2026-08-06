"""Focused Alicat command fallback behavior tests."""

from __future__ import annotations

import threading
import time

from app.hardware.alicat import AlicatController, AlicatReading
from app.hardware.port import Port, PortId


def test_alicat_set_pressure_uses_compact_fallback_command() -> None:
    controller = AlicatController({'address': 'A'})
    sent: list[str] = []

    def fake_send(command: str) -> str:
        sent.append(command)
        return '?' if len(sent) == 1 else 'A'

    controller._send_command = fake_send  # type: ignore[method-assign]
    assert controller.set_pressure(7.0434)
    assert sent[0].startswith('S ')
    assert sent[1].startswith('S') and not sent[1].startswith('S ')


def test_alicat_set_pressure_no_fallback_when_acknowledged() -> None:
    controller = AlicatController({'address': 'A'})
    sent: list[str] = []
    controller._send_command = lambda command: sent.append(command) or 'A'  # type: ignore[method-assign]
    assert controller.set_pressure(7.0)
    assert len(sent) == 1


def test_alicat_configure_units_from_ptp_verifies_readback_before_success() -> None:
    controller = AlicatController({'address': 'A'})
    controller._is_connected = True
    commands: list[str] = []

    def fake_send(command: str) -> str:
        commands.append(command)
        if command == 'DCU 2':
            return 'A 10' if commands.count('DCU 2') == 1 else 'A 13'
        if command == 'DCU 2 13':
            return 'A'
        return 'A'

    controller._send_command = fake_send  # type: ignore[method-assign]
    assert controller.configure_units_from_ptp('21')
    assert controller._display_units_label == 'Torr'
    assert 'DCU 2 13' in commands


def test_alicat_torr_units_ignore_stale_psi_command_preference() -> None:
    controller = AlicatController({'address': 'A'})
    controller._pressure_units_value = 13
    controller._update_display_units_label()
    controller._prefer_psi_commands = True
    sent: list[str] = []

    controller._send_command = lambda command: sent.append(command) or 'A'  # type: ignore[method-assign]

    assert controller.set_pressure(1.5)
    assert controller.set_ramp_rate(0.0967)

    expected_setpoint = controller._psi_to_display(1.5)
    expected_rate = controller._psi_to_display(0.0967)
    assert sent[0] == f'S {expected_setpoint:.2f}'
    assert sent[1] == f'SR {expected_rate:.4f} 4'
    assert not controller._prefer_psi_commands


def test_alicat_serial_uses_configured_identity_without_hardware(monkeypatch) -> None:
    monkeypatch.setattr('app.hardware.alicat.SERIAL_AVAILABLE', False)
    controller = AlicatController({'address': 'A', 'expected_serial_number': '601126'})

    assert controller.refresh_serial_number() == '601126'
    assert controller.serial_number == '601126'


def test_auto_tare_on_connect_does_not_exhaust_at_atmosphere() -> None:
    controller = AlicatController({
        'address': 'A',
        'auto_tare_on_connect': True,
        'auto_tare_max_delta_psi': 0.5,
        'auto_tare_delay_s': 0,
    })
    controller._is_connected = True
    sent: list[str] = []
    tared = {'value': False}

    def fake_send(command: str) -> str:
        sent.append(command)
        return 'A'

    controller._send_command = fake_send  # type: ignore[method-assign]
    controller.read_status = lambda: AlicatReading(  # type: ignore[method-assign]
        pressure=14.68,
        setpoint=14.7,
        barometric_pressure=14.7,
        timestamp=1.0,
    )

    def fake_tare() -> bool:
        tared['value'] = True
        return True

    controller.tare = fake_tare  # type: ignore[method-assign]
    controller._maybe_auto_tare()
    assert 'E' not in sent
    assert tared['value'] is True


def test_auto_tare_on_connect_skips_when_not_at_atmosphere() -> None:
    controller = AlicatController({
        'address': 'A',
        'auto_tare_on_connect': True,
        'auto_tare_max_delta_psi': 0.5,
        'auto_tare_delay_s': 0,
    })
    controller._is_connected = True
    sent: list[str] = []

    controller._send_command = lambda command: sent.append(command) or 'A'  # type: ignore[method-assign]
    controller.read_status = lambda: AlicatReading(  # type: ignore[method-assign]
        pressure=0.5,
        setpoint=0.5,
        barometric_pressure=14.7,
        timestamp=1.0,
    )
    controller.tare = lambda: False  # type: ignore[method-assign]
    controller._maybe_auto_tare()
    assert sent == []


def test_background_read_cannot_interleave_resume_and_setpoint() -> None:
    controller = AlicatController({'address': 'A'})
    read_entered = threading.Event()
    release_read = threading.Event()
    commands: list[str] = []

    def send(command: str) -> str:
        commands.append(command)
        if command == '':
            read_entered.set()
            assert release_read.wait(timeout=1.0)
            return 'A +014.70 +010.00 EXH'
        return 'A'

    controller._send_command = send  # type: ignore[method-assign]
    reader = threading.Thread(target=controller.read_status)
    setter = threading.Thread(target=lambda: controller.resume_and_set_pressure(7.5))

    reader.start()
    assert read_entered.wait(timeout=1.0)
    setter.start()
    time.sleep(0.02)
    assert commands == ['']

    release_read.set()
    reader.join(timeout=1.0)
    setter.join(timeout=1.0)
    assert commands[:3] == ['', 'C', 'S 7.50']


def test_port_configures_switch_pins_from_ptp() -> None:
    port = Port(
        PortId.PORT_B,
        {
            'device_type': 'T7',
            'connection_type': 'USB',
            'identifier': 'ANY',
            'switch_com_state': 0,
            'switch_sensed_db9_pins': [3],
        },
        {'address': 'B'},
        {},
    )
    calls = []

    def _record_configure_di(*args, **kwargs):
        calls.append((args, kwargs))

    port.daq.configure_di_pins = _record_configure_di  # type: ignore[method-assign]
    ok = port.configure_from_ptp(
        {
            'NormallyOpenTerminal': '3',
            'NormallyClosedTerminal': '1',
            'CommonTerminal': '4',
            'PressureReference': 'Absolute',
        },
    )
    assert ok
    assert calls == [((11, 11, 12), {'com_state': 0})]
    assert port.daq.switch_nc_derived_from_no
