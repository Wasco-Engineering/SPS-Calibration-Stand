"""Unit tests for Port and PortManager using fake hardware."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import pytest

import app.hardware.port as port_module
from app.hardware.alicat import AlicatReading
from app.hardware.labjack import SwitchState, TransducerReading
from app.hardware.port import Port, PortId, PortManager, PortReading


@dataclass
class _FakeLabJackController:
    config: dict[str, Any]

    def __post_init__(self) -> None:
        self.pressure_reference = str(self.config.get('transducer_reference', 'absolute')).lower()
        self.switch_com_state = int(self.config.get('switch_com_state', 1))
        self.switch_nc_derived_from_no = bool(self.config.get('switch_nc_derived_from_no', False))
        self.switch_no_derived_from_nc = bool(self.config.get('switch_no_derived_from_nc', False))
        self.solenoid_calls: list[bool] = []
        self.configure_di_calls: list[tuple[int, int, int | None, int | None]] = []
        self.next_pressure = 0.0
        self.next_switch_activated = False
        self.reset_filter_calls = 0

    def configure(self) -> bool:
        return True

    def configure_di_pins(
        self, no_pin: int, nc_pin: int, com_pin: int | None = None, com_state: int | None = None
    ) -> None:
        self.configure_di_calls.append((no_pin, nc_pin, com_pin, com_state))

    def set_pressure_reference(self, reference: str) -> None:
        self.pressure_reference = reference.lower()

    def read_transducer(self) -> TransducerReading:
        return TransducerReading(
            voltage=2.5,
            pressure=self.next_pressure,
            pressure_raw=self.next_pressure,
            pressure_reference=self.pressure_reference,
            timestamp=1.0,
        )

    def read_switch_state(self) -> SwitchState:
        return SwitchState(
            no_active=self.next_switch_activated,
            nc_active=not self.next_switch_activated,
            timestamp=1.0,
        )

    def read_dio_values(self, max_dio: int = 22) -> dict[int, int]:
        return {i: 0 for i in range(max_dio + 1)}

    def set_solenoid(self, to_vacuum: bool) -> bool:
        self.solenoid_calls.append(to_vacuum)
        return True

    def set_solenoid_safe(self) -> bool:
        self.solenoid_calls.append(False)
        return True

    def reset_filter(self) -> None:
        self.reset_filter_calls += 1

    def cleanup(self, *, preserve_solenoid_state: bool = False) -> None:
        return None

    def get_status(self) -> dict[str, Any]:
        return {'configured': True}


class _FakeAlicatController:
    def __init__(self, _config: dict[str, Any]) -> None:
        self.connected = False
        self.next_reading = AlicatReading(
            pressure=14.7,
            setpoint=14.7,
            timestamp=1.0,
            gauge_pressure=0.0,
            barometric_pressure=14.7,
        )
        self.hold_calls = 0
        self.hold_closed: bool | None = None
        self.exhaust_calls = 0
        self.cancel_hold_calls = 0
        self.disconnect_calls = 0
        self.set_pressure_calls: list[float] = []

    def connect(self) -> bool:
        self.connected = True
        return True

    def read_status(self) -> AlicatReading:
        return self.next_reading

    def set_pressure(self, setpoint: float) -> bool:
        self.set_pressure_calls.append(float(setpoint))
        return True

    def set_ramp_rate(self, _rate: float) -> bool:
        return True

    def cancel_hold(self) -> bool:
        self.cancel_hold_calls += 1
        return True

    def exhaust(self) -> bool:
        self.exhaust_calls += 1
        # Mirror real Alicat: EXH bit appears on the status line.
        raw = self.next_reading.raw_response or ''
        if 'EXH' not in raw.upper():
            self.next_reading = AlicatReading(
                pressure=self.next_reading.pressure,
                setpoint=self.next_reading.setpoint,
                timestamp=self.next_reading.timestamp,
                gauge_pressure=self.next_reading.gauge_pressure,
                barometric_pressure=self.next_reading.barometric_pressure,
                pressure_raw=self.next_reading.pressure_raw,
                raw_response=(raw + ' EXH').strip() if raw else 'A +014.70 +014.70 EXH',
            )
        return True

    def hold_valve(self, closed: bool = False) -> bool:
        self.hold_calls += 1
        self.hold_closed = closed
        return True

    def disconnect(self) -> None:
        self.disconnect_calls += 1

    def get_status(self) -> dict[str, Any]:
        return {'connected': self.connected}


def _make_port(
    monkeypatch: Any,
    *,
    labjack_overrides: dict[str, Any] | None = None,
    solenoid_cfg: dict[str, Any] | None = None,
) -> Port:
    monkeypatch.setattr(port_module, 'LabJackController', _FakeLabJackController)
    monkeypatch.setattr(port_module, 'AlicatController', _FakeAlicatController)
    labjack_config = {
        'switch_sensed_db9_pins': [1, 3],
        'switch_com_state': 0,
    }
    if labjack_overrides:
        labjack_config.update(labjack_overrides)
    return Port(
        port_id=PortId.PORT_A,
        labjack_config=labjack_config,
        alicat_config={'address': 'A'},
        solenoid_config=solenoid_cfg or {},
    )


def test_configure_from_ptp_maps_terminal_pins(monkeypatch: Any) -> None:
    port = _make_port(monkeypatch)
    ok = port.configure_from_ptp(
        {
            'NormallyOpenTerminal': '3',
            'NormallyClosedTerminal': '1',
            'CommonTerminal': '2',
            'PressureReference': 'Gauge',
        }
    )
    assert ok
    daq = port.daq
    assert isinstance(daq, _FakeLabJackController)
    assert daq.configure_di_calls
    no_pin, nc_pin, com_pin, com_state = daq.configure_di_calls[-1]
    assert (no_pin, nc_pin, com_pin, com_state) == (2, 0, 1, 0)
    assert daq.pressure_reference == 'absolute'
    assert not daq.switch_nc_derived_from_no


def test_ptp_single_sense_no_derives_nc(monkeypatch: Any) -> None:
    port = _make_port(
        monkeypatch,
        labjack_overrides={
            'switch_sensed_db9_pins': [3],
        },
    )
    ok = port.configure_from_ptp(
        {
            'NormallyOpenTerminal': '3',
            'NormallyClosedTerminal': '1',
            'CommonTerminal': '4',
            'PressureReference': 'Gauge',
        }
    )
    assert ok
    daq = port.daq
    assert isinstance(daq, _FakeLabJackController)
    assert daq.configure_di_calls[-1] == (2, 2, 3, 0)
    assert daq.switch_nc_derived_from_no
    assert not daq.switch_no_derived_from_nc


def test_ptp_single_sense_nc_derives_no(monkeypatch: Any) -> None:
    port = _make_port(
        monkeypatch,
        labjack_overrides={
            'switch_sensed_db9_pins': [3],
        },
    )
    ok = port.configure_from_ptp(
        {
            'NormallyOpenTerminal': '1',
            'NormallyClosedTerminal': '3',
            'CommonTerminal': '4',
            'PressureReference': 'Absolute',
        }
    )
    assert ok
    daq = port.daq
    assert isinstance(daq, _FakeLabJackController)
    assert daq.configure_di_calls[-1] == (2, 2, 3, 0)
    assert not daq.switch_nc_derived_from_no
    assert daq.switch_no_derived_from_nc


def test_ptp_dual_sense_maps_db9_common(monkeypatch: Any) -> None:
    port = _make_port(
        monkeypatch,
        labjack_overrides={
            'switch_sensed_db9_pins': [4, 6],
        },
    )
    ok = port.configure_from_ptp(
        {
            'NormallyOpenTerminal': '4',
            'NormallyClosedTerminal': '6',
            'CommonTerminal': '5',
            'PressureReference': 'Gauge',
        }
    )
    assert ok
    daq = port.daq
    assert isinstance(daq, _FakeLabJackController)
    assert daq.configure_di_calls[-1] == (3, 5, 4, 0)
    assert not daq.switch_nc_derived_from_no
    assert not daq.switch_no_derived_from_nc


def test_ptp_not_connected_terminal_uses_observed_throw(monkeypatch: Any) -> None:
    port = _make_port(
        monkeypatch,
        labjack_overrides={
            'switch_sensed_db9_pins': [1],
        },
    )
    ok = port.configure_from_ptp(
        {
            'NormallyOpenTerminal': '0',
            'NormallyClosedTerminal': '1',
            'CommonTerminal': '6',
            'PressureReference': 'Gauge',
        }
    )
    assert ok
    daq = port.daq
    assert isinstance(daq, _FakeLabJackController)
    assert daq.configure_di_calls[-1] == (0, 0, 5, 0)
    assert not daq.switch_nc_derived_from_no
    assert daq.switch_no_derived_from_nc
    assert port.last_switch_resolution is not None
    assert port.last_switch_resolution.normally_open_terminal is None
    assert any(
        'NormallyOpenTerminal=0' in warning
        for warning in port.last_switch_resolution.warnings
    )


def test_ptp_common_sensed_drives_connected_throw(monkeypatch: Any) -> None:
    port = _make_port(
        monkeypatch,
        labjack_overrides={
            'switch_sensed_db9_pins': [3],
        },
    )
    ok = port.configure_from_ptp(
        {
            'NormallyOpenTerminal': '0',
            'NormallyClosedTerminal': '6',
            'CommonTerminal': '3',
            'PressureReference': 'Gauge',
        }
    )

    assert ok
    daq = port.daq
    assert isinstance(daq, _FakeLabJackController)
    assert daq.configure_di_calls[-1] == (2, 2, 5, 0)
    assert not daq.switch_nc_derived_from_no
    assert daq.switch_no_derived_from_nc
    assert port.last_switch_resolution is not None
    assert port.last_switch_resolution.derivation_mode == 'drive_nc_read_common'
    assert port.last_switch_resolution.drive_dio == 5


def test_ptp_spst_fallback_reads_connected_throw(monkeypatch: Any) -> None:
    port = _make_port(
        monkeypatch,
        labjack_overrides={
            'switch_sensed_db9_pins': [3],
        },
    )
    ok = port.configure_from_ptp(
        {
            'NormallyOpenTerminal': '2',
            'NormallyClosedTerminal': '0',
            'CommonTerminal': '1',
            'PressureReference': 'Gauge',
        }
    )

    assert ok
    daq = port.daq
    assert isinstance(daq, _FakeLabJackController)
    assert daq.configure_di_calls[-1] == (1, 1, 0, 0)
    assert daq.switch_nc_derived_from_no
    assert not daq.switch_no_derived_from_nc
    assert port.last_switch_resolution is not None
    assert port.last_switch_resolution.derivation_mode == 'derive_nc_from_no'


def test_ptp_invalid_terminal_fails_without_configured_pin_fallback(monkeypatch: Any) -> None:
    port = _make_port(
        monkeypatch,
        labjack_overrides={
            'switch_sensed_db9_pins': [1],
        },
    )
    ok = port.configure_from_ptp(
        {
            'NormallyOpenTerminal': '10',
            'NormallyClosedTerminal': '1',
            'CommonTerminal': '6',
            'PressureReference': 'Gauge',
        }
    )
    assert not ok
    daq = port.daq
    assert isinstance(daq, _FakeLabJackController)
    assert daq.configure_di_calls == []
    assert port.last_switch_resolution is not None
    assert port.last_switch_resolution.errors


def test_set_solenoid_refuses_unsafe_vacuum(monkeypatch: Any) -> None:
    port = _make_port(monkeypatch, solenoid_cfg={'safe_vacuum_switch_threshold_psi': 2.0})
    daq = port.daq
    alicat = port.alicat
    assert isinstance(daq, _FakeLabJackController)
    assert isinstance(alicat, _FakeAlicatController)
    alicat.next_reading = AlicatReading(
        pressure=20.0,
        setpoint=20.0,
        timestamp=1.0,
        gauge_pressure=5.3,
        barometric_pressure=14.7,
    )
    daq.next_pressure = 20.0

    assert not port.set_solenoid(True)
    assert daq.solenoid_calls == []


def test_set_solenoid_allows_safe_vacuum_and_resets_filter(monkeypatch: Any) -> None:
    port = _make_port(monkeypatch, solenoid_cfg={'safe_vacuum_switch_threshold_psi': 2.0})
    daq = port.daq
    alicat = port.alicat
    assert isinstance(daq, _FakeLabJackController)
    assert isinstance(alicat, _FakeAlicatController)
    alicat.next_reading = AlicatReading(
        pressure=15.0,
        setpoint=15.0,
        timestamp=1.0,
        gauge_pressure=0.3,
        barometric_pressure=14.7,
    )

    assert port.set_solenoid(True)
    assert daq.solenoid_calls == [True]
    assert daq.reset_filter_calls == 1


def test_set_solenoid_ignores_single_transducer_mux_outlier(monkeypatch: Any) -> None:
    port = _make_port(monkeypatch, solenoid_cfg={'safe_vacuum_switch_threshold_psi': 2.0})
    daq = port.daq
    assert isinstance(daq, _FakeLabJackController)
    pressures = iter((14.7, 21.4, 14.7, 14.7, 14.7))

    def read_transducer() -> TransducerReading:
        pressure = next(pressures)
        return TransducerReading(
            voltage=pressure / 3.0,
            pressure=pressure,
            pressure_raw=pressure,
            pressure_reference='absolute',
            timestamp=1.0,
        )

    daq.read_transducer = read_transducer  # type: ignore[method-assign]

    assert port.set_solenoid(True)
    assert daq.solenoid_calls == [True]
    assert daq.reset_filter_calls == 1


def test_read_fast_uses_cached_alicat_and_gauge_conversion(monkeypatch: Any) -> None:
    port = _make_port(monkeypatch)
    daq = port.daq
    assert isinstance(daq, _FakeLabJackController)
    daq.pressure_reference = 'gauge'
    daq.next_pressure = 16.0
    port._cached_alicat = AlicatReading(
        pressure=16.0,
        setpoint=16.0,
        timestamp=1.0,
        gauge_pressure=1.3,
        barometric_pressure=14.7,
    )

    reading = port.read_fast()
    assert reading.alicat is not None
    assert reading.transducer is not None
    assert reading.transducer.pressure == pytest.approx(1.3)
    assert reading.transducer.pressure_reference == 'gauge'


def test_edge_callback_invoked_on_switch_transition(monkeypatch: Any) -> None:
    port = _make_port(monkeypatch)
    daq = port.daq
    assert isinstance(daq, _FakeLabJackController)
    seen: list[tuple[bool, float]] = []
    port.register_edge_callback(lambda edge: seen.append((edge.activated, edge.pressure)))

    daq.next_pressure = 4.2
    daq.next_switch_activated = False
    _ = port.read_fast()
    daq.next_pressure = 3.7
    daq.next_switch_activated = True
    _ = port.read_fast()

    assert seen == [(True, pytest.approx(3.7))]


class _FakeManagedPort:
    def __init__(
        self,
        port_id: PortId,
        labjack_config: dict[str, Any],
        alicat_config: dict[str, Any],
        solenoid_config: dict[str, Any] | None = None,
    ) -> None:
        self.port_id = port_id
        self.labjack_config = dict(labjack_config)
        self.alicat_config = dict(alicat_config)
        self.solenoid_config = dict(solenoid_config or {})
        self.connect_result = True
        self.connect_calls = 0
        self.refresh_calls = 0
        self.read_fast_calls = 0
        self.read_precision_fast_calls = 0
        self.read_all_calls = 0
        self.disconnect_calls = 0
        self.vent_calls = 0
        self.exhaust_idle_calls = 0

    def connect(self) -> bool:
        self.connect_calls += 1
        return self.connect_result

    def refresh_alicat(self) -> bool:
        self.refresh_calls += 1
        return True

    def is_at_atmospheric_idle(self) -> bool:
        return False

    def _alicat_in_exhaust_mode(self) -> bool:
        return False

    def read_all(self) -> PortReading:
        self.read_all_calls += 1
        return PortReading(timestamp=float(self.read_all_calls))

    def read_fast(self) -> PortReading:
        self.read_fast_calls += 1
        return PortReading(timestamp=float(self.read_fast_calls))

    def read_precision_fast(self) -> PortReading:
        self.read_precision_fast_calls += 1
        return PortReading(timestamp=float(self.read_precision_fast_calls))

    def vent_to_atmosphere(self, **_kwargs: Any) -> bool:
        self.vent_calls += 1
        return True

    def ensure_exhaust_idle(self, **_kwargs: Any) -> bool:
        self.exhaust_idle_calls += 1
        return True

    def disconnect(self, **_kwargs: Any) -> None:
        self.disconnect_calls += 1

    def get_status(self) -> dict[str, Any]:
        return {'ok': True}


def _manager_config() -> dict[str, Any]:
    return {
        'timing': {'hardware_poll_interval_ms': 0, 'alicat_poll_divisor': 5},
        'hardware': {
            'solenoid': {'safe_vacuum_switch_threshold_psi': 2.5},
            'labjack': {
                'device_type': 'T7',
                'port_a': {'switch_no_dio': 1},
                'port_b': {'switch_no_dio': 9},
            },
            'alicat': {
                'port_a': {'address': 'A'},
                'port_b': {'address': 'B'},
                'serial_port': 'COM5',
            },
        },
    }


def test_port_manager_initializes_connects_and_reads(monkeypatch: Any) -> None:
    monkeypatch.setattr(port_module, 'Port', _FakeManagedPort)
    manager = PortManager(_manager_config())
    assert manager.initialize_ports()
    assert set(manager.ports.keys()) == {PortId.PORT_A, PortId.PORT_B}

    assert manager.connect_all()
    for port in manager.ports.values():
        assert isinstance(port, _FakeManagedPort)
        assert port.exhaust_idle_calls == 1
        assert port.vent_calls == 0
    readings = manager.read_all_ports()
    assert set(readings.keys()) == {PortId.PORT_A, PortId.PORT_B}
    assert manager.get_port('port_a') is not None
    assert manager.get_port('invalid') is None


def test_port_manager_connect_all_reports_failure(monkeypatch: Any) -> None:
    monkeypatch.setattr(port_module, 'Port', _FakeManagedPort)
    manager = PortManager(_manager_config())
    manager.initialize_ports()
    port_b = manager.get_port(PortId.PORT_B)
    assert isinstance(port_b, _FakeManagedPort)
    port_b.connect_result = False
    assert not manager.connect_all()


def test_port_manager_poll_loop_refreshes_cached_alicat(monkeypatch: Any) -> None:
    monkeypatch.setattr(port_module, 'Port', _FakeManagedPort)
    manager = PortManager(_manager_config())
    manager.initialize_ports()

    callback_count = {'value': 0}

    def on_poll(_readings: dict[PortId, PortReading]) -> None:
        callback_count['value'] += 1
        manager._polling = False

    manager.set_poll_callback(on_poll)
    manager._polling = True
    manager._poll_loop()

    assert callback_count['value'] == 1
    for port in manager.ports.values():
        assert isinstance(port, _FakeManagedPort)
        # One refresh while seeding; first loop respects divisor countdown.
        assert port.refresh_calls == 1
        assert port.read_fast_calls == 1


def test_port_manager_background_alicat_polling_lifecycle(monkeypatch: Any) -> None:
    monkeypatch.setattr(port_module, 'Port', _FakeManagedPort)
    config = _manager_config()
    config['timing'].update({
        'alicat_background_polling_enabled': True,
        'alicat_background_poll_hz': 100,
    })
    manager = PortManager(config)
    manager.initialize_ports()

    assert manager.start_polling()
    deadline = time.monotonic() + 0.25
    while time.monotonic() < deadline:
        status = manager.get_alicat_background_poll_status()
        if all(port['successes'] >= 3 for port in status['ports'].values()):
            break
        time.sleep(0.005)

    status = manager.get_alicat_background_poll_status()
    assert status['running'] is True
    assert status['target_hz_per_controller'] == pytest.approx(100.0)
    assert all(port['successes'] >= 3 for port in status['ports'].values())

    manager.stop_polling()
    assert manager.is_alicat_background_polling() is False


def test_port_manager_foreground_poll_is_cache_only_with_background_thread(monkeypatch: Any) -> None:
    monkeypatch.setattr(port_module, 'Port', _FakeManagedPort)
    manager = PortManager(_manager_config())
    manager.initialize_ports()
    monkeypatch.setattr(manager, 'is_alicat_background_polling', lambda: True)

    readings = manager._collect_poll_readings()

    assert set(readings) == {PortId.PORT_A, PortId.PORT_B}
    for port in manager.ports.values():
        assert isinstance(port, _FakeManagedPort)
        assert port.refresh_calls == 0
        assert port.read_fast_calls == 1


def test_port_manager_runtime_poll_profile_switch(monkeypatch: Any) -> None:
    monkeypatch.setattr(port_module, 'Port', _FakeManagedPort)
    manager = PortManager(_manager_config())
    manager.initialize_ports()

    # Defaults to normal divisor.
    divisors = manager.get_alicat_poll_divisors()
    assert divisors['port_a'] == 5
    assert divisors['port_b'] == 5

    # Precision profile: owner gets precision divisor; siblings stay normal until added.
    manager._alicat_poll_divisor_normal = 14
    manager._alicat_poll_divisor_precision = 2
    manager.set_alicat_poll_profile('port_b')
    divisors = manager.get_alicat_poll_divisors()
    assert divisors['port_a'] == 14
    assert divisors['port_b'] == 2

    # Concurrent precision: both ports can be in the precision set.
    manager.set_alicat_poll_profile('port_a')
    divisors = manager.get_alicat_poll_divisors()
    assert divisors['port_a'] == 2
    assert divisors['port_b'] == 2
    manager.remove_precision_port('port_a')
    divisors = manager.get_alicat_poll_divisors()
    assert divisors['port_a'] == 14
    assert divisors['port_b'] == 2

    # Manual override for one port.
    assert manager.set_alicat_poll_divisor('port_a', 9)
    divisors = manager.get_alicat_poll_divisors()
    assert divisors['port_a'] == 9
    assert divisors['port_b'] == 2


def test_read_precision_fast_skips_dio(monkeypatch: Any) -> None:
    port = _make_port(monkeypatch)
    daq = port.daq
    assert isinstance(daq, _FakeLabJackController)
    port._cached_alicat = AlicatReading(
        pressure=14.7,
        setpoint=14.7,
        timestamp=1.0,
        gauge_pressure=0.0,
        barometric_pressure=14.7,
    )

    reading = port.read_precision_fast()
    assert reading.transducer is not None
    assert reading.switch is not None
    assert reading.dio is None


def test_port_manager_precision_poll_prioritizes_owner(monkeypatch: Any) -> None:
    monkeypatch.setattr(port_module, 'Port', _FakeManagedPort)
    manager = PortManager(_manager_config())
    manager._labjack_poll_divisor_sibling = 3
    manager.initialize_ports()
    manager.set_alicat_poll_profile('port_a')

    port_a = manager.get_port(PortId.PORT_A)
    port_b = manager.get_port(PortId.PORT_B)
    assert isinstance(port_a, _FakeManagedPort)
    assert isinstance(port_b, _FakeManagedPort)

    for _ in range(5):
        manager._poll_reading(PortId.PORT_A, port_a)
        manager._poll_reading(PortId.PORT_B, port_b)

    assert port_a.read_precision_fast_calls == 5
    assert port_b.read_fast_calls == 5


def test_port_manager_disconnect_all_clears_ports(monkeypatch: Any) -> None:
    monkeypatch.setattr(port_module, 'Port', _FakeManagedPort)
    manager = PortManager(_manager_config())
    manager.initialize_ports()
    ports = list(manager.ports.values())
    manager.disconnect_all()
    assert manager.ports == {}
    for port in ports:
        assert isinstance(port, _FakeManagedPort)
        assert port.disconnect_calls == 1


def test_vent_to_atmosphere_leaves_exh_idle_alone(monkeypatch: Any) -> None:
    """Already EXH near baro must not exit exhaust into a baro setpoint."""
    port = _make_port(monkeypatch)
    alicat = port.alicat
    assert isinstance(alicat, _FakeAlicatController)
    alicat.next_reading = AlicatReading(
        pressure=14.68,
        setpoint=0.5,
        timestamp=1.0,
        gauge_pressure=-0.02,
        barometric_pressure=14.7,
        raw_response='A +014.68 +000.50 EXH',
    )
    assert port.vent_to_atmosphere() is True
    assert alicat.exhaust_calls == 0
    assert alicat.set_pressure_calls == []
    daq = port.daq
    assert isinstance(daq, _FakeLabJackController)
    assert daq.solenoid_calls == []


def test_vent_to_atmosphere_skips_when_already_at_atmospheric_idle(monkeypatch: Any) -> None:
    port = _make_port(monkeypatch)
    alicat = port.alicat
    assert isinstance(alicat, _FakeAlicatController)
    alicat.next_reading = AlicatReading(
        pressure=14.68,
        setpoint=14.7,
        timestamp=1.0,
        gauge_pressure=-0.02,
        barometric_pressure=14.7,
        raw_response='A +014.68 +014.70 HLD',
    )
    daq = port.daq
    assert isinstance(daq, _FakeLabJackController)
    assert port.is_at_atmospheric_idle() is True
    assert port.vent_to_atmosphere() is True
    assert alicat.hold_calls == 0
    assert alicat.cancel_hold_calls == 0
    assert daq.solenoid_calls == []


def test_vent_to_atmosphere_skips_idaho_open_fitting_hold(monkeypatch: Any) -> None:
    """Open fittings at altitude: P~13.5 HLD should not trigger bleed to 14.7."""
    port = _make_port(monkeypatch)
    alicat = port.alicat
    assert isinstance(alicat, _FakeAlicatController)
    alicat.next_reading = AlicatReading(
        pressure=13.476,
        setpoint=14.7,
        timestamp=1.0,
        gauge_pressure=-1.22,
        raw_response='A +013.48 +014.70 HLD',
    )
    daq = port.daq
    assert isinstance(daq, _FakeLabJackController)
    assert port.is_at_atmospheric_idle() is True
    assert port.vent_to_atmosphere() is True
    assert alicat.hold_calls == 0
    assert alicat.cancel_hold_calls == 0
    assert daq.solenoid_calls == []


def test_is_at_atmospheric_idle_accepts_exh_near_baro(monkeypatch: Any) -> None:
    """Boot EXH at local baro is idle — must not trigger setpoint pressurization."""
    port = _make_port(monkeypatch)
    alicat = port.alicat
    assert isinstance(alicat, _FakeAlicatController)
    alicat.next_reading = AlicatReading(
        pressure=14.595,
        setpoint=14.7,
        timestamp=1.0,
        gauge_pressure=-0.1,
        barometric_pressure=14.7,
        raw_response='A +014.60 +014.70 HLD EXH',
    )
    assert port.is_at_atmospheric_idle(14.595) is True
    assert port._alicat_in_exhaust_mode() is True


def test_ensure_exhaust_idle_enables_exh_from_hold(monkeypatch: Any) -> None:
    port = _make_port(monkeypatch)
    alicat = port.alicat
    assert isinstance(alicat, _FakeAlicatController)
    alicat.next_reading = AlicatReading(
        pressure=14.60,
        setpoint=14.70,
        timestamp=1.0,
        gauge_pressure=-0.1,
        barometric_pressure=14.7,
        raw_response='A +014.60 +014.70 HLD',
    )
    assert port.ensure_exhaust_idle() is True
    assert alicat.exhaust_calls >= 1
    assert port._alicat_in_exhaust_mode() is True
    assert alicat.set_pressure_calls == []


def test_is_at_atmospheric_idle_rejects_vacuum_hold(monkeypatch: Any) -> None:
    """A vacuum-held line must not be treated as atmospheric idle."""
    port = _make_port(monkeypatch)
    alicat = port.alicat
    assert isinstance(alicat, _FakeAlicatController)
    alicat.next_reading = AlicatReading(
        pressure=1.48,
        setpoint=0.93,
        timestamp=1.0,
        raw_response='A +001.48 +000.93 HLD',
    )
    assert port.is_at_atmospheric_idle(14.7) is False


def test_vent_to_atmosphere_reduces_low_gauge_positive_pressure(monkeypatch: Any) -> None:
    """Low mmHg tests can sit ~0.8 PSIG above baro while abs still looks idle."""
    port = _make_port(monkeypatch)
    alicat = port.alicat
    assert isinstance(alicat, _FakeAlicatController)
    pressurized = AlicatReading(
        pressure=15.43,
        setpoint=15.28,
        timestamp=1.0,
        gauge_pressure=0.83,
        barometric_pressure=14.6,
        raw_response='A +015.43 +015.28',
    )
    atmosphere = AlicatReading(
        pressure=14.6,
        setpoint=14.6,
        timestamp=2.0,
        gauge_pressure=0.0,
        barometric_pressure=14.6,
        raw_response='A +014.60 +014.60 EXH',
    )
    readings = [pressurized] * 3 + [atmosphere]
    state = {'index': 0}

    def _next_reading() -> PortReading:
        reading = readings[min(state['index'], len(readings) - 1)]
        state['index'] += 1
        return PortReading(alicat=reading, timestamp=reading.timestamp)

    port.read_all = _next_reading  # type: ignore[method-assign]

    assert port.is_at_atmospheric_idle(14.6) is True
    assert port.vent_to_atmosphere() is True
    assert alicat.exhaust_calls == 0
    assert alicat.set_pressure_calls


def test_vent_to_atmosphere_reduces_positive_test_pressure(monkeypatch: Any) -> None:
    """A completed positive-pressure test must not be locked at its test pressure."""
    port = _make_port(monkeypatch)
    alicat = port.alicat
    assert isinstance(alicat, _FakeAlicatController)
    high = AlicatReading(
        pressure=60.0,
        setpoint=60.0,
        timestamp=1.0,
        barometric_pressure=14.7,
        raw_response='A +060.00 +060.00',
    )
    atmosphere = AlicatReading(
        pressure=14.7,
        setpoint=14.7,
        timestamp=2.0,
        barometric_pressure=14.7,
        raw_response='A +014.70 +014.70',
    )
    readings = [high] * 5 + [atmosphere]
    state = {'index': 0}

    def _next_reading() -> PortReading:
        reading = readings[min(state['index'], len(readings) - 1)]
        state['index'] += 1
        return PortReading(alicat=reading, timestamp=reading.timestamp)

    port.read_all = _next_reading  # type: ignore[method-assign]

    assert port.vent_to_atmosphere() is True
    assert alicat.exhaust_calls == 0
    assert alicat.set_pressure_calls


def test_disconnect_restores_atmosphere_control(monkeypatch: Any) -> None:
    port = _make_port(monkeypatch)
    alicat = port.alicat
    assert isinstance(alicat, _FakeAlicatController)
    # Start in hold (not EXH) so disconnect parks into EXH idle.
    alicat.next_reading = AlicatReading(
        pressure=14.68,
        setpoint=14.70,
        timestamp=1.0,
        gauge_pressure=-0.02,
        barometric_pressure=14.7,
        raw_response='A +014.68 +014.70 HLD',
    )
    port.disconnect(restore_safe_state=True)
    alicat = port.alicat
    assert isinstance(alicat, _FakeAlicatController)
    assert alicat.exhaust_calls >= 1
    assert alicat.cancel_hold_calls == 0
    assert alicat.disconnect_calls == 1
    assert 'EXH' in (alicat.next_reading.raw_response or '').upper()


def test_port_manager_disconnect_all_vents_before_shared_disconnect(monkeypatch: Any) -> None:
    monkeypatch.setattr(port_module, 'Port', _FakeManagedPort)
    manager = PortManager(_manager_config())
    manager.initialize_ports()
    manager.connect_all(safe_idle_on_connect=False)
    ports = list(manager.ports.values())
    manager.disconnect_all()
    assert manager.ports == {}
    for port in ports:
        assert isinstance(port, _FakeManagedPort)
        assert port.vent_calls == 1
        assert port.disconnect_calls == 1


def test_vent_to_atmosphere_open_fitting_uses_exhaust(monkeypatch: Any) -> None:
    """Open fittings must pressurize up on the atmosphere route; EXH pulls vacuum."""
    port = _make_port(
        monkeypatch,
        labjack_overrides={
            'open_fitting': True,
            'transducer_installed': False,
            'local_barometric_psi': 13.48,
        },
    )
    alicat = port.alicat
    assert isinstance(alicat, _FakeAlicatController)
    low = AlicatReading(
        pressure=0.20,
        setpoint=0.5,
        timestamp=1.0,
        raw_response='A +000.20 +000.50 HLD',
    )
    high = AlicatReading(
        pressure=13.50,
        setpoint=13.48,
        timestamp=3.0,
        raw_response='A +013.50 +013.48 HLD',
    )
    readings = [low] * 8 + [high]
    state = {'index': 0}

    def _next_reading() -> AlicatReading:
        reading = readings[min(state['index'], len(readings) - 1)]
        state['index'] += 1
        return reading

    alicat.read_status = _next_reading  # type: ignore[method-assign]
    port.read_all = lambda: PortReading(alicat=_next_reading(), timestamp=1.0)  # type: ignore[method-assign]

    assert port.vent_to_atmosphere(bleed_installed_dut=True, timeout_s=5.0) is True
    assert alicat.exhaust_calls == 1
    assert alicat.set_pressure_calls == []


def test_vent_to_atmosphere_uses_exhaust_from_low_pressure(monkeypatch: Any) -> None:
    port = _make_port(monkeypatch)
    alicat = port.alicat
    assert isinstance(alicat, _FakeAlicatController)
    low = AlicatReading(
        pressure=0.05,
        setpoint=0.5,
        timestamp=1.0,
        barometric_pressure=14.7,
        raw_response='A +000.05 +000.50 EXH',
    )
    high = AlicatReading(
        pressure=14.6,
        setpoint=14.7,
        timestamp=3.0,
        barometric_pressure=14.7,
        raw_response='A +014.60 +014.70',
    )
    readings = [low] * 12 + [high]
    state = {'index': 0}

    def _next_reading() -> AlicatReading:
        reading = readings[min(state['index'], len(readings) - 1)]
        state['index'] += 1
        return reading

    alicat.read_status = _next_reading  # type: ignore[method-assign]
    port.read_all = lambda: PortReading(alicat=_next_reading(), timestamp=1.0)  # type: ignore[method-assign]

    assert port.vent_to_atmosphere(bleed_installed_dut=True, timeout_s=5.0) is True
    daq = port.daq
    assert isinstance(daq, _FakeLabJackController)
    assert daq.solenoid_calls
    assert daq.solenoid_calls[-1] is False
    assert alicat.exhaust_calls == 1


def test_session_gauge_zero_boot_lock(monkeypatch: Any) -> None:
    """Atmosphere P0 is process-wide and cleared only for reconnect."""
    Port.clear_session_gauge_zero_psia()
    assert Port.get_session_gauge_zero_psia() is None
    Port.set_session_gauge_zero_psia(14.6)
    assert Port.get_session_gauge_zero_psia() == pytest.approx(14.6)
    Port.clear_session_gauge_zero_psia()
    assert Port.get_session_gauge_zero_psia() is None


def test_sample_barometric_locks_session_gauge_zero(monkeypatch: Any) -> None:
    Port.clear_session_gauge_zero_psia()
    port = _make_port(monkeypatch)
    alicat = port.alicat
    assert isinstance(alicat, _FakeAlicatController)
    atm = AlicatReading(
        pressure=14.60,
        setpoint=14.60,
        timestamp=1.0,
        gauge_pressure=0.0,
        pressure_raw=14.60,
        barometric_pressure=14.60,
        raw_response='A +014.60 +014.60 EXH',
    )
    alicat.next_reading = atm
    port.read_all = lambda: PortReading(alicat=atm, timestamp=1.0)  # type: ignore[method-assign]
    measured = port._sample_barometric_via_exhaust(timeout_s=2.0)
    assert measured == pytest.approx(14.60)
    assert Port.get_session_gauge_zero_psia() == pytest.approx(14.60)
    Port.clear_session_gauge_zero_psia()
