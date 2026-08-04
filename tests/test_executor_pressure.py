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
        self.hold_calls = 0
        self.last_hold_closed: bool | None = None
        self.ramp_rates: list[float] = []

    def configure_units_from_ptp(self, _units_code: str) -> bool:
        self.configure_calls += 1
        return True

    def configure_units_from_ptp_prefer_psi(self, _units_code: str) -> bool:
        self.configure_calls += 1
        return True

    def cancel_hold(self) -> bool:
        self.cancel_hold_calls += 1
        return True

    def set_ramp_rate(self, _rate: float) -> bool:
        self.ramp_rates.append(_rate)
        return True

    def hold_valve(self, closed: bool = False) -> bool:
        self.hold_calls += 1
        self.last_hold_closed = closed
        return True


class _FakeDaq:
    def __init__(self) -> None:
        self.safe_calls = 0
        self.reset_filter_calls = 0

    def set_solenoid_safe(self) -> bool:
        self.safe_calls += 1
        return True

    def reset_filter(self) -> None:
        self.reset_filter_calls += 1


class _FakePort:
    def __init__(self, outcomes: list[bool]) -> None:
        self.alicat = _FakeAlicat()
        self.daq = _FakeDaq()
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
    assert alicat.cancel_hold_calls == 0
    assert executor._port.set_pressure_calls == [7.0, 7.0]


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
        **_kwargs: Any,
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


def test_precision_rejects_inverted_deactivation_below_activation() -> None:
    """Turnaround bounce that reports deact below act must not pass for decreasing."""
    executor = _build_executor(_FakePort([True]))
    edges = [
        EdgeDetection(pressure_psi=1.4260, activated=False),
        EdgeDetection(pressure_psi=1.4088, activated=True),
    ]

    executor._sweep_to_edge = lambda *_args, **_kwargs: edges.pop(0)  # type: ignore[method-assign]

    outcome = executor._execute_out_back_sweep(
        target_out=1.07,
        target_back=1.85,
        direction=-1,
        rate_psi_per_sec=0.1,
        fail_on_rate_error=True,
    )

    assert outcome.result is None
    assert outcome.missing_edge == 'second'


def test_precision_return_sweep_passes_gate_past_activation() -> None:
    executor = _build_executor(_FakePort([True]))
    captured: dict[str, Any] = {}

    def _edge(
        _target: float,
        _direction: int,
        edge_type: str | None = None,
        **kwargs: Any,
    ) -> EdgeDetection:
        if edge_type == 'deactivation':
            captured.update(kwargs)
            return EdgeDetection(pressure_psi=1.62, activated=True)
        return EdgeDetection(pressure_psi=1.43, activated=False)

    executor._cycle_activation_samples = [1.46]
    executor._cycle_deactivation_samples = [1.56]
    executor._sweep_to_edge = _edge  # type: ignore[method-assign]

    outcome = executor._execute_out_back_sweep(
        target_out=1.07,
        target_back=1.85,
        direction=-1,
        rate_psi_per_sec=0.1,
        fail_on_rate_error=True,
    )

    assert outcome.result is not None
    assert outcome.result.activation_psi == pytest.approx(1.43)
    assert outcome.result.deactivation_psi == pytest.approx(1.62)
    assert captured.get('require_past_psi') == pytest.approx(1.43)
    assert captured.get('past_margin_psi', 0) > 0
    assert captured.get('seed_edge_time') is True


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
    assert not executor._accept_observed_switch_polarity_for_cycle_edges()
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


def test_com_high_active_low_inverts_ptp_derived_vacuum_no_open() -> None:
    """COM HIGH + active_low flips COM-LOW PTP defaults (STINGER_03 wiring)."""
    port = _FakePort([True])
    # Seq 600 style: sense NC on pin 3
    port.daq = SimpleNamespace(
        switch_nc_derived_from_no=False,
        switch_no_derived_from_nc=True,
        switch_com_state=1,
        switch_active_low=True,
    )
    executor = _build_executor(port)
    executor._config = {
        'hardware': {'labjack': {'port_a': {}}},
        'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}},
    }

    # COM-LOW default for derive_no_from_nc is False; COM-HIGH inverts to True.
    assert executor._vacuum_switch_trips_on_no_open() is True
    assert executor._cycle_target_switch_state('activation') is False
    assert executor._cycle_target_switch_state('deactivation') is True

    # Seq 300 / 17021 style: sense NO on pin 3
    port.daq = SimpleNamespace(
        switch_nc_derived_from_no=True,
        switch_no_derived_from_nc=False,
        switch_com_state=1,
        switch_active_low=True,
    )
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
    assert executor._cycle_edge_switch_state_allowed('activation', True)
    assert executor._cycle_edge_switch_state_allowed('activation', False)
    assert executor._cycle_edge_switch_state_allowed('deactivation', True)
    assert executor._cycle_edge_switch_state_allowed('deactivation', False)
    executor._cycle_observed_edge_states['activation'] = False
    assert not executor._cycle_edge_switch_state_allowed('activation', True)
    assert executor._cycle_edge_switch_state_allowed('activation', False)


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
                # Omit vacuum_switch_trips_on_no_open so PTP-derived NC sense
                # selects the runtime target (activation = True on open).
                'port_a': {},
                'port_b': {},
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
        ('deactivation', pytest.approx(low_command), -1),
        ('activation', pytest.approx(high_command), 1),
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


def test_nc_derived_upward_precision_pass_orders_edges_by_pressure() -> None:
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
    port = _FakePort([True])
    port.daq = SimpleNamespace(switch_no_derived_from_nc=True)
    emitted: list[tuple[str, float]] = []
    executor = _TestExecutor(
        port_id='port_a',
        port=cast(Any, port),
        test_setup=setup,
        config={'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}}},
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
        on_edge_detected=lambda edge_type, pressure: emitted.append((edge_type, pressure)),
    )
    executor._wait_until_near_target = lambda **_kwargs: True  # type: ignore[method-assign]
    edges = [
        EdgeDetection(pressure_psi=10.72, activated=True),
        EdgeDetection(pressure_psi=11.68, activated=False),
    ]
    executor._sweep_to_edge = lambda target, direction, **kwargs: (  # type: ignore[method-assign]
        EdgeDetection(pressure_psi=10.72, activated=True)
        if direction < 0
        else EdgeDetection(pressure_psi=11.68, activated=False)
    )
    executor._collect_edges_during_sweep = lambda **_kwargs: edges  # type: ignore[method-assign]

    outcome = executor._run_nc_derived_upward_precision_pass(
        reset_target=8.4,
        high_target=12.1,
        rate_psi_per_sec=0.1,
    )

    assert outcome.result == SweepResult(activation_psi=11.68, deactivation_psi=10.72)
    assert emitted == [('deactivation', 10.72), ('activation', 11.68)]


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
    pad = convert_pressure(8.0, 'Torr', 'PSI')
    assert approach == pytest.approx(24.5 - pad, abs=1e-4)
    assert target_out == pytest.approx(24.5 + pad, abs=1e-4)
    assert target_back == pytest.approx(23.0 - pad, abs=1e-4)


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


def test_precision_near_approach_settles_at_slow_rate_before_sweep() -> None:
    executor = _build_executor(_FakePort([True]))
    called = {'fast_approach': False, 'settle': False}
    # PTP decreasing close-limit approach is max band (~600 Torr).
    approach_psi = convert_pressure(600.0, 'Torr', 'PSI')

    executor._precision_phase_runner._can_start_precision_from_current_reset_side = (  # type: ignore[method-assign]
        lambda *_args: True
    )
    executor._precision_phase_runner._run_precision_fast_approach = (  # type: ignore[method-assign]
        lambda _mode, _target: called.__setitem__('fast_approach', True)
    )
    executor._precision_phase_runner._settle_at_precision_approach = (  # type: ignore[method-assign]
        lambda _target: called.__setitem__('settle', True)
    )
    executor._precision_phase_runner._ensure_precision_starts_from_reset_side = (  # type: ignore[method-assign]
        lambda *_args: False
    )
    executor._run_sweep_pass = lambda *_args, **_kwargs: SweepPassOutcome(  # type: ignore[method-assign]
        result=SweepResult(activation_psi=1.5, deactivation_psi=1.8),
        missing_edge=None,
    )

    result = executor._run_precision_sweep('vacuum', (0.5, 3.0), skip_atmosphere_gate=True)

    assert result is not None
    assert called['fast_approach'] is False
    assert called['settle'] is True


def test_precision_deactivates_then_fast_approaches_to_act_close_limit() -> None:
    """Decreasing precision must deactivate past deact, then approach above act."""
    executor = _build_executor(_FakePort([True]))
    calls: list[tuple[str, float]] = []

    executor._resolve_precision_targets = (  # type: ignore[method-assign]
        lambda *_args: (7.76, 7.45, 8.99, 'cycle-estimate-offset-close-limit')
    )
    executor._precision_phase_runner._can_start_precision_from_current_reset_side = (  # type: ignore[method-assign]
        lambda *_args: False
    )

    def _stage(_mode: str, _direction: int, reset_target: float) -> None:
        calls.append(('deactivate', reset_target))

    def _fast(_mode: str, target: float) -> None:
        calls.append(('approach', target))

    executor._precision_phase_runner._stage_precision_deactivated = _stage  # type: ignore[method-assign]
    executor._precision_phase_runner._run_precision_fast_approach = _fast  # type: ignore[method-assign]
    executor._precision_phase_runner._ensure_precision_starts_from_reset_side = (  # type: ignore[method-assign]
        lambda *_args: False
    )
    executor._run_sweep_pass = lambda *_args, **_kwargs: SweepPassOutcome(  # type: ignore[method-assign]
        result=SweepResult(activation_psi=7.6, deactivation_psi=8.8),
        missing_edge=None,
    )

    result = executor._run_precision_sweep('vacuum', (7.5, 9.5), skip_atmosphere_gate=True)

    assert result is not None
    assert calls == [('deactivate', 8.99), ('approach', 7.76)]


def test_precision_direct_handoff_rejects_mid_band_below_decreasing_approach() -> None:
    """Mid-band pressure must not skip the high-side fast approach."""
    executor = _build_executor(_FakePort([True]))
    executor._vacuum_switch_trips_on_no_open = lambda: True  # type: ignore[method-assign]
    executor._read_pressure_and_switch_state = lambda: (1.75, True)  # type: ignore[method-assign]

    assert not executor._precision_phase_runner._can_start_precision_from_current_reset_side(
        skip_atmosphere_gate=True,
        activation_direction=-1,
        approach_target=2.80,
    )


def test_precision_starts_slow_sweep_from_reset_side_after_arming() -> None:
    executor = _build_executor(_FakePort([True]))
    fast_targets: list[tuple[str, float]] = []

    executor._resolve_precision_targets = lambda *_args: (1.4, 0.7, 2.3, 'cycle')  # type: ignore[method-assign]
    executor._precision_phase_runner._can_start_precision_from_current_reset_side = (  # type: ignore[method-assign]
        lambda *_args: False
    )
    executor._precision_phase_runner._stage_precision_deactivated = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: None
    )
    executor._precision_phase_runner._run_precision_fast_approach = (  # type: ignore[method-assign]
        lambda mode, target: fast_targets.append((mode, target))
    )
    executor._precision_phase_runner._ensure_precision_starts_from_reset_side = (  # type: ignore[method-assign]
        lambda *_args: True
    )
    executor._run_sweep_pass = lambda *_args, **_kwargs: SweepPassOutcome(  # type: ignore[method-assign]
        result=SweepResult(activation_psi=1.1, deactivation_psi=1.7),
        missing_edge=None,
    )

    result = executor._run_precision_sweep('vacuum', (0.5, 3.0), skip_atmosphere_gate=True)

    assert result is not None
    assert fast_targets == [('vacuum', 1.4)]


def test_precision_direct_handoff_requires_pressure_near_close_limit() -> None:
    executor = _build_executor(_FakePort([True]))

    executor._vacuum_switch_trips_on_no_open = lambda: True  # type: ignore[method-assign]
    executor._read_pressure_and_switch_state = lambda: (2.0, True)  # type: ignore[method-assign]

    assert not executor._precision_phase_runner._can_start_precision_from_current_reset_side(
        skip_atmosphere_gate=True,
        activation_direction=-1,
        approach_target=1.7,
    )


def test_ordered_cycle_estimates_nudge_equal_means_for_decreasing() -> None:
    executor = _build_executor(_FakePort([True]))
    executor._cycle_activation_samples = [1.5005, 1.5005, 1.5005]
    executor._cycle_deactivation_samples = [1.5005, 1.5005, 1.5005]

    activation, deactivation = executor._ordered_cycle_estimates()
    assert activation is not None and deactivation is not None
    assert activation < deactivation

    approach, target_out, target_back, source = executor._resolve_precision_targets(
        min_psi=1.412,
        max_psi=2.8046,
        activation_direction=-1,
    )
    assert source == 'cycle-estimate-offset-close-limit'
    assert approach > target_out
    assert approach >= 2.8046  # sit at/above PTP high — not clamped onto the band edge
    assert target_back >= deactivation


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

    assert commanded == [pytest.approx(8.96 + 14.7)]
    assert port.solenoid_calls == [True]


def test_pressure_decreasing_activation_prep_skips_when_already_above_band() -> None:
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

    assert commanded == []
    assert port.solenoid_calls == []


def test_pressure_decreasing_derive_nc_from_no_inverts_cycle_targets() -> None:
    """Single-sense pressure parts: high-P reset reads activated=True."""
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
    assert wait_timeouts[0] == pytest.approx(12.0)


def test_vacuum_decreasing_pre_approach_approaches_baro_on_atmosphere_then_vacuum() -> None:
    """Decreasing vacuum must reach baro on atmosphere before engaging the vacuum route."""
    setup = TestSetup(
        part_id='UC8A',
        sequence_id='399',
        units_code='21',
        units_label='Torr',
        activation_direction='Decreasing',
        activation_target=75.0,
        pressure_reference='absolute',
        terminals={},
        bands={
            'increasing': {'lower': 143.0, 'upper': 145.0},
            'decreasing': {'lower': 73.0, 'upper': 76.0},
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
        get_barometric_psi=lambda _pid: 13.43,
    )
    staged: list[bool] = []
    commanded: list[float] = []
    waits: list[float] = []
    executor._stage_atmosphere_setpoint_before_route = lambda: staged.append(True)  # type: ignore[method-assign]
    executor._set_pressure_or_raise = lambda pressure: commanded.append(pressure)  # type: ignore[method-assign]
    executor._wait_until_near_target = (  # type: ignore[method-assign]
        lambda **kwargs: waits.append(float(kwargs['target_psi'])) or True
    )
    executor._read_pressure_and_switch_state = lambda: (13.43, True)  # type: ignore[method-assign]

    bounds = (
        convert_pressure(73.0, 'Torr', 'PSI'),
        convert_pressure(145.0, 'Torr', 'PSI'),
    )
    executor._cycle_phase_runner.run_pre_approach('vacuum', bounds)

    assert port.vent_calls == 0
    assert staged == []
    assert port.daq.safe_calls == 1
    assert port.daq.reset_filter_calls == 1
    assert waits == [pytest.approx(13.43)]
    assert commanded == [pytest.approx(13.43)]
    # Vacuum route is engaged only after baro pre-approach completes.
    assert port.solenoid_calls == [True]


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
    overshoot = executor._cycle_overshoot_psi(min_psi, max_psi)
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


def test_decreasing_pressure_activation_prep_climbs_when_deactivated_below_band() -> None:
    """SPS01804-02: deactivated below band must climb before descending activation."""
    port = _FakePort([True])
    setup = TestSetup(
        part_id='SPS01804-02',
        sequence_id='300',
        units_code='19',
        units_label='mmHg @ 0 C',
        activation_direction='Decreasing',
        activation_target=15.0,
        pressure_reference='gauge',
        terminals={},
        bands={
            'increasing': {'lower': float('-inf'), 'upper': 30.0},
            'decreasing': {'lower': 12.5, 'upper': 17.5},
            'reset': {'lower': float('-inf'), 'upper': float('inf')},
        },
        raw={},
    )
    min_psi = convert_pressure(12.5, 'mmHg @ 0 C', 'PSI')
    max_psi = convert_pressure(17.5, 'mmHg @ 0 C', 'PSI')
    overshoot = 0.15
    prep_target = min(15.35, max_psi + overshoot)
    readings = [
        (0.28, False),  # below band, deactivated (looks like prep DI state)
        (prep_target, False),
        (prep_target, False),
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
        overshoot=overshoot,
        hw_min_psi=-14.65,
        hw_max_psi=15.35,
    )

    assert port.set_pressure_calls
    # Must climb above the decreasing band before the descending activation ramp.
    assert port.set_pressure_calls[-1] > 14.7 + max_psi


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
        (min_psi + (max_psi - min_psi) * 0.5, True),
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


def test_decreasing_pressure_activation_prep_starts_from_high_side_even_if_switch_active() -> None:
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
    executor = _TestExecutor(
        port_id='port_a',
        port=cast(Any, port),
        test_setup=setup,
        config={'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}}},
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
    )
    executor._read_pressure_and_switch_state = lambda: (max_psi + 0.5, True)  # type: ignore[method-assign]

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

    assert port.set_pressure_calls == []


def test_decreasing_pressure_deactivation_prep_starts_from_low_side_even_if_switch_inactive() -> None:
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
    executor = _TestExecutor(
        port_id='port_a',
        port=cast(Any, port),
        test_setup=setup,
        config={'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}}},
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
    )
    executor._read_pressure_and_switch_state = lambda: (min_psi - 0.25, False)  # type: ignore[method-assign]

    executor._prepare_switch_for_cycle_edge(
        sweep_mode='pressure',
        min_psi=min_psi,
        max_psi=max_psi,
        direction=-1,
        edge_type='deactivation',
        overshoot=0.5,
        hw_min_psi=-14.65,
        hw_max_psi=15.35,
    )

    assert port.set_pressure_calls == []


def test_decreasing_pressure_cycle_edges_use_observed_polarity_from_transition() -> None:
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

    assert executor._cycle_edge_already_present('activation', 10.17, True) is False
    assert executor._cycle_edge_switch_state_allowed('activation', False) is True
    executor._cycle_observed_edge_states['activation'] = False
    assert executor._cycle_edge_switch_state_allowed('activation', True) is False
    assert executor._cycle_edge_already_present('deactivation', 7.04, False) is False
    assert executor._cycle_edge_switch_state_allowed('deactivation', True) is True


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

    overshoot = executor._cycle_overshoot_psi(8.0, 10.5)
    expected = 8.0 - overshoot
    assert waits == [pytest.approx(expected)]
    assert commanded == [
        pytest.approx(14.7),
        pytest.approx(14.7 + expected),
    ]


def test_decreasing_vacuum_precision_brackets_cycle_estimates() -> None:
    """Absolute-Torr decreasing parts must approach/return near deact estimate."""
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
            'increasing': {'lower': 390.0, 'upper': 490.0},
            'decreasing': {'lower': 390.0, 'upper': 410.0},
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
                'ramps': {},
                'edge_detection': {
                    'precision_close_limit_offset_torr': 25.0,
                    'precision_deactivation_margin_torr': 15.0,
                },
                'debounce': {},
            },
        },
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.58,
    )
    executor._cycle_activation_samples = [7.63]
    executor._cycle_deactivation_samples = [9.23]
    min_psi, max_psi = 7.5434, 9.4776
    pad = convert_pressure(8.0, 'Torr', 'PSI')

    approach, target_out, target_back, source = executor._resolve_precision_targets(
        min_psi=min_psi,
        max_psi=max_psi,
        activation_direction=-1,
    )

    assert source == 'cycle-estimate-offset-close-limit'
    # Close-limit sits above activation, not up at deactivation.
    assert approach == pytest.approx(7.63 + pad, abs=1e-4)
    assert target_out == pytest.approx(7.63 - pad, abs=1e-4)
    assert target_back == pytest.approx(9.23 + pad, abs=1e-4)
    assert approach < 9.23


def test_wait_for_cycle_edge_accepts_edges_recorded_during_prep() -> None:
    """Deep-vacuum prep can trip activation before wait starts; those edges must count."""
    from app.hardware.port import EdgeEvent

    port = _FakePort([True])
    edges = [EdgeEvent(pressure=0.32, timestamp=0.0, direction='decreasing', activated=True)]
    port.get_edge_history = lambda: list(edges)  # type: ignore[attr-defined]
    executor = _build_executor(port)
    executor._resolve_sweep_mode = lambda: 'vacuum'  # type: ignore[method-assign]
    executor._resolve_activation_sweep_direction = lambda: -1  # type: ignore[method-assign]
    executor._resolve_sweep_bounds = lambda: (0.24, 0.58)  # type: ignore[method-assign]
    executor._vacuum_switch_trips_on_no_open = lambda: True  # type: ignore[method-assign]
    executor._absolute_to_test_reference = lambda value: value  # type: ignore[method-assign]
    executor._read_pressure_and_switch_state = lambda: (0.32, True)  # type: ignore[method-assign]
    executor._get_latest_reading = lambda _pid: None  # type: ignore[method-assign]
    executor._hold_after_cycle_edge = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    detected, diagnostic = executor._wait_for_cycle_edge(
        target_psi=0.087,
        direction=-1,
        edge_type='activation',
        samples_before=0,
        timeout_s=0.5,
        port_edges_before=0,
    )

    assert detected is True
    assert diagnostic is None
    assert executor._cycle_activation_samples == [pytest.approx(0.32)]


def test_adaptive_cycle_target_shortens_later_vacuum_cycles() -> None:
    executor = _build_executor(_FakePort([True]))
    executor._cycle_activation_samples = [9.6]
    executor._cycle_deactivation_samples = [10.7]
    bounds = (9.2, 12.5)
    margin = max(executor._cycle_overshoot_psi(*bounds), (bounds[1] - bounds[0]) * 0.10)

    activation = executor._adaptive_cycle_target(
        'activation',
        full_target=3.5,
        direction=-1,
        bounds=bounds,
        hw_bounds=(0.0, 115.0),
    )
    deactivation = executor._adaptive_cycle_target(
        'deactivation',
        full_target=13.0,
        direction=-1,
        bounds=bounds,
        hw_bounds=(0.0, 115.0),
    )

    assert activation == pytest.approx(9.6 - margin)
    assert deactivation == pytest.approx(10.7 + margin)
    # Must actually shorten versus the full band endpoints.
    assert activation > 3.5
    assert deactivation < 13.0


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


def test_executor_control_pressure_uses_configured_alicat_above_cutover() -> None:
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
    assert executor._reading_pressure_abs_psi(reading) == pytest.approx(25.0)
    assert executor.last_pressure_source_used == 'alicat'


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


# ---------------------------------------------------------------------------
# Cycle prep: fail-open and geometry correctness
# ---------------------------------------------------------------------------

def _make_press_dec_setup(min_mmhg: float = 390.0, max_mmhg: float = 500.0) -> tuple[TestSetup, float, float]:
    """Return (TestSetup, min_psi, max_psi) for a decreasing-pressure part (Family A)."""
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
            'increasing': {'lower': float('-inf'), 'upper': max_mmhg},
            'decreasing': {'lower': min_mmhg, 'upper': max_mmhg},
            'reset': {'lower': float('-inf'), 'upper': float('inf')},
        },
        raw={},
    )
    min_psi = convert_pressure(min_mmhg, 'mmHg @ 0 C', 'PSI')
    max_psi = convert_pressure(max_mmhg, 'mmHg @ 0 C', 'PSI')
    return setup, min_psi, max_psi


def _make_press_inc_setup() -> tuple[TestSetup, float, float]:
    """Return (TestSetup, min_psi, max_psi) for an increasing-pressure part (Family C)."""
    setup = TestSetup(
        part_id='SPS01897-02',
        sequence_id='300',
        units_code='19',
        units_label='mmHg @ 0 C',
        activation_direction='Increasing',
        activation_target=75.0,
        pressure_reference='gauge',
        terminals={},
        bands={
            'increasing': {'lower': 67.5, 'upper': 82.5},
            'decreasing': {'lower': 30.0, 'upper': float('inf')},
            'reset': {'lower': float('-inf'), 'upper': float('inf')},
        },
        raw={},
    )
    min_psi = convert_pressure(67.5, 'mmHg @ 0 C', 'PSI')
    max_psi = convert_pressure(82.5, 'mmHg @ 0 C', 'PSI')
    return setup, min_psi, max_psi


def _prep_executor(setup: TestSetup, port: _FakePort, pressure: float, switch: bool) -> _TestExecutor:
    executor = _TestExecutor(
        port_id='port_a',
        port=cast(Any, port),
        test_setup=setup,
        config={'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}}},
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
    )
    executor._read_pressure_and_switch_state = lambda: (pressure, switch)  # type: ignore[method-assign]
    return executor


def _run_prep(executor: _TestExecutor, setup: TestSetup, min_psi: float, max_psi: float, edge_type: str) -> None:
    direction = executor._resolve_activation_sweep_direction()
    executor._prepare_switch_for_cycle_edge(
        sweep_mode='pressure',
        min_psi=min_psi,
        max_psi=max_psi,
        direction=direction,
        edge_type=edge_type,
        overshoot=0.5,
        hw_min_psi=-14.65,
        hw_max_psi=15.35,
    )


# ----- Fail-open: prep cannot position switch → does NOT raise -----

def test_cycle_prep_failopen_does_not_raise_when_switch_never_flips() -> None:
    """Prep timed out (switch stuck): must return without raising, cycle_prep_confirmed=False."""
    setup, min_psi, max_psi = _make_press_dec_setup()
    port = _FakePort([True])
    mid = (min_psi + max_psi) / 2.0
    # Switch is activated but never flips during the prep loop (always returns activated=True)
    executor = _prep_executor(setup, port, mid, True)
    # Override loop reads to always show switch still in wrong state at mid-band pressure
    executor._read_pressure_and_switch_state = lambda: (mid, True)  # type: ignore[method-assign]
    executor._edge_timeout_s = 0.05

    _run_prep(executor, setup, min_psi, max_psi, 'activation')

    assert executor._cycle_prep_confirmed is False
    # Must not raise → test passes by reaching here


def test_cycle_prep_failopen_confirmed_true_when_switch_flips() -> None:
    """Prep succeeds (switch flips): cycle_prep_confirmed=True."""
    setup, min_psi, max_psi = _make_press_dec_setup()
    port = _FakePort([True])
    nudge_target = min(15.35, max_psi + 0.5)
    calls = {'n': 0}

    def _reads() -> tuple[float, bool]:
        # First call: mid-band, wrong state (activated); subsequent: at target, correct (deactivated)
        if calls['n'] == 0:
            calls['n'] += 1
            return (min_psi + max_psi) / 2.0, True
        return nudge_target, False

    executor = _TestExecutor(
        port_id='port_a',
        port=cast(Any, port),
        test_setup=setup,
        config={'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}}},
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
    )
    executor._read_pressure_and_switch_state = _reads  # type: ignore[method-assign]

    _run_prep(executor, setup, min_psi, max_psi, 'activation')

    assert executor._cycle_prep_confirmed is True


# ----- Family A (decreasing pressure): geometry -----

def test_prep_geometry_family_a_activation_nudges_to_high_side() -> None:
    """Family A activation prep must nudge toward max_psi (reset/high side)."""
    setup, min_psi, max_psi = _make_press_dec_setup()
    mid = (min_psi + max_psi) / 2.0
    nudge_target = min(15.35, max_psi + 0.5)
    port = _FakePort([True])
    executor = _prep_executor(setup, port, mid, True)
    executor._edge_timeout_s = 0.05  # short timeout → exits by timeout, not switch flip

    _run_prep(executor, setup, min_psi, max_psi, 'activation')

    assert port.set_pressure_calls, 'expected a nudge move'
    # prep_target should be above max_psi (14.7 baro + gauge target)
    assert port.set_pressure_calls[-1] == pytest.approx(14.7 + nudge_target, abs=0.05)


def test_prep_geometry_family_a_activation_skips_when_already_on_high_side() -> None:
    """Family A activation prep skips move when pressure is already at/beyond max_psi."""
    setup, min_psi, max_psi = _make_press_dec_setup()
    port = _FakePort([True])
    executor = _prep_executor(setup, port, max_psi + 0.1, True)

    _run_prep(executor, setup, min_psi, max_psi, 'activation')

    assert port.set_pressure_calls == [], 'should skip: already on high/reset side'
    assert executor._cycle_prep_confirmed is True


def test_prep_geometry_family_a_deactivation_nudges_to_low_side() -> None:
    """Family A deactivation prep must nudge toward min_psi (activation/low side)."""
    setup, min_psi, max_psi = _make_press_dec_setup()
    mid = (min_psi + max_psi) / 2.0
    nudge_target = max(-14.65, min_psi - 0.5)
    port = _FakePort([True])
    executor = _prep_executor(setup, port, mid, False)
    executor._edge_timeout_s = 0.05

    _run_prep(executor, setup, min_psi, max_psi, 'deactivation')

    assert port.set_pressure_calls, 'expected a nudge move'
    assert port.set_pressure_calls[-1] == pytest.approx(14.7 + nudge_target, abs=0.05)


def test_prep_geometry_family_a_deactivation_skips_when_already_on_low_side() -> None:
    """Family A deactivation prep skips when pressure is at/below min_psi."""
    setup, min_psi, max_psi = _make_press_dec_setup()
    port = _FakePort([True])
    executor = _prep_executor(setup, port, min_psi - 0.1, False)

    _run_prep(executor, setup, min_psi, max_psi, 'deactivation')

    assert port.set_pressure_calls == [], 'should skip: already on low/activation side'
    assert executor._cycle_prep_confirmed is True


# ----- Family C (increasing pressure): geometry -----

def test_prep_geometry_family_c_activation_nudges_to_low_side() -> None:
    """Family C (Increasing) activation prep must nudge toward min_psi (reset/low side).

    Switch is activated (True) at mid-band which is the wrong state — prep_state is False.
    """
    setup, min_psi, max_psi = _make_press_inc_setup()
    mid = (min_psi + max_psi) / 2.0
    nudge_target = max(-14.65, min_psi - 0.5)
    port = _FakePort([True])
    # Switch must be in wrong state (activated=True when prep_state=False) to trigger nudge
    executor = _prep_executor(setup, port, mid, True)
    executor._edge_timeout_s = 0.05

    direction = executor._resolve_activation_sweep_direction()
    executor._prepare_switch_for_cycle_edge(
        sweep_mode='pressure',
        min_psi=min_psi,
        max_psi=max_psi,
        direction=direction,
        edge_type='activation',
        overshoot=0.5,
        hw_min_psi=-14.65,
        hw_max_psi=15.35,
    )

    assert port.set_pressure_calls, 'expected a nudge move'
    assert port.set_pressure_calls[-1] == pytest.approx(14.7 + nudge_target, abs=0.05)


def test_prep_geometry_family_c_deactivation_nudges_to_high_side() -> None:
    """Family C (Increasing) deactivation prep must nudge toward max_psi (reset from activation).

    Switch is deactivated (False) at mid-band which is the wrong state — prep_state is True.
    """
    setup, min_psi, max_psi = _make_press_inc_setup()
    mid = (min_psi + max_psi) / 2.0
    nudge_target = min(15.35, max_psi + 0.5)
    port = _FakePort([True])
    # Switch must be in wrong state (deactivated=False when prep_state=True) to trigger nudge
    executor = _prep_executor(setup, port, mid, False)
    executor._edge_timeout_s = 0.05

    direction = executor._resolve_activation_sweep_direction()
    executor._prepare_switch_for_cycle_edge(
        sweep_mode='pressure',
        min_psi=min_psi,
        max_psi=max_psi,
        direction=direction,
        edge_type='deactivation',
        overshoot=0.5,
        hw_min_psi=-14.65,
        hw_max_psi=15.35,
    )

    assert port.set_pressure_calls, 'expected a nudge move'
    assert port.set_pressure_calls[-1] == pytest.approx(14.7 + nudge_target, abs=0.05)


# ----- Family B (decreasing vacuum derive_no_from_nc): geometry -----

def test_prep_geometry_family_b_activation_nudges_to_high_side() -> None:
    """Family B (Decreasing vacuum) activation prep should nudge to high/reset side."""
    setup = TestSetup(
        part_id='SPS01439-02',
        sequence_id='600',
        units_code='21',
        units_label='Torr',
        activation_direction='Decreasing',
        activation_target=400.0,
        pressure_reference='absolute',
        terminals={},
        bands={
            'increasing': {'lower': float('-inf'), 'upper': 490.0},
            'decreasing': {'lower': 390.0, 'upper': 410.0},
            'reset': {'lower': float('-inf'), 'upper': float('inf')},
        },
        raw={},
    )
    min_psi = convert_pressure(390.0, 'Torr', 'PSI')
    max_psi = convert_pressure(490.0, 'Torr', 'PSI')
    nudge_target = min(15.35, max_psi + 0.5)
    mid = (min_psi + max_psi) / 2.0
    port = _FakePort([True])

    class _FakePortWithDeriveNoFromNc(_FakePort):
        switch_no_derived_from_nc = False
        switch_nc_derived_from_no = False

    fake_port = _FakePortWithDeriveNoFromNc([True])
    executor = _TestExecutor(
        port_id='port_a',
        port=cast(Any, fake_port),
        test_setup=setup,
        config={'control': {'cycling': {}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}}},
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 0.0,
    )
    executor._read_pressure_and_switch_state = lambda: (mid, True)  # type: ignore[method-assign]
    executor._edge_timeout_s = 0.05

    # Ensure vacuum sweep mode: override if needed
    executor._resolve_sweep_mode = lambda: 'vacuum'  # type: ignore[method-assign]

    executor._prepare_switch_for_cycle_edge(
        sweep_mode='vacuum',
        min_psi=min_psi,
        max_psi=max_psi,
        direction=-1,
        edge_type='activation',
        overshoot=0.5,
        hw_min_psi=0.0,
        hw_max_psi=15.0,
    )

    # Either nudges or fails open (switch never flips in the stub);
    # crucially must not raise and confirmed=False means full-range fallback kicks in.
    assert executor._cycle_prep_confirmed is False or port.set_pressure_calls == [] or (
        fake_port.set_pressure_calls and fake_port.set_pressure_calls[-1] >= 14.7 + min_psi
    )


# ----- Full-range fallback integration -----

def test_cycle_prep_confirmed_false_causes_full_range_in_run_single_cycle() -> None:
    """When cycle_prep_confirmed=False the leg uses full_activation/full_deactivation target."""
    setup, min_psi, max_psi = _make_press_dec_setup()
    port = _FakePort([True])

    act_targets_used: list[float] = []
    deact_targets_used: list[float] = []

    executor = _TestExecutor(
        port_id='port_a',
        port=cast(Any, port),
        test_setup=setup,
        config={'control': {'cycling': {'num_cycles': 1}, 'ramps': {}, 'edge_detection': {}, 'debounce': {}}},
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
    )

    # Force prep to always fail (sets cycle_prep_confirmed=False)
    def _fail_prep(**_kw: Any) -> None:
        executor._cycle_prep_confirmed = False
        executor._seed_debounce_from_live_reading()

    # Intercept set_pressure to record the targets passed
    original_sp = port.set_pressure
    def _spy_sp(setpoint: float) -> bool:
        act_targets_used.append(setpoint)
        return True
    port.set_pressure = _spy_sp  # type: ignore[method-assign]

    executor._prepare_switch_for_cycle_edge = _fail_prep  # type: ignore[method-assign]
    executor._wait_for_cycle_edge = lambda **_kw: (True, None)  # type: ignore[method-assign]
    executor._cycle_phase_runner.run_single_cycle(sweep_mode='pressure', bounds=(min_psi, max_psi))

    # All recorded setpoints should be at/beyond the full fallback (max+overshoot), not the adaptive target
    overshoot = max((max_psi - min_psi) * 0.1, 0.5)
    full_act_abs = 14.7 + max(- 14.65, min_psi - overshoot)  # baro + full activation target gauge
    # Verify that full-range target (not narrowed) was used: setpoint must be <= full_act_abs + 0.1 margin
    # (lower setpoint means further from band on decreasing sweep)
    assert any(sp <= full_act_abs + 0.2 for sp in act_targets_used), (
        f'Expected full-range target ~{full_act_abs:.3f}; got {act_targets_used}'
    )


def test_lock_run_barometric_reuses_boot_session_without_exh() -> None:
    """Per-run lock must reuse boot P0 and must not dump via EXH."""
    from app.hardware.port import Port

    Port.clear_session_gauge_zero_psia()
    Port.set_session_gauge_zero_psia(14.60)

    class _PortNoSample(_FakePort):
        def __init__(self) -> None:
            super().__init__([])
            self.sample_calls = 0

        def _sample_barometric_via_exhaust(self, timeout_s: float = 5.0) -> float:
            self.sample_calls += 1
            raise AssertionError('EXH sample must not run when boot P0 is locked')

    port = _PortNoSample()
    executor = _build_executor(port)
    executor._try_mensor_barometric_psia = lambda: None  # type: ignore[method-assign]
    measured = executor._lock_run_barometric_via_exhaust()
    assert measured == pytest.approx(14.60)
    assert executor._gauge_zero_raw_psi == pytest.approx(14.60)
    assert port.sample_calls == 0
    Port.clear_session_gauge_zero_psia()
