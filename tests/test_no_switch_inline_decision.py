from __future__ import annotations

from typing import Any

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
