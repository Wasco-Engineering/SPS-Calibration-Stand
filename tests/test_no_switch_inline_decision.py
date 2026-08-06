from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.hardware.labjack import SwitchState
from app.hardware.port import PortReading
from app.services.state.port_state_machine import PortState, PortStateMachine, PortSubstate
from app.services.work_order_controller import WorkOrderController


class _FakeUiBridge:
    def __init__(self) -> None:
        self.info_messages: list[tuple[str, str]] = []

    def show_info_message(self, title: str, message: str) -> None:
        self.info_messages.append((title, message))


def _no_switch_sm(workflow: str = 'QAL16') -> PortStateMachine:
    sm = PortStateMachine('port_a')
    sm.set_workflow_type(workflow)
    sm.trigger('initialize_complete')
    sm.trigger('error', message='no_switch_detected')
    return sm


def test_no_switch_error_enters_inline_decision_without_popup() -> None:
    controller = WorkOrderController.__new__(WorkOrderController)
    sm = PortStateMachine('port_a')
    sm.set_workflow_type('QAL16')
    sm.trigger('initialize_complete')
    sm.trigger('start_test')
    sm.trigger('cycles_complete')

    ui_bridge = _FakeUiBridge()
    vents: list[str] = []
    releases: list[tuple[str, str]] = []
    controller._state_machines = {'port_a': sm}
    controller._ui_bridge = ui_bridge
    controller._vent_port = lambda port_id: vents.append(port_id)
    controller._release_precision_slot = (
        lambda port_id, reason: releases.append((port_id, reason))
    )

    controller._slot_trigger_error(
        'port_a',
        'No switch detected on port_a - switch state did not change during pressure ramp',
    )

    assert sm.current_state == PortState.ERROR.value
    assert sm.current_substate == PortSubstate.ERROR_NO_SWITCH.value
    assert ui_bridge.info_messages == []
    assert vents == ['port_a']
    assert releases == [('port_a', 'no-switch-failure')]


def test_active_test_cancel_starts_hardware_vent() -> None:
    """Cancel must start hardware vent; the state-machine callback only logs."""
    controller = WorkOrderController.__new__(WorkOrderController)
    sm = PortStateMachine('port_a')
    sm.set_workflow_type('QAL16')
    sm.trigger('initialize_complete')
    sm.trigger('start_test')
    vents: list[str] = []
    releases: list[tuple[str, str]] = []

    class _Executor:
        is_running = True

        def __init__(self) -> None:
            self.cancel_requested = False

        def request_cancel(self) -> None:
            self.cancel_requested = True

    executor = _Executor()
    controller._state_machines = {'port_a': sm}
    controller._test_executors = {'port_a': executor}
    controller._restore_normal_viz = lambda _port_id: None
    controller._cancel_hw_action = lambda _port_id: None
    controller._remove_precision_waiter = lambda _port_id: None
    controller._release_precision_slot = (
        lambda port_id, reason: releases.append((port_id, reason))
    )
    controller._vent_port = lambda port_id: vents.append(port_id)

    controller._on_cancel('port_a')

    assert executor.cancel_requested is True
    assert vents == ['port_a']
    assert releases == [('port_a', 'cancel')]


def test_no_switch_retry_persists_null_failure_then_relaunches_same_serial() -> None:
    controller = WorkOrderController.__new__(WorkOrderController)
    sm = _no_switch_sm('QAL16')
    saves: list[dict[str, Any]] = []
    launches: list[str] = []

    controller._state_machines = {'port_a': sm}
    controller._restore_normal_viz = lambda _port_id: None
    controller._capture_save_args = (
        lambda port_id, force_pass, allow_null_measurements=False: {
            'port_id': port_id,
            'force_pass': force_pass,
            'allow_null_measurements': allow_null_measurements,
        }
    )
    controller._persist_result_async = (
        lambda port_id, save_args, refresh_progress=True: saves.append(save_args)
    )
    controller._launch_test_executor = lambda port_id: launches.append(port_id)
    controller._start_pressurize_hw = lambda _port_id: None

    controller._on_retest('port_a')

    assert saves == [
        {
            'port_id': 'port_a',
            'force_pass': False,
            'allow_null_measurements': True,
        }
    ]
    assert sm.current_state == PortState.CYCLING.value
    assert sm._attempt_count == 1
    assert launches == ['port_a']


def test_no_switch_retry_still_works_after_max_out_of_spec_attempts() -> None:
    """No-switch Retry must not be dead after the normal 3-attempt retest gate."""
    controller = WorkOrderController.__new__(WorkOrderController)
    sm = _no_switch_sm('QAL16')
    sm._attempt_count = sm._max_attempts - 1
    launches: list[str] = []

    controller._state_machines = {'port_a': sm}
    controller._restore_normal_viz = lambda _port_id: None
    controller._capture_save_args = (
        lambda port_id, force_pass, allow_null_measurements=False: {
            'port_id': port_id,
            'force_pass': force_pass,
            'allow_null_measurements': allow_null_measurements,
        }
    )
    controller._persist_result_async = (
        lambda _port_id, _save_args, refresh_progress=True: None
    )
    controller._launch_test_executor = lambda port_id: launches.append(port_id)
    controller._start_pressurize_hw = lambda _port_id: None

    controller._on_retest('port_a')

    assert sm.current_state == PortState.CYCLING.value
    assert launches == ['port_a']


def test_no_switch_fail_part_persists_null_failure_and_advances_serial() -> None:
    controller = WorkOrderController.__new__(WorkOrderController)
    sm = _no_switch_sm('QAL16')
    saves: list[dict[str, Any]] = []
    advanced: list[str] = []

    controller._state_machines = {'port_a': sm}
    controller._restore_normal_viz = lambda _port_id: None
    controller._capture_save_args = (
        lambda port_id, force_pass, allow_null_measurements=False: {
            'port_id': port_id,
            'force_pass': force_pass,
            'allow_null_measurements': allow_null_measurements,
        }
    )
    controller._persist_result_async = (
        lambda port_id, save_args, refresh_progress=True: saves.append(save_args)
    )
    controller._advance_serial = lambda port_id, refresh_progress=True: advanced.append(port_id)

    controller._on_record_failure('port_a')

    assert saves == [
        {
            'port_id': 'port_a',
            'force_pass': False,
            'allow_null_measurements': True,
        }
    ]
    assert sm.current_state == PortState.IDLE.value
    assert advanced == ['port_a']


def test_derived_spst_requires_activated_transition_for_qal15_switch_detect() -> None:
    """Complementary NO/NC derivation must not latch switch_changed on first read."""
    controller = WorkOrderController.__new__(WorkOrderController)
    sm = PortStateMachine('port_b')
    sm.set_workflow_type('QAL15')
    sm.trigger('initialize_complete')
    sm.trigger('start_pressurize')

    controller._state_machines = {'port_b': sm}
    controller._switch_presence = {'port_b': False}
    controller._manual_switch_latched = {'port_b': False}
    controller._manual_switch_last_activated = {'port_b': None}
    controller._switch_transition_seen = {'port_b': False}
    controller._config = {'hardware': {'labjack': {'port_b': {}}}}
    controller._port_manager = SimpleNamespace(
        get_port=lambda _pid: SimpleNamespace(
            daq=SimpleNamespace(
                switch_no_derived_from_nc=True,
                switch_nc_derived_from_no=False,
            )
        )
    )

    activated_reading = PortReading(
        switch=SwitchState(no_active=True, nc_active=False, timestamp=0.0),
        timestamp=0.0,
    )
    deactivated_reading = PortReading(
        switch=SwitchState(no_active=False, nc_active=True, timestamp=0.0),
        timestamp=0.0,
    )

    # First pressurize samples establish baseline — no latch yet.
    controller._handle_switch_presence('port_b', activated_reading)
    assert sm.current_substate != PortSubstate.MANUAL_DETECTED.value
    assert controller._switch_presence['port_b'] is True

    sm.trigger('pressure_reached')
    # Same state after entering manual_adjust: still no detection.
    controller._handle_switch_presence('port_b', activated_reading)
    assert sm.current_substate != PortSubstate.MANUAL_DETECTED.value

    # Transition during pressurize/manual window latches detection.
    controller._handle_switch_presence('port_b', deactivated_reading)
    assert sm.current_substate == PortSubstate.MANUAL_DETECTED.value
    assert controller._manual_switch_latched['port_b'] is True
