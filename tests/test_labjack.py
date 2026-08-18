"""Unit tests for LabJackController simulation and helpers."""

from __future__ import annotations

import pytest

import app.hardware.labjack as labjack_module
from app.hardware.labjack import LabJackController


def _base_config() -> dict[str, object]:
    return {
        'device_type': 'T7',
        'connection_type': 'USB',
        'identifier': 'ANY',
        'allow_simulated_hardware': True,
        'transducer_ain': 0,
        'transducer_voltage_min': 0.5,
        'transducer_voltage_max': 4.5,
        'transducer_pressure_min': 0.0,
        'transducer_pressure_max': 100.0,
        'switch_no_dio': 1,
        'switch_nc_dio': 2,
        'solenoid_dio': 3,
        'pressure_filter_alpha': 0.5,
    }


def test_configure_and_status_in_sim_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(labjack_module, 'LJM_AVAILABLE', False)

    controller = LabJackController(_base_config())
    assert controller.configure()
    status = controller.get_status()
    assert status['configured'] is True
    assert controller.hardware_available() is False


def test_read_transducer_sim_uses_offset_and_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(labjack_module, 'LJM_AVAILABLE', False)

    cfg = _base_config()
    cfg['transducer_offset_psi'] = 2.0
    controller = LabJackController(cfg)
    controller.sim_set_pressure(20.0)

    first = controller.read_transducer()
    assert first is not None
    assert first.pressure_raw == pytest.approx(22.0)
    assert first.pressure == pytest.approx(22.0)

    controller.sim_set_pressure(30.0)
    second = controller.read_transducer()
    assert second is not None
    assert second.pressure_raw == pytest.approx(32.0)
    # EMA alpha=0.5 over samples 22 -> 32
    assert second.pressure == pytest.approx(27.0)


def test_read_switch_state_and_dio_in_sim_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(labjack_module, 'LJM_AVAILABLE', False)

    controller = LabJackController(_base_config())
    controller.sim_set_switch(True)
    state = controller.read_switch_state()
    assert state is not None
    assert state.switch_activated is True
    dio = controller.read_dio_values(max_dio=3)
    assert dio is not None
    assert len(dio) == 4


def test_set_solenoid_safe_in_sim_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(labjack_module, 'LJM_AVAILABLE', False)
    controller = LabJackController(_base_config())
    assert controller.set_solenoid(True)
    assert controller.set_solenoid_safe()


def test_set_solenoid_honors_port_route_polarity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(labjack_module, 'LJM_AVAILABLE', False)
    cfg = _base_config()
    cfg.update(
        {
            'solenoid_vacuum_state': 0,
            'solenoid_atmosphere_state': 1,
        }
    )
    controller = LabJackController(cfg)

    assert controller.set_solenoid(True)
    assert controller._solenoid_dio_state[3] == 0
    assert controller.set_solenoid_safe()
    assert controller._solenoid_dio_state[3] == 1


def test_transient_error_classifier() -> None:
    assert LabJackController._is_transient_ljm_error(RuntimeError('LJME_RECONNECT_FAILED'))
    assert LabJackController._is_transient_ljm_error(RuntimeError('code 1239 timeout'))
    assert not LabJackController._is_transient_ljm_error(RuntimeError('unrelated failure'))


def test_read_transducer_voltage_settles_differential_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(labjack_module, 'LJM_AVAILABLE', True)

    calls: list[tuple[str, str, float | None]] = []

    def fake_write(handle: int, name: str, value: float) -> None:
        calls.append(('write', name, value))

    def fake_read(handle: int, name: str) -> float:
        calls.append(('read', name, None))
        return {'AIN1': 0.01, 'AIN0': 4.5}.get(name, 0.0)

    monkeypatch.setattr(labjack_module.ljm, 'eWriteName', fake_write)
    monkeypatch.setattr(labjack_module.ljm, 'eReadName', fake_read)

    cfg = _base_config()
    cfg['transducer_ain_neg'] = 1
    controller = LabJackController(cfg)
    controller._shared_handle = 1
    controller._is_configured = True

    voltage = controller._read_transducer_voltage()
    assert voltage == pytest.approx(4.5)
    assert ('write', 'AIN1_NEGATIVE_CH', 199) in calls
    assert ('read', 'AIN1', None) in calls
    assert ('write', 'AIN0_NEGATIVE_CH', 1) in calls
    assert ('read', 'AIN0', None) in calls
