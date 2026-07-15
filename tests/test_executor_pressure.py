"""Focused TestExecutor pressure behavior tests."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.hardware.alicat import AlicatReading
from app.hardware.labjack import SwitchState, TransducerReading
from app.hardware.port import PortReading
from app.services.ptp_service import TestSetup, convert_pressure
from app.services.sweep_primitives import EdgeDetection, SpdtDebounceState, SweepPassOutcome, SweepResult
from app.services.test_executor import TestExecutor as _TestExecutor
from tests.fixtures.pressure_data import build_port_reading


class _FakeAlicat:
    def __init__(self) -> None:
        self.configure_calls = 0
        self.cancel_hold_calls = 0
        self.ramp_rates: list[float] = []

    def configure_units_from_ptp(self, _units_code: str) -> bool:
        self.configure_calls += 1
        return True

    def cancel_hold(self) -> bool:
        self.cancel_hold_calls += 1
        return True

    def set_ramp_rate(self, _rate: float) -> bool:
        self.ramp_rates.append(_rate)
        return True


class _FakePort:
    def __init__(self, outcomes: list[bool]) -> None:
        self.alicat = _FakeAlicat()
        self._outcomes = outcomes
        self.vent_calls = 0
        self.set_pressure_calls: list[float] = []
        self.solenoid_calls: list[bool] = []

    def set_pressure(self, setpoint: float) -> bool:
        self.set_pressure_calls.append(setpoint)
        if not self._outcomes:
            return True
        return self._outcomes.pop(0)

    def set_solenoid(self, to_vacuum: bool) -> bool:
        self.solenoid_calls.append(to_vacuum)
        return True

    def vent_to_atmosphere(self) -> bool:
        self.vent_calls += 1
        return True


def _build_executor(
    port: _FakePort,
    get_latest_reading: Any = None,
    on_cancelled: Any = None,
    wait_for_precision_slot: Any = None,
) -> _TestExecutor:
    setup = TestSetup(
        part_id='17025',
        sequence_id='399',
        units_code='21',
        units_label='Torr',
        activation_direction='Decreasing',
        activation_target=400.0,
        pressure_reference='absolute',
        terminals={},
        bands={
            'increasing': {'lower': 550.0, 'upper': 600.0},
            'decreasing': {'lower': 400.0, 'upper': 500.0},
            'reset': {'lower': 300.0, 'upper': 350.0},
        },
        raw={},
    )
    return _TestExecutor(
        port_id='port_a',
        port=cast(Any, port),
        test_setup=setup,
        config={'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}}},
        get_latest_reading=get_latest_reading or (lambda _pid: None),
        get_barometric_psi=lambda _pid: 14.7,
        on_cancelled=on_cancelled,
        wait_for_precision_slot=wait_for_precision_slot,
    )


def test_executor_set_pressure_recovers_after_one_failure() -> None:
    executor = _build_executor(_FakePort([False, True]))
    executor._set_pressure_or_raise(7.0)
    alicat = cast(_FakeAlicat, executor._port.alicat)
    assert alicat.configure_calls >= 1
    assert alicat.cancel_hold_calls == 1


def test_executor_set_pressure_raises_after_second_failure() -> None:
    executor = _build_executor(_FakePort([False, False]))
    with pytest.raises(RuntimeError):
        executor._set_pressure_or_raise(7.0)


def test_executor_run_emits_cancelled_and_vents() -> None:
    port = _FakePort([True])
    cancelled = {'called': False}
    executor = _build_executor(port, on_cancelled=lambda: cancelled.__setitem__('called', True))
    executor.request_cancel()
    executor._run()
    assert cancelled['called']
    assert port.vent_calls >= 1


def test_decreasing_pressure_cycle_uses_falling_edge_as_activation() -> None:
    setup = TestSetup(
        part_id='SPS01439-02',
        sequence_id='300',
        units_code='19',
        units_label='mmHg @ 0 C',
        activation_direction='Decreasing',
        activation_target=400.0,
        pressure_reference='gauge',
        terminals={},
        bands={
            'increasing': {'lower': 562.9, 'upper': 585.54},
            'decreasing': {'lower': 395.0, 'upper': 405.0},
            'reset': {'lower': float('-inf'), 'upper': float('inf')},
        },
        raw={},
    )
    executor = _TestExecutor(
        port_id='port_a',
        port=cast(Any, _FakePort([True])),
        test_setup=setup,
        config={'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}}},
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
    )

    assert executor._resolve_sweep_mode() == 'pressure'
    assert executor._resolve_activation_sweep_direction() == -1
    assert executor._cycle_target_switch_state('activation') is True
    assert executor._cycle_target_switch_state('deactivation') is False


def test_low_torr_vacuum_cycle_prep_resets_single_sense_no_switch() -> None:
    setup = TestSetup(
        part_id='SPS01804-02',
        sequence_id='600',
        units_code='21',
        units_label='Torr',
        activation_direction='Decreasing',
        activation_target=15.0,
        pressure_reference='absolute',
        terminals={},
        bands={
            'increasing': {'lower': float('-inf'), 'upper': 30.0},
            'decreasing': {'lower': 12.5, 'upper': 17.5},
            'reset': {'lower': float('-inf'), 'upper': float('inf')},
        },
        raw={},
    )
    port = _FakePort([True])
    executor = _TestExecutor(
        port_id='port_b',
        port=cast(Any, port),
        test_setup=setup,
        config={
            'hardware': {
                'labjack': {
                    'port_b': {'switch_nc_derived_from_no': True},
                },
            },
            'control': {
                'cycling': {},
                'ramps': {},
                'edge_detection': {'overshoot_beyond_limit_percent': 10.0},
                'debounce': {},
            },
        },
        get_latest_reading=lambda _pid: PortReading(
            transducer=TransducerReading(
                voltage=0.0,
                pressure=1.0803 if port.set_pressure_calls else 0.55,
                pressure_raw=1.0803 if port.set_pressure_calls else 0.55,
                pressure_reference='absolute',
                timestamp=0.0,
            ),
            switch=SwitchState(
                no_active=bool(port.set_pressure_calls),
                nc_active=bool(port.set_pressure_calls),
                timestamp=0.0,
            ),
            timestamp=0.0,
        ),
        get_barometric_psi=lambda _pid: 14.7,
    )

    assert executor._cycle_edge_already_present('activation', 0.55, False) is False
    executor._prepare_switch_for_cycle_edge(
        sweep_mode='vacuum',
        min_psi=convert_pressure(12.5, 'Torr', 'PSI'),
        max_psi=convert_pressure(30.0, 'Torr', 'PSI'),
        direction=-1,
        edge_type='activation',
        overshoot=0.5,
        hw_min_psi=0.0,
        hw_max_psi=115.0,
    )

    assert port.set_pressure_calls
    assert port.set_pressure_calls[-1] == pytest.approx(convert_pressure(30.0, 'Torr', 'PSI') + 0.5)


def test_executor_sweep_to_edge_returns_none_without_switch_transition() -> None:
    port = _FakePort([True])
    reading = PortReading(
        transducer=TransducerReading(
            voltage=2.5,
            pressure=14.7,
            pressure_raw=14.7,
            pressure_reference='absolute',
            timestamp=0.0,
        ),
        alicat=AlicatReading(
            pressure=14.7,
            setpoint=14.7,
            timestamp=0.0,
            gauge_pressure=0.0,
            barometric_pressure=14.7,
        ),
        switch=SwitchState(no_active=False, nc_active=True, timestamp=0.0),
        timestamp=0.0,
    )
    executor = _build_executor(port, get_latest_reading=lambda _pid: reading)
    executor._edge_timeout_s = 0.05
    executor._stable_count = 2
    assert executor._sweep_to_edge(target_psi=0.0, direction=1) is None


def test_executor_sweep_to_edge_honors_post_target_grace_window() -> None:
    port = _FakePort([True])
    samples = [
        PortReading(
            transducer=TransducerReading(
                voltage=2.5,
                pressure=0.95,
                pressure_raw=0.95,
                pressure_reference='absolute',
                timestamp=0.00,
            ),
            alicat=AlicatReading(
                pressure=0.95,
                setpoint=1.00,
                timestamp=0.00,
                gauge_pressure=0.0,
                barometric_pressure=14.7,
            ),
            switch=SwitchState(no_active=False, nc_active=True, timestamp=0.00),
            timestamp=0.00,
        ),
        PortReading(
            transducer=TransducerReading(
                voltage=2.5,
                pressure=1.00,
                pressure_raw=1.00,
                pressure_reference='absolute',
                timestamp=0.02,
            ),
            alicat=AlicatReading(
                pressure=1.00,
                setpoint=1.00,
                timestamp=0.02,
                gauge_pressure=0.0,
                barometric_pressure=14.7,
            ),
            switch=SwitchState(no_active=False, nc_active=True, timestamp=0.02),
            timestamp=0.02,
        ),
        PortReading(
            transducer=TransducerReading(
                voltage=2.5,
                pressure=1.01,
                pressure_raw=1.01,
                pressure_reference='absolute',
                timestamp=0.04,
            ),
            alicat=AlicatReading(
                pressure=1.01,
                setpoint=1.00,
                timestamp=0.04,
                gauge_pressure=0.0,
                barometric_pressure=14.7,
            ),
            switch=SwitchState(no_active=True, nc_active=False, timestamp=0.04),
            timestamp=0.04,
        ),
        PortReading(
            transducer=TransducerReading(
                voltage=2.5,
                pressure=1.01,
                pressure_raw=1.01,
                pressure_reference='absolute',
                timestamp=0.06,
            ),
            alicat=AlicatReading(
                pressure=1.01,
                setpoint=1.00,
                timestamp=0.06,
                gauge_pressure=0.0,
                barometric_pressure=14.7,
            ),
            switch=SwitchState(no_active=True, nc_active=False, timestamp=0.06),
            timestamp=0.06,
        ),
    ]
    idx = {'value': -1}

    def _reading(_pid: str) -> PortReading:
        idx['value'] = min(idx['value'] + 1, len(samples) - 1)
        return samples[idx['value']]

    executor = _build_executor(port, get_latest_reading=_reading)
    executor._edge_timeout_s = 0.35
    executor._stable_count = 2
    executor._precision_post_target_grace_s = 0.15
    edge = executor._sweep_to_edge(target_psi=1.0, direction=1, edge_type='activation')
    assert edge is not None
    assert edge.activated is True


def test_precision_activation_accepts_right_port_vacuum_no_open_edge() -> None:
    """Right-port 17029 wiring activates when the NO sense line opens."""
    setup = TestSetup(
        part_id='17029',
        sequence_id='399',
        units_code='1',
        units_label='PSI',
        activation_direction='Decreasing',
        activation_target=8.3,
        pressure_reference='gauge',
        terminals={},
        bands={
            'increasing': {'lower': float('-inf'), 'upper': 11.0},
            'decreasing': {'lower': 7.8, 'upper': 8.8},
            'reset': {'lower': float('-inf'), 'upper': float('inf')},
        },
        raw={},
    )
    samples = [
        PortReading(
            transducer=TransducerReading(
                voltage=2.5,
                pressure=9.2,
                pressure_raw=9.2,
                pressure_reference='absolute',
                timestamp=0.0,
            ),
            alicat=AlicatReading(
                pressure=9.2,
                setpoint=9.2,
                timestamp=0.0,
                gauge_pressure=-5.5,
                barometric_pressure=14.7,
            ),
            switch=SwitchState(no_active=True, nc_active=False, timestamp=0.0),
            timestamp=0.0,
        ),
        PortReading(
            transducer=TransducerReading(
                voltage=2.5,
                pressure=8.2,
                pressure_raw=8.2,
                pressure_reference='absolute',
                timestamp=0.1,
            ),
            alicat=AlicatReading(
                pressure=8.2,
                setpoint=7.8,
                timestamp=0.1,
                gauge_pressure=-6.5,
                barometric_pressure=14.7,
            ),
            switch=SwitchState(no_active=False, nc_active=True, timestamp=0.1),
            timestamp=0.1,
        ),
        PortReading(
            transducer=TransducerReading(
                voltage=2.5,
                pressure=8.1,
                pressure_raw=8.1,
                pressure_reference='absolute',
                timestamp=0.2,
            ),
            alicat=AlicatReading(
                pressure=8.1,
                setpoint=7.8,
                timestamp=0.2,
                gauge_pressure=-6.6,
                barometric_pressure=14.7,
            ),
            switch=SwitchState(no_active=False, nc_active=True, timestamp=0.2),
            timestamp=0.2,
        ),
    ]
    idx = {'value': -1}

    def _reading(_pid: str) -> PortReading:
        idx['value'] = min(idx['value'] + 1, len(samples) - 1)
        return samples[idx['value']]

    executor = _TestExecutor(
        port_id='port_b',
        port=cast(Any, _FakePort([True])),
        test_setup=setup,
        config={
            'hardware': {'labjack': {'port_b': {'vacuum_switch_trips_on_no_open': True}}},
            'control': {
                'cycling': {},
                'ramps': {},
                'edge_detection': {'timeout_sec': 0.5},
                'debounce': {'stable_sample_count': 2, 'min_edge_interval_ms': 0},
            },
        },
        get_latest_reading=_reading,
        get_barometric_psi=lambda _pid: 14.7,
    )

    edge = executor._sweep_to_edge(target_psi=7.8, direction=-1, edge_type='activation')

    assert edge is not None
    assert edge.activated is False
    assert edge.pressure_psi == pytest.approx(8.2)


def test_precision_result_uses_out_edge_as_activation_for_vacuum_no_open() -> None:
    executor = _build_executor(_FakePort([True]))
    edges = {
        'activation': EdgeDetection(pressure_psi=1.48, activated=False),
        'deactivation': EdgeDetection(pressure_psi=1.85, activated=True),
    }

    def _edge_for_type(
        _target: float,
        _direction: int,
        edge_type: str | None = None,
    ) -> EdgeDetection:
        return edges[edge_type or 'activation']

    executor._sweep_to_edge = _edge_for_type  # type: ignore[method-assign]

    outcome = executor._execute_out_back_sweep(
        target_out=0.7,
        target_back=2.6,
        direction=-1,
        rate_psi_per_sec=0.1,
        fail_on_rate_error=True,
    )

    assert outcome.result is not None
    assert outcome.result.activation_psi == pytest.approx(1.48)
    assert outcome.result.deactivation_psi == pytest.approx(1.85)


def test_precision_reasserts_ramp_rate_before_return_sweep() -> None:
    port = _FakePort([True])
    executor = _build_executor(port)
    edges = [
        EdgeDetection(pressure_psi=1.48, activated=False),
        EdgeDetection(pressure_psi=1.85, activated=True),
    ]

    executor._sweep_to_edge = lambda *_args, **_kwargs: edges.pop(0)  # type: ignore[method-assign]

    outcome = executor._execute_out_back_sweep(
        target_out=1.0,
        target_back=2.0,
        direction=-1,
        rate_psi_per_sec=0.0967,
        fail_on_rate_error=True,
    )

    assert outcome.result is not None
    assert port.alicat.ramp_rates == [pytest.approx(0.0967), pytest.approx(0.0967)]


def test_cycle_activation_rejects_decreasing_vacuum_edge_above_ptp_band() -> None:
    setup = TestSetup(
        part_id='17021',
        sequence_id='399',
        units_code='21',
        units_label='Torr',
        activation_direction='Decreasing',
        activation_target=75.0,
        pressure_reference='absolute',
        terminals={},
        bands={
            'increasing': {'lower': float('-inf'), 'upper': 145.0},
            'decreasing': {'lower': 70.0, 'upper': 80.0},
            'reset': {'lower': float('-inf'), 'upper': float('inf')},
        },
        raw={},
    )
    executor = _TestExecutor(
        port_id='port_b',
        port=cast(Any, _FakePort([True])),
        test_setup=setup,
        config={
            'hardware': {'labjack': {'port_b': {'vacuum_switch_trips_on_no_open': True}}},
            'control': {
                'cycling': {},
                'ramps': {},
                'edge_detection': {'timeout_sec': 0.5},
                'debounce': {'stable_sample_count': 1, 'min_edge_interval_ms': 0},
            },
        },
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
    )
    executor._cycle_waiting_edge = 'activation'

    executor._observe_cycle_switch_sample(
        pressure_test_psi=9.5,
        switch_state=SwitchState(no_active=True, nc_active=False, timestamp=0.0),
    )
    executor._observe_cycle_switch_sample(
        pressure_test_psi=9.4,
        switch_state=SwitchState(no_active=False, nc_active=True, timestamp=0.1),
    )

    assert executor._cycle_activation_samples == []
    assert not executor._cycle_edge_pressure_allowed('activation', 3.05)
    assert executor._cycle_edge_pressure_allowed('activation', 1.45)


def test_vacuum_no_open_config_inverts_cycle_target_state() -> None:
    setup = TestSetup(
        part_id='17021',
        sequence_id='399',
        units_code='21',
        units_label='Torr',
        activation_direction='Decreasing',
        activation_target=75.0,
        pressure_reference='absolute',
        terminals={},
        bands={
            'increasing': {'lower': float('-inf'), 'upper': 145.0},
            'decreasing': {'lower': 70.0, 'upper': 80.0},
            'reset': {'lower': float('-inf'), 'upper': float('inf')},
        },
        raw={},
    )
    executor = _TestExecutor(
        port_id='port_a',
        port=cast(Any, _FakePort([True])),
        test_setup=setup,
        config={
            'hardware': {
                'labjack': {
                    'port_a': {
                        'switch_nc_derived_from_no': True,
                    },
                },
            },
            'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}},
        },
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
    )

    assert executor._vacuum_switch_trips_on_no_open() is True
    assert executor._cycle_target_switch_state('activation') is False
    assert executor._cycle_target_switch_state('deactivation') is True
    assert executor._cycle_edge_switch_state_allowed('activation', True)
    assert executor._cycle_edge_switch_state_allowed('activation', False)

    executor._cycle_observed_edge_states['activation'] = True
    executor._cycle_observed_edge_states['deactivation'] = False

    assert executor._target_switch_state_for_edge('activation') is True
    assert executor._target_switch_state_for_edge('deactivation') is False
    assert executor._cycle_edge_switch_state_allowed('activation', True)
    assert not executor._cycle_edge_switch_state_allowed('activation', False)


def test_increasing_vacuum_no_open_uses_false_activation_state() -> None:
    setup = TestSetup(
        part_id='SPS02262-02',
        sequence_id='600',
        units_code='21',
        units_label='Torr',
        activation_direction='Increasing',
        activation_target=600.0,
        pressure_reference='absolute',
        terminals={},
        bands={
            'increasing': {'lower': 590.0, 'upper': 610.0},
            'decreasing': {'lower': 450.0, 'upper': float('inf')},
            'reset': {'lower': float('-inf'), 'upper': float('inf')},
        },
        raw={},
    )
    executor = _TestExecutor(
        port_id='port_b',
        port=cast(Any, _FakePort([True])),
        test_setup=setup,
        config={
            'hardware': {
                'labjack': {
                    'port_b': {
                        'switch_nc_derived_from_no': True,
                    },
                },
            },
            'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}},
        },
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
    )

    assert executor._vacuum_switch_trips_on_no_open() is True
    assert executor._cycle_target_switch_state('activation') is False
    assert executor._cycle_target_switch_state('deactivation') is True
    assert not executor._cycle_edge_switch_state_allowed('activation', True)
    assert executor._cycle_edge_switch_state_allowed('activation', False)


def test_vacuum_no_open_string_false_is_not_truthy() -> None:
    executor = _build_executor(_FakePort([True]))
    executor._config = {
        'hardware': {
            'labjack': {
                'port_a': {
                    'vacuum_switch_trips_on_no_open': 'false',
                    'switch_nc_derived_from_no': True,
                },
            },
        },
        'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}},
    }

    assert executor._vacuum_switch_trips_on_no_open() is False


def test_decreasing_vacuum_activation_prep_skips_when_already_reset_side() -> None:
    port = _FakePort([True])
    executor = _build_executor(port)
    executor._config = {
        'hardware': {
            'labjack': {
                'port_a': {
                    'switch_nc_derived_from_no': True,
                },
            },
        },
        'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}},
    }
    executor._read_pressure_and_switch_state = lambda: (1.5, False)  # type: ignore[method-assign]

    executor._prepare_switch_for_cycle_edge(
        sweep_mode='vacuum',
        min_psi=0.87,
        max_psi=1.93,
        direction=-1,
        edge_type='activation',
        overshoot=0.5,
        hw_min_psi=0.0,
        hw_max_psi=14.7,
    )

    assert port.set_pressure_calls == []
    assert port.solenoid_calls == []


def test_nc_derived_runtime_mode_overrides_stale_vacuum_no_open_config() -> None:
    port = _FakePort([True])
    port.daq = SimpleNamespace(
        switch_nc_derived_from_no=False,
        switch_no_derived_from_nc=True,
    )
    executor = _build_executor(port)
    executor._config = {
        'hardware': {
            'labjack': {
                'port_a': {
                    'vacuum_switch_trips_on_no_open': True,
                },
            },
        },
        'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}},
    }

    assert executor._vacuum_switch_trips_on_no_open() is False
    assert executor._cycle_target_switch_state('activation') is True
    assert executor._cycle_target_switch_state('deactivation') is False


def test_nc_derived_single_sense_without_explicit_config_uses_nc_runtime_fallback() -> None:
    port = _FakePort([True])
    port.daq = SimpleNamespace(
        switch_nc_derived_from_no=False,
        switch_no_derived_from_nc=True,
    )
    executor = _build_executor(port)
    executor._config = {
        'hardware': {'labjack': {'port_a': {}}},
        'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}},
    }

    assert executor._vacuum_switch_trips_on_no_open() is False
    assert executor._cycle_target_switch_state('activation') is True
    assert executor._cycle_target_switch_state('deactivation') is False


def test_sps01496_seq600_uses_nc_derived_runtime_vacuum_target_on_both_ports() -> None:
    setup = TestSetup(
        part_id='SPS01496-02',
        sequence_id='600',
        units_code='21',
        units_label='Torr',
        activation_direction='Decreasing',
        activation_target=400.0,
        pressure_reference='absolute',
        terminals={'common': 4, 'normally_open': 1, 'normally_closed': 3},
        bands={
            'increasing': {'lower': float('-inf'), 'upper': 490.0},
            'decreasing': {'lower': 390.0, 'upper': 410.0},
            'reset': {'lower': float('-inf'), 'upper': float('inf')},
        },
        raw={},
    )
    config = {
        'hardware': {
            'labjack': {
                # The old port-level default is stale for this PTP setup; the
                # runtime PTP switch resolution must decide the target state.
                'port_a': {'vacuum_switch_trips_on_no_open': True},
                'port_b': {'vacuum_switch_trips_on_no_open': True},
            },
        },
        'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}},
    }

    for port_id in ('port_a', 'port_b'):
        port = _FakePort([True])
        port.daq = SimpleNamespace(
            switch_nc_derived_from_no=False,
            switch_no_derived_from_nc=True,
        )
        executor = _TestExecutor(
            port_id=port_id,
            port=cast(Any, port),
            test_setup=setup,
            config=config,
            get_latest_reading=lambda _pid: None,
            get_barometric_psi=lambda _pid: 14.7,
        )

        assert executor._resolve_sweep_mode() == 'vacuum'
        assert executor._vacuum_switch_trips_on_no_open() is False
        assert executor._cycle_target_switch_state('activation') is True
        assert executor._cycle_target_switch_state('deactivation') is False


def test_vacuum_increasing_nc_derived_reset_target_is_above_band() -> None:
    setup = TestSetup(
        part_id='SPS02305-02',
        sequence_id='600',
        units_code='21',
        units_label='Torr',
        activation_direction='Increasing',
        activation_target=550.0,
        pressure_reference='absolute',
        terminals={},
        bands={
            'increasing': {'lower': 537.0, 'upper': 563.0},
            'decreasing': {'lower': 400.0, 'upper': float('inf')},
            'reset': {'lower': float('-inf'), 'upper': float('inf')},
        },
        raw={},
    )
    port = _FakePort([True])
    port.daq = SimpleNamespace(
        switch_nc_derived_from_no=False,
        switch_no_derived_from_nc=True,
    )
    executor = _TestExecutor(
        port_id='port_a',
        port=cast(Any, port),
        test_setup=setup,
        config={
            'hardware': {'labjack': {'port_a': {'vacuum_switch_trips_on_no_open': True}}},
            'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}},
        },
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
    )
    min_psi, max_psi = executor._resolve_sweep_bounds()
    activation, deactivation = executor._resolve_cycle_targets(
        sweep_mode='vacuum',
        min_psi=min_psi,
        max_psi=max_psi,
        overshoot=0.5,
        hw_min_psi=0.0,
        hw_max_psi=14.7,
    )

    assert activation == pytest.approx(max_psi + 0.5)
    assert deactivation == pytest.approx(max_psi + 0.5)

def test_vacuum_increasing_nc_derived_cycle_repositions_low_then_sweeps_high() -> None:
    setup = TestSetup(
        part_id='SPS02305-02',
        sequence_id='600',
        units_code='21',
        units_label='Torr',
        activation_direction='Increasing',
        activation_target=550.0,
        pressure_reference='absolute',
        terminals={},
        bands={
            'increasing': {'lower': 537.0, 'upper': 563.0},
            'decreasing': {'lower': 400.0, 'upper': float('inf')},
            'reset': {'lower': float('-inf'), 'upper': float('inf')},
        },
        raw={},
    )
    port = _FakePort([True, True])
    port.daq = SimpleNamespace(
        switch_nc_derived_from_no=False,
        switch_no_derived_from_nc=True,
    )
    executor = _TestExecutor(
        port_id='port_a',
        port=cast(Any, port),
        test_setup=setup,
        config={
            'hardware': {'labjack': {'port_a': {'vacuum_switch_trips_on_no_open': True}}},
            'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}},
        },
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
    )
    waits: list[tuple[str, float, int]] = []
    executor._wait_until_near_target = lambda **_kwargs: True  # type: ignore[method-assign]

    def _record_wait(**kwargs: Any) -> tuple[bool, None]:
        waits.append((kwargs['edge_type'], kwargs['target_psi'], kwargs['direction']))
        if kwargs['edge_type'] == 'activation':
            executor._cycle_activation_samples.append(9.95)
        else:
            executor._cycle_deactivation_samples.append(10.85)
        return True, None

    executor._wait_for_cycle_edge = _record_wait  # type: ignore[method-assign]

    bounds = executor._resolve_sweep_bounds()
    executor._cycle_phase_runner.run_single_cycle('vacuum', bounds)

    low_command, high_command = port.set_pressure_calls
    assert low_command < bounds[0]
    assert high_command > bounds[1]
    assert waits == [
        ('activation', pytest.approx(low_command), -1),
        ('deactivation', pytest.approx(high_command), 1),
    ]


def test_deep_vacuum_precision_rate_stays_fast() -> None:
    setup = TestSetup(
        part_id='LOW-VAC-SIDE',
        sequence_id='600',
        units_code='21',
        units_label='Torr',
        activation_direction='Increasing',
        activation_target=550.0,
        pressure_reference='absolute',
        terminals={},
        bands={
            'increasing': {'lower': 5.0, 'upper': 600.0},
            'decreasing': {'lower': 5.0, 'upper': float('inf')},
            'reset': {'lower': float('-inf'), 'upper': float('inf')},
        },
        raw={},
    )
    executor = _TestExecutor(
        port_id='port_b',
        port=cast(Any, _FakePort([True])),
        test_setup=setup,
        config={
            'control': {
                'cycling': {},
                'ramps': {
                    'precision_sweep_rate_torr_per_sec': 5.0,
                    'precision_edge_rate_torr_per_sec': 5.0,
                    'low_pressure_precision_threshold_psi': 1.0,
                    'low_pressure_precision_sweep_rate_torr_per_sec': 1.0,
                },
                'edge_detection': {},
                'debounce': {},
            },
        },
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
    )

    assert executor._slow_edge_rate_psi == pytest.approx(convert_pressure(5.0, 'Torr', 'PSI'))


def test_near_atmosphere_precision_rate_slows() -> None:
    setup = TestSetup(
        part_id='NEAR-ATM',
        sequence_id='300',
        units_code='1',
        units_label='PSI',
        activation_direction='Increasing',
        activation_target=0.4,
        pressure_reference='gauge',
        terminals={},
        bands={
            'increasing': {'lower': 0.2, 'upper': 0.6},
            'decreasing': {'lower': -0.2, 'upper': 0.1},
            'reset': {'lower': float('-inf'), 'upper': float('inf')},
        },
        raw={},
    )
    executor = _TestExecutor(
        port_id='port_b',
        port=cast(Any, _FakePort([True])),
        test_setup=setup,
        config={
            'control': {
                'cycling': {},
                'ramps': {
                    'precision_sweep_rate_torr_per_sec': 5.0,
                    'precision_edge_rate_torr_per_sec': 5.0,
                    'low_pressure_precision_threshold_psi': 1.0,
                    'low_pressure_precision_sweep_rate_torr_per_sec': 1.0,
                },
                'edge_detection': {},
                'debounce': {},
            },
        },
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
    )

    assert executor._slow_edge_rate_psi == pytest.approx(convert_pressure(1.0, 'Torr', 'PSI'))


def test_vacuum_increasing_nc_derived_precision_orders_by_ptp_direction() -> None:
    setup = TestSetup(
        part_id='SPS02305-02',
        sequence_id='600',
        units_code='21',
        units_label='Torr',
        activation_direction='Increasing',
        activation_target=550.0,
        pressure_reference='absolute',
        terminals={},
        bands={
            'increasing': {'lower': 537.0, 'upper': 563.0},
            'decreasing': {'lower': 400.0, 'upper': float('inf')},
            'reset': {'lower': float('-inf'), 'upper': float('inf')},
        },
        raw={},
    )
    port = _FakePort([True])
    port.daq = SimpleNamespace(
        switch_nc_derived_from_no=False,
        switch_no_derived_from_nc=True,
    )
    executor = _TestExecutor(
        port_id='port_a',
        port=cast(Any, port),
        test_setup=setup,
        config={
            'hardware': {'labjack': {'port_a': {'vacuum_switch_trips_on_no_open': True}}},
            'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}},
        },
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
    )
    executor._cycle_activation_samples = [9.96]
    executor._cycle_deactivation_samples = [10.85]

    activation, deactivation = executor._ordered_cycle_estimates()
    approach, target_out, _target_back, source = executor._resolve_precision_targets(
        min_psi=convert_pressure(400.0, 'Torr', 'PSI'),
        max_psi=convert_pressure(563.0, 'Torr', 'PSI'),
        activation_direction=1,
    )

    assert (activation, deactivation) == (pytest.approx(10.85), pytest.approx(9.96))
    assert source == 'cycle-estimate-offset-close-limit'
    assert approach < 10.85
    assert target_out > 10.85


def test_window_precision_emits_each_edge_as_it_is_found() -> None:
    setup = TestSetup(
        part_id='SPS02305-02',
        sequence_id='600',
        units_code='21',
        units_label='Torr',
        activation_direction='Increasing',
        activation_target=550.0,
        pressure_reference='absolute',
        terminals={},
        bands={
            'increasing': {'lower': 537.0, 'upper': 563.0},
            'decreasing': {'lower': 400.0, 'upper': float('inf')},
            'reset': {'lower': float('-inf'), 'upper': float('inf')},
        },
        raw={},
    )
    emitted: list[tuple[str, float]] = []
    executor = _TestExecutor(
        port_id='port_a',
        port=cast(Any, _FakePort([True])),
        test_setup=setup,
        config={'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}}},
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
        on_edge_detected=lambda edge_type, pressure: emitted.append((edge_type, pressure)),
    )
    edges = [EdgeDetection(pressure_psi=10.0, activated=True), EdgeDetection(pressure_psi=10.8, activated=False)]
    executor._sweep_to_edge = lambda *_args, **_kwargs: edges.pop(0)  # type: ignore[method-assign]

    outcome = executor._run_window_precision_pass(
        low_target=9.6,
        high_target=11.2,
        rate_psi_per_sec=0.1,
    )

    assert outcome.result == SweepResult(activation_psi=10.8, deactivation_psi=10.0)
    assert emitted == [('deactivation', 10.0), ('activation', 10.8)]


def test_executor_precision_targets_use_close_limit_for_decreasing() -> None:
    executor = _build_executor(_FakePort([True]))
    approach, target_out, target_back, source = executor._resolve_precision_targets(
        min_psi=convert_pressure(390.0, 'Torr', 'PSI'),
        max_psi=convert_pressure(600.0, 'Torr', 'PSI'),
        activation_direction=-1,
    )
    assert source == 'ptp-close-limit'
    assert approach == pytest.approx(convert_pressure(600.0, 'Torr', 'PSI'), rel=1e-6)
    assert target_out == pytest.approx(convert_pressure(400.0, 'Torr', 'PSI'), rel=1e-6)
    assert target_back == pytest.approx(convert_pressure(600.0, 'Torr', 'PSI'), rel=1e-6)


def test_executor_precision_targets_use_close_limit_for_increasing() -> None:
    setup = TestSetup(
        part_id='17025',
        sequence_id='399',
        units_code='1',
        units_label='PSI',
        activation_direction='Increasing',
        activation_target=25.0,
        pressure_reference='gauge',
        terminals={},
        bands={
            'increasing': {'lower': 24.0, 'upper': 26.0},
            'decreasing': {'lower': 22.0, 'upper': 23.0},
            'reset': {'lower': 21.0, 'upper': 27.0},
        },
        raw={},
    )
    executor = _TestExecutor(
        port_id='port_a',
        port=cast(Any, _FakePort([True])),
        test_setup=setup,
        config={'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}}},
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
    )
    approach, target_out, target_back, source = executor._resolve_precision_targets(
        min_psi=22.0,
        max_psi=26.0,
        activation_direction=1,
    )
    assert source == 'ptp-close-limit'
    assert approach == pytest.approx(22.0, rel=1e-6)
    assert target_out == pytest.approx(26.0, rel=1e-6)
    assert target_back == pytest.approx(22.0, rel=1e-6)


def test_executor_precision_targets_auto_reorder_swapped_cycle_estimates() -> None:
    """When cycle estimates are in the wrong order for the activation direction,
    _ordered_cycle_estimates swaps them so that valid precision targets are
    derived from cycle data rather than falling back to PTP close-limit."""
    setup = TestSetup(
        part_id='17025',
        sequence_id='399',
        units_code='1',
        units_label='PSI',
        activation_direction='Increasing',
        activation_target=25.0,
        pressure_reference='gauge',
        terminals={},
        bands={
            'increasing': {'lower': 24.0, 'upper': 26.0},
            'decreasing': {'lower': 22.0, 'upper': 23.0},
            'reset': {'lower': 21.0, 'upper': 27.0},
        },
        raw={},
    )
    executor = _TestExecutor(
        port_id='port_a',
        port=cast(Any, _FakePort([True])),
        test_setup=setup,
        config={'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}}},
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
    )
    # Raw labels are swapped for increasing direction (activation below deactivation),
    # but _ordered_cycle_estimates auto-corrects this.
    executor._cycle_activation_samples = [23.0]
    executor._cycle_deactivation_samples = [24.5]
    approach, target_out, target_back, source = executor._resolve_precision_targets(
        min_psi=22.0,
        max_psi=26.0,
        activation_direction=1,
    )
    assert source == 'cycle-estimate-offset-close-limit'
    # After reorder: activation_est=24.5 (higher), deactivation_est=23.0 (lower)
    offset = 20.0 * (14.7 / 760.0)  # precision_close_limit_offset_torr in PSI
    margin = 15.0 * (14.7 / 760.0)  # precision_deactivation_margin_torr in PSI
    expected_approach = max(22.0, min(24.5 - offset, 24.0 - offset * 0.25))
    assert approach == pytest.approx(expected_approach, rel=1e-3)
    # target_out is widened to cover the activation band upper limit (26.0)
    # plus a 25% offset buffer to guarantee the sweep passes through
    assert target_out == pytest.approx(26.0 + offset * 0.25, rel=1e-3)
    assert target_back == pytest.approx(max(22.0, 23.0 - margin), rel=1e-3)


def test_executor_run_precision_skips_atmosphere_gate_after_cycling() -> None:
    executor = _build_executor(_FakePort([True]))
    captured: dict[str, bool] = {}

    executor._ensure_alicat_units = lambda: None
    executor._resolve_sweep_mode = lambda: 'pressure'
    executor._resolve_sweep_bounds = lambda: (0.0, 2.0)
    executor._cycle_phase_runner.run_pre_approach = lambda _mode, _bounds: None
    executor._run_single_cycle = lambda _mode, _bounds: None
    executor._run_precision_sweep = (
        lambda _mode, _bounds, skip_atmosphere_gate=False: (
            captured.__setitem__('skip_atmosphere_gate', skip_atmosphere_gate)
            or SimpleNamespace(activation_psi=1.2, deactivation_psi=0.8)
        )
    )

    executor._run()
    assert captured['skip_atmosphere_gate'] is True


def test_executor_waits_for_precision_slot_before_sweep() -> None:
    executor = _build_executor(
        _FakePort([True]),
        wait_for_precision_slot=lambda: False,
    )
    calls: list[str] = []

    executor._ensure_alicat_units = lambda: None
    executor._lock_alicat_setpoint_reference = lambda: None
    executor._resolve_sweep_mode = lambda: 'pressure'
    executor._resolve_sweep_bounds = lambda: (1.0, 2.0)
    executor._cycle_phase_runner.run_pre_approach = lambda _mode, _bounds: None
    executor._run_single_cycle = lambda _mode, _bounds: None
    executor._run_precision_sweep = lambda *_args, **_kwargs: calls.append('precision')
    executor._num_cycles = 0

    executor._run()

    assert calls == []


def test_precision_direct_handoff_skips_fast_approach_when_switch_is_reset() -> None:
    executor = _build_executor(_FakePort([True]))
    called = {'fast_approach': False}

    executor._vacuum_switch_trips_on_no_open = lambda: True  # type: ignore[method-assign]
    executor._read_pressure_and_switch_state = lambda: (2.0, True)  # type: ignore[method-assign]
    executor._precision_phase_runner._run_precision_fast_approach = (  # type: ignore[method-assign]
        lambda _mode, _target: called.__setitem__('fast_approach', True)
    )
    executor._run_sweep_pass = lambda *_args: SweepPassOutcome(  # type: ignore[method-assign]
        result=SweepResult(activation_psi=1.5, deactivation_psi=1.8),
        missing_edge=None,
    )

    result = executor._run_precision_sweep('vacuum', (0.5, 3.0), skip_atmosphere_gate=True)

    assert result is not None
    assert called['fast_approach'] is False


def test_precision_reapproaches_after_reset_side_arming() -> None:
    executor = _build_executor(_FakePort([True]))
    fast_targets: list[tuple[str, float]] = []

    executor._resolve_precision_targets = lambda *_args: (1.4, 0.7, 2.3, 'cycle')  # type: ignore[method-assign]
    executor._precision_phase_runner._can_start_precision_from_current_reset_side = (  # type: ignore[method-assign]
        lambda *_args: False
    )
    executor._precision_phase_runner._run_precision_fast_approach = (  # type: ignore[method-assign]
        lambda mode, target: fast_targets.append((mode, target))
    )
    executor._precision_phase_runner._ensure_precision_starts_from_reset_side = (  # type: ignore[method-assign]
        lambda *_args: True
    )
    executor._run_sweep_pass = lambda *_args: SweepPassOutcome(  # type: ignore[method-assign]
        result=SweepResult(activation_psi=1.1, deactivation_psi=1.7),
        missing_edge=None,
    )

    result = executor._run_precision_sweep('vacuum', (0.5, 3.0), skip_atmosphere_gate=True)

    assert result is not None
    assert fast_targets == [('vacuum', 1.4), ('vacuum', 1.4)]


def test_precision_direct_handoff_requires_pressure_near_close_limit() -> None:
    executor = _build_executor(_FakePort([True]))

    executor._vacuum_switch_trips_on_no_open = lambda: True  # type: ignore[method-assign]
    executor._read_pressure_and_switch_state = lambda: (2.0, True)  # type: ignore[method-assign]

    assert not executor._precision_phase_runner._can_start_precision_from_current_reset_side(
        skip_atmosphere_gate=True,
        activation_direction=-1,
        approach_target=1.7,
    )


def test_precision_direct_handoff_disabled_for_nc_derived_vacuum_window() -> None:
    executor = _build_executor(_FakePort([True]))
    executor._resolve_sweep_mode = lambda: 'vacuum'  # type: ignore[method-assign]
    executor._resolve_activation_sweep_direction = lambda: 1  # type: ignore[method-assign]
    executor._port.daq = SimpleNamespace(switch_no_derived_from_nc=True)
    executor._read_pressure_and_switch_state = lambda: (11.0, False)  # type: ignore[method-assign]

    assert not executor._precision_phase_runner._can_start_precision_from_current_reset_side(
        skip_atmosphere_gate=True,
        activation_direction=1,
        approach_target=9.5,
    )


def test_precision_arming_uses_reset_target_when_already_activated_decreasing() -> None:
    setup = TestSetup(
        part_id='SPS01496-02',
        sequence_id='300',
        units_code='19',
        units_label='mmHg @ 0 C',
        activation_direction='Decreasing',
        activation_target=400.0,
        pressure_reference='gauge',
        terminals={},
        bands={
            'increasing': {'lower': float('-inf'), 'upper': 490.0},
            'decreasing': {'lower': 395.0, 'upper': 405.0},
            'reset': {'lower': float('-inf'), 'upper': float('inf')},
        },
        raw={},
    )
    port = _FakePort([True])
    executor = _TestExecutor(
        port_id='port_a',
        port=cast(Any, port),
        test_setup=setup,
        config={'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}}},
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
    )
    states = iter([(8.33, True), (8.60, True), (8.90, False)])
    commanded: list[float] = []

    executor._read_pressure_and_switch_state = lambda: next(states, (8.90, False))  # type: ignore[method-assign]
    executor._set_pressure_or_raise = lambda pressure: commanded.append(pressure)  # type: ignore[method-assign]

    executor._precision_phase_runner._ensure_precision_starts_from_reset_side(
        sweep_mode='pressure',
        activation_direction=-1,
        approach_target=7.93,
        reset_target=8.96,
        min_psi=7.64,
        max_psi=9.48,
    )

    assert commanded == [pytest.approx(23.66)]
    assert port.solenoid_calls == [True]


def test_pressure_decreasing_activation_prep_resets_above_band() -> None:
    port = _FakePort([True])
    executor = _build_executor(port)
    executor._resolve_sweep_mode = lambda: 'pressure'  # type: ignore[method-assign]
    executor._resolve_activation_sweep_direction = lambda: -1  # type: ignore[method-assign]
    states = iter([(9.9, True), (9.98, False)])
    executor._read_pressure_and_switch_state = lambda: next(states, (9.98, False))  # type: ignore[method-assign]
    executor._get_latest_reading = lambda _pid: None  # type: ignore[method-assign]

    commanded: list[float] = []
    executor._set_pressure_or_raise = lambda pressure: commanded.append(pressure)  # type: ignore[method-assign]

    executor._prepare_switch_for_cycle_edge(
        sweep_mode='pressure',
        min_psi=7.64,
        max_psi=9.48,
        direction=-1,
        edge_type='activation',
        overshoot=0.5,
        hw_min_psi=0.0,
        hw_max_psi=30.0,
    )

    assert commanded == [pytest.approx(9.98)]
    assert port.solenoid_calls == [True]


def test_pressure_decreasing_derive_nc_from_no_inverts_cycle_targets() -> None:
    port = _FakePort([True])
    port.daq = SimpleNamespace(switch_nc_derived_from_no=True)
    executor = _build_executor(port)
    executor._resolve_sweep_mode = lambda: 'pressure'  # type: ignore[method-assign]
    executor._resolve_activation_sweep_direction = lambda: -1  # type: ignore[method-assign]

    assert executor._cycle_target_switch_state('activation') is False
    assert executor._cycle_target_switch_state('deactivation') is True


def test_vacuum_increasing_pre_approach_starts_on_low_reset_side() -> None:
    setup = TestSetup(
        part_id='SPS02305-02',
        sequence_id='600',
        units_code='21',
        units_label='Torr',
        activation_direction='Increasing',
        activation_target=550.0,
        pressure_reference='absolute',
        terminals={},
        bands={
            'increasing': {'lower': 537.0, 'upper': 563.0},
            'decreasing': {'lower': 400.0, 'upper': float('inf')},
            'reset': {'lower': float('-inf'), 'upper': float('inf')},
        },
        raw={},
    )
    executor = _TestExecutor(
        port_id='port_a',
        port=cast(Any, _FakePort([True])),
        test_setup=setup,
        config={'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}}},
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
    )
    commanded: list[float] = []
    waits: list[float] = []
    wait_timeouts: list[float] = []
    executor._set_pressure_or_raise = lambda pressure: commanded.append(pressure)  # type: ignore[method-assign]

    def _wait_until_near_target(target_psi: float, **kwargs: Any) -> bool:
        waits.append(target_psi)
        wait_timeouts.append(float(kwargs['timeout_s']))
        return True

    executor._wait_until_near_target = _wait_until_near_target  # type: ignore[method-assign]

    bounds = (
        convert_pressure(400.0, 'Torr', 'PSI'),
        convert_pressure(563.0, 'Torr', 'PSI'),
    )
    executor._cycle_phase_runner.run_pre_approach('vacuum', bounds)

    assert waits
    assert convert_pressure(waits[0], 'PSI', 'Torr') < 400.0
    assert commanded[0] == pytest.approx(14.7, rel=1e-6)
    assert commanded[1] == pytest.approx(waits[0], rel=1e-6)
    assert wait_timeouts[0] < executor._edge_timeout_s


def test_pressure_decreasing_pre_approach_resets_above_deactivation_side() -> None:
    setup = TestSetup(
        part_id='SPS02072-02',
        sequence_id='300',
        units_code='19',
        units_label='mmHg @ 0 C',
        activation_direction='Decreasing',
        activation_target=400.0,
        pressure_reference='gauge',
        terminals={},
        bands={
            'increasing': {'lower': float('-inf'), 'upper': 500.0},
            'decreasing': {'lower': 390.0, 'upper': 410.0},
            'reset': {'lower': float('-inf'), 'upper': float('inf')},
        },
        raw={},
    )
    port = _FakePort([True])
    executor = _TestExecutor(
        port_id='port_a',
        port=cast(Any, port),
        test_setup=setup,
        config={
            'control': {
                'cycling': {},
                'ramps': {'fast_cycle_rate_psi_per_sec': 5.0},
                'edge_detection': {'overshoot_beyond_limit_percent': 10.0},
                'debounce': {},
            },
        },
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
    )
    commanded: list[float] = []
    waits: list[float] = []
    tolerances: list[float] = []
    executor._read_pressure_and_switch_state = lambda: (0.0, True)  # type: ignore[method-assign]
    executor._set_pressure_or_raise = lambda pressure: commanded.append(pressure)  # type: ignore[method-assign]

    def _wait_until_near_target(target_psi: float, **kwargs: Any) -> bool:
        waits.append(target_psi)
        tolerances.append(float(kwargs['tolerance_psi']))
        return True

    executor._wait_until_near_target = _wait_until_near_target  # type: ignore[method-assign]

    min_psi = convert_pressure(390.0, 'mmHg @ 0 C', 'PSI')
    max_psi = convert_pressure(500.0, 'mmHg @ 0 C', 'PSI')
    overshoot = 0.5
    executor._cycle_phase_runner.run_pre_approach('pressure', (min_psi, max_psi))

    assert waits == [pytest.approx(max_psi + overshoot)]
    assert commanded == [
        pytest.approx(14.7),
        pytest.approx(14.7 + max_psi + overshoot),
    ]
    assert tolerances[0] < overshoot


def test_pressure_decreasing_activation_edge_allowed_up_to_reset_band() -> None:
    """SPS02072-02 trips near the reset/deactivation side, above the 50% midpoint."""
    setup = TestSetup(
        part_id='SPS02072-02',
        sequence_id='300',
        units_code='19',
        units_label='mmHg @ 0 C',
        activation_direction='Decreasing',
        activation_target=400.0,
        pressure_reference='gauge',
        terminals={},
        bands={
            'increasing': {'lower': float('-inf'), 'upper': 500.0},
            'decreasing': {'lower': 390.0, 'upper': 410.0},
            'reset': {'lower': float('-inf'), 'upper': float('inf')},
        },
        raw={},
    )
    executor = _TestExecutor(
        port_id='port_a',
        port=cast(Any, _FakePort([True])),
        test_setup=setup,
        config={'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}}},
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
    )

    assert executor._resolve_sweep_mode() == 'pressure'
    assert executor._cycle_edge_pressure_allowed('activation', 8.87) is True
    assert executor._cycle_edge_pressure_allowed('activation', 9.50) is True
    assert executor._cycle_edge_pressure_allowed('activation', 10.20) is False


def test_decreasing_pressure_activation_prep_not_skipped_when_switch_wrong_state() -> None:
    port = _FakePort([True])
    setup = TestSetup(
        part_id='SPS02072-02',
        sequence_id='300',
        units_code='19',
        units_label='mmHg @ 0 C',
        activation_direction='Decreasing',
        activation_target=400.0,
        pressure_reference='gauge',
        terminals={},
        bands={
            'increasing': {'lower': float('-inf'), 'upper': 500.0},
            'decreasing': {'lower': 390.0, 'upper': 410.0},
            'reset': {'lower': float('-inf'), 'upper': float('inf')},
        },
        raw={},
    )
    min_psi = convert_pressure(390.0, 'mmHg @ 0 C', 'PSI')
    max_psi = convert_pressure(500.0, 'mmHg @ 0 C', 'PSI')
    nudge_target = min(15.35, max_psi + 0.5)
    readings = [
        (max_psi + 0.5, True),
        (nudge_target, True),
        (nudge_target, False),
    ]
    idx = {'value': 0}

    def _read_state() -> tuple[float, bool]:
        pressure, switch = readings[min(idx['value'], len(readings) - 1)]
        idx['value'] += 1
        return pressure, switch

    executor = _TestExecutor(
        port_id='port_a',
        port=cast(Any, port),
        test_setup=setup,
        config={'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}}},
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
    )
    executor._read_pressure_and_switch_state = _read_state  # type: ignore[method-assign]

    executor._prepare_switch_for_cycle_edge(
        sweep_mode='pressure',
        min_psi=min_psi,
        max_psi=max_psi,
        direction=-1,
        edge_type='activation',
        overshoot=0.5,
        hw_min_psi=-14.65,
        hw_max_psi=15.35,
    )

    assert port.set_pressure_calls
    assert port.set_pressure_calls[-1] == pytest.approx(14.7 + nudge_target)


def test_pressure_increasing_pre_approach_resets_below_deactivation_side() -> None:
    setup = TestSetup(
        part_id='SPS-PRESSURE-INCREASING',
        sequence_id='300',
        units_code='1',
        units_label='PSI',
        activation_direction='Increasing',
        activation_target=10.0,
        pressure_reference='gauge',
        terminals={},
        bands={
            'increasing': {'lower': 9.5, 'upper': 10.5},
            'decreasing': {'lower': 8.0, 'upper': 9.0},
            'reset': {'lower': float('-inf'), 'upper': float('inf')},
        },
        raw={},
    )
    executor = _TestExecutor(
        port_id='port_a',
        port=cast(Any, _FakePort([True])),
        test_setup=setup,
        config={
            'control': {
                'cycling': {},
                'ramps': {'fast_cycle_rate_psi_per_sec': 5.0},
                'edge_detection': {'overshoot_beyond_limit_percent': 10.0},
                'debounce': {},
            },
        },
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
    )
    commanded: list[float] = []
    waits: list[float] = []
    executor._read_pressure_and_switch_state = lambda: (14.0, False)  # type: ignore[method-assign]
    executor._set_pressure_or_raise = lambda pressure: commanded.append(pressure)  # type: ignore[method-assign]
    executor._wait_until_near_target = (  # type: ignore[method-assign]
        lambda target_psi, **_kwargs: waits.append(target_psi) or True
    )

    executor._cycle_phase_runner.run_pre_approach('pressure', (8.0, 10.5))

    assert waits == [pytest.approx(7.5)]
    assert commanded == [
        pytest.approx(14.7),
        pytest.approx(22.2),
    ]


def test_adaptive_cycle_target_shortens_later_vacuum_cycles() -> None:
    executor = _build_executor(_FakePort([True]))
    executor._cycle_activation_samples = [9.6]
    executor._cycle_deactivation_samples = [10.7]

    activation = executor._adaptive_cycle_target(
        'activation',
        full_target=3.5,
        direction=-1,
        bounds=(9.2, 12.5),
        hw_bounds=(0.0, 115.0),
    )
    deactivation = executor._adaptive_cycle_target(
        'deactivation',
        full_target=13.0,
        direction=-1,
        bounds=(9.2, 12.5),
        hw_bounds=(0.0, 115.0),
    )

    assert activation == pytest.approx(8.6)
    assert deactivation == pytest.approx(11.7)


def test_cycle_edge_wait_falls_back_to_full_target_when_adaptive_misses() -> None:
    setup = TestSetup(
        part_id='16039',
        sequence_id='399',
        units_code='1',
        units_label='PSI',
        activation_direction='Decreasing',
        activation_target=10.0,
        pressure_reference='gauge',
        terminals={},
        bands={
            'increasing': {'lower': 10.5, 'upper': 11.2},
            'decreasing': {'lower': 7.8, 'upper': 8.5},
            'reset': {'lower': 7.0, 'upper': 12.5},
        },
        raw={},
    )
    sim = _FlowSimulator(14.7, 8.0, 10.8, -1, 14.7, 14.7, max_step_psi=0.7)
    executor, port, captured = _build_flow_executor(setup, sim)
    executor._lock_alicat_setpoint_reference()
    executor._cycle_debounce_state = SpdtDebounceState()
    executor._set_pressure_or_raise(10.0)

    detected, diagnostic = executor._wait_for_cycle_edge(
        target_psi=10.0,
        direction=-1,
        edge_type='activation',
        samples_before=0,
        timeout_s=3.0,
        fallback_target_psi=7.0,
    )

    assert captured['errors'] == []
    assert diagnostic is None
    assert detected is True
    assert any(call == pytest.approx(7.0) for call in port.set_pressure_calls)


@dataclass
class _FlowSimulator:
    atmosphere_psi: float
    activation_edge_psi: float
    deactivation_edge_psi: float
    activation_direction: int
    pressure_psi: float
    target_psi: float
    switch_activated: bool = False
    max_step_psi: float = 0.45
    tick: int = 0

    def step(self) -> PortReading:
        delta = self.target_psi - self.pressure_psi
        if abs(delta) <= self.max_step_psi:
            self.pressure_psi = self.target_psi
        elif delta > 0:
            self.pressure_psi += self.max_step_psi
        else:
            self.pressure_psi -= self.max_step_psi
        if self.activation_direction < 0:
            if not self.switch_activated and self.pressure_psi <= self.activation_edge_psi:
                self.switch_activated = True
            elif self.switch_activated and self.pressure_psi >= self.deactivation_edge_psi:
                self.switch_activated = False
        else:
            if not self.switch_activated and self.pressure_psi >= self.activation_edge_psi:
                self.switch_activated = True
            elif self.switch_activated and self.pressure_psi <= self.deactivation_edge_psi:
                self.switch_activated = False
        self.tick += 1
        ts = self.tick * 0.02
        return PortReading(
            transducer=TransducerReading(voltage=2.5, pressure=self.pressure_psi, pressure_raw=self.pressure_psi, pressure_reference='absolute', timestamp=ts),
            alicat=AlicatReading(pressure=self.pressure_psi, setpoint=self.target_psi, timestamp=ts, gauge_pressure=self.pressure_psi - self.atmosphere_psi, barometric_pressure=self.atmosphere_psi),
            switch=SwitchState(no_active=self.switch_activated, nc_active=not self.switch_activated, timestamp=ts),
            timestamp=ts,
        )


class _FlowAlicat:
    def configure_units_from_ptp(self, _units_code: str) -> bool:
        return True

    def cancel_hold(self) -> bool:
        return True

    def set_ramp_rate(self, _rate: float) -> bool:
        return True


class _FlowPort:
    def __init__(self, sim: _FlowSimulator) -> None:
        self._sim = sim
        self.alicat = _FlowAlicat()
        self.set_pressure_calls: list[float] = []

    def set_pressure(self, command_psi: float) -> bool:
        self.set_pressure_calls.append(command_psi)
        baro = self._sim.atmosphere_psi
        # Simulator tracks absolute line pressure; negative Alicat commands are PSIG.
        self._sim.target_psi = command_psi + baro if command_psi < 0.0 else command_psi
        return True

    def set_solenoid(self, to_vacuum: bool) -> bool:
        return True

    def vent_to_atmosphere(self) -> bool:
        self._sim.target_psi = self._sim.atmosphere_psi
        return True


def _flow_config() -> dict[str, Any]:
    return {
        'control': {
            'cycling': {'num_cycles': 3},
            'ramps': {'precision_sweep_rate_torr_per_sec': 18.0, 'precision_edge_rate_torr_per_sec': 18.0},
            'edge_detection': {'overshoot_beyond_limit_percent': 10.0, 'timeout_sec': 4.0},
            'debounce': {'stable_sample_count': 2, 'min_edge_interval_ms': 0},
        },
    }


def _build_flow_executor(setup: TestSetup, sim: _FlowSimulator) -> tuple[_TestExecutor, _FlowPort, dict[str, Any]]:
    port = _FlowPort(sim)
    captured: dict[str, Any] = {'cycling_complete': False, 'edges': None, 'errors': []}
    executor = _TestExecutor(
        port_id='port_b',
        port=cast(Any, port),
        test_setup=setup,
        config=_flow_config(),
        get_latest_reading=lambda _pid: sim.step(),
        get_barometric_psi=lambda _pid: sim.atmosphere_psi,
        on_cycling_complete=lambda: captured.__setitem__('cycling_complete', True),
        on_edges_captured=lambda a, d: captured.__setitem__('edges', (a, d)),
        on_error=lambda message: captured['errors'].append(message),
    )
    ptp_ref = str(setup.pressure_reference or 'absolute').strip().lower()
    executor._alicat_setpoint_ref = ptp_ref
    return executor, port, captured


def test_executor_control_pressure_prefers_transducer_over_stale_alicat() -> None:
    setup = TestSetup(
        part_id='17025',
        sequence_id='399',
        units_code='1',
        units_label='PSI',
        activation_direction='Increasing',
        activation_target=20.0,
        pressure_reference='absolute',
        terminals={},
        bands={},
        raw={},
    )
    sim = _FlowSimulator(14.7, 7.8, 9.2, -1, 14.7, 14.7)
    executor, _port, _captured = _build_flow_executor(setup, sim)
    reading = build_port_reading(transducer_pressure=12.0, alicat_pressure=25.0)
    assert executor._reading_pressure_abs_psi(reading) == pytest.approx(12.0)


def _run_full_flow_sim(port_key: str) -> None:
    setup = TestSetup(
        part_id='17025',
        sequence_id='399',
        units_code='21',
        units_label='Torr',
        activation_direction='Decreasing',
        activation_target=400.0,
        pressure_reference='absolute',
        terminals={},
        bands={'increasing': {'lower': 550.0, 'upper': 600.0}, 'decreasing': {'lower': 390.0, 'upper': 410.0}, 'reset': {'lower': 360.0, 'upper': 370.0}},
        raw={},
    )
    sim = _FlowSimulator(14.7, 7.8, 9.2, -1, 14.7, 14.7)
    executor, port, captured = _build_flow_executor(setup, sim)
    executor._port_id = port_key
    executor._run()
    assert captured['errors'] == [], f'{port_key}: {captured["errors"]}'
    assert captured['cycling_complete'] is True
    assert captured['edges'] is not None
    assert len(port.set_pressure_calls) >= 3


def test_executor_full_flow_cycle_and_precision_port_a() -> None:
    _run_full_flow_sim('port_a')


def test_executor_full_flow_cycle_and_precision_port_b() -> None:
    _run_full_flow_sim('port_b')


def test_executor_precision_failure_message_identifies_second_edge() -> None:
    setup = TestSetup(
        part_id='17025',
        sequence_id='399',
        units_code='21',
        units_label='Torr',
        activation_direction='Decreasing',
        activation_target=400.0,
        pressure_reference='absolute',
        terminals={},
        bands={},
        raw={},
    )
    errors: list[str] = []
    port = _FakePort([True])
    executor = _TestExecutor(
        port_id='port_b',
        port=cast(Any, port),
        test_setup=setup,
        config={'control': {'cycling': {'num_cycles': 1}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}}},
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
        on_error=errors.append,
    )

    executor._ensure_alicat_units = lambda: None
    executor._resolve_sweep_mode = lambda: 'pressure'
    executor._resolve_sweep_bounds = lambda: (0.0, 2.0)
    executor._cycle_phase_runner.run_pre_approach = lambda _mode, _bounds: None
    executor._run_single_cycle = lambda _mode, _bounds: None

    def _force_second_edge_failure(
        _mode: str,
        _bounds: tuple[float, float],
        skip_atmosphere_gate: bool = False,
    ) -> None:
        del skip_atmosphere_gate
        executor._last_precision_missing_edge = 'second'
        return None

    executor._run_precision_sweep = _force_second_edge_failure
    executor._run()

    assert errors
    assert 'Deactivation edge not detected during precision return-sweep' in errors[0]
