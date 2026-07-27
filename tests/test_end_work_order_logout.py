"""Tests for End Work Order logout vs mid-test vent race."""

from __future__ import annotations

from unittest.mock import MagicMock

from app.services.work_order_controller import WorkOrderController


def _make_controller() -> WorkOrderController:
    controller = WorkOrderController.__new__(WorkOrderController)
    controller._test_executors = {}
    controller._state_machines = {}
    controller._current_test_setup = {'part': 'SPS01438-02'}
    controller._base_viz = {}
    controller._precision_zoom_active = {'port_a': False, 'port_b': False}
    controller._current_measured_values = {
        'port_a': {'activation': None, 'deactivation': None},
        'port_b': {'activation': None, 'deactivation': None},
    }
    controller._cycle_estimates_abs_psi = {}
    controller._ui_bridge = MagicMock()
    controller._reset_precision_coordination = MagicMock()
    controller._vent_port = MagicMock()
    return controller


def test_logout_skips_sync_vent_while_executor_running() -> None:
    controller = _make_controller()
    executor = MagicMock()
    executor.is_running = True
    sm_a = MagicMock()
    sm_a.trigger.return_value = True
    sm_b = MagicMock()
    sm_b.trigger.return_value = True
    controller._test_executors['port_a'] = executor
    controller._state_machines['port_a'] = sm_a
    controller._state_machines['port_b'] = sm_b

    controller._on_logout_requested()

    executor.request_cancel.assert_called_once()
    # Idle port_b still vents; running port_a must not sync-vent on GUI thread.
    controller._vent_port.assert_called_once_with('port_b')
    sm_a.trigger.assert_any_call('end_work_order')
    controller._ui_bridge.set_work_order.assert_called_once_with({})


def test_logout_vents_idle_ports() -> None:
    controller = _make_controller()
    sm = MagicMock()
    sm.trigger.return_value = True
    controller._state_machines['port_a'] = sm
    controller._state_machines['port_b'] = sm

    controller._on_logout_requested()

    assert controller._vent_port.call_count == 2
    controller._vent_port.assert_any_call('port_a')
    controller._vent_port.assert_any_call('port_b')
