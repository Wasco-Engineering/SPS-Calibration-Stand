"""Unit tests for concurrent precision coordination in WorkOrderController."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from app.services.work_order_controller import WorkOrderController


@dataclass
class _FakePortManager:
    profiles: list[Any] = field(default_factory=list)
    removed: list[Any] = field(default_factory=list)
    cleared: int = 0

    def set_alicat_poll_profile(self, precision_port: Any) -> None:
        self.profiles.append(precision_port)

    def remove_precision_port(self, precision_port: Any) -> None:
        self.removed.append(precision_port)

    def clear_precision_ports(self) -> None:
        self.cleared += 1


@dataclass
class _FakeUiBridge:
    updates: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def update_substate(self, port_id: str, substate: str, data: dict[str, Any]) -> None:
        self.updates.append((port_id, substate, data))


class _FakeStateMachine:
    def __init__(self, can_cycles_complete: bool = True) -> None:
        self.current_state = 'cycling'
        self._can_cycles_complete = can_cycles_complete
        self.triggers: list[str] = []

    def can_trigger(self, event_name: str) -> bool:
        if event_name == 'cycles_complete':
            return self._can_cycles_complete
        return False

    def trigger(self, event_name: str) -> bool:
        self.triggers.append(event_name)
        if event_name == 'cycles_complete':
            self.current_state = 'precision_test'
        return True


class _FakeExecutor:
    def __init__(self, running: bool = True) -> None:
        self.is_running = running


def _make_controller() -> WorkOrderController:
    controller = WorkOrderController.__new__(WorkOrderController)
    controller._precision_active_ports = set()
    controller._precision_grant_events = {
        'port_a': threading.Event(),
        'port_b': threading.Event(),
    }
    controller._port_manager = _FakePortManager()
    controller._ui_bridge = _FakeUiBridge()
    controller._state_machines = {
        'port_a': _FakeStateMachine(can_cycles_complete=True),
        'port_b': _FakeStateMachine(can_cycles_complete=True),
    }
    controller._test_executors = {
        'port_a': _FakeExecutor(running=True),
        'port_b': _FakeExecutor(running=True),
    }
    return controller


def test_cycles_complete_grants_both_ports_concurrently() -> None:
    controller = _make_controller()

    controller._slot_cycles_complete('port_a')
    assert controller._precision_active_ports == {'port_a'}
    assert controller._precision_grant_events['port_a'].is_set()
    assert controller._state_machines['port_a'].triggers == ['cycles_complete']

    controller._slot_cycles_complete('port_b')
    assert controller._precision_active_ports == {'port_a', 'port_b'}
    assert controller._precision_grant_events['port_b'].is_set()
    assert controller._state_machines['port_b'].triggers == ['cycles_complete']
    assert controller._port_manager.profiles == ['port_a', 'port_b']
    assert controller._ui_bridge.updates == []


def test_cycles_complete_grants_immediately_when_sibling_not_cycling() -> None:
    controller = _make_controller()
    controller._state_machines['port_b'].current_state = 'idle'
    controller._test_executors['port_b'].is_running = False

    controller._slot_cycles_complete('port_a')

    assert controller._precision_active_ports == {'port_a'}
    assert controller._precision_grant_events['port_a'].is_set()
    assert controller._state_machines['port_a'].triggers == ['cycles_complete']


def test_release_keeps_sibling_precision_active() -> None:
    controller = _make_controller()

    controller._slot_cycles_complete('port_a')
    controller._slot_cycles_complete('port_b')

    controller._release_precision_slot('port_a', reason='completed')
    assert controller._precision_active_ports == {'port_b'}
    assert controller._port_manager.removed == ['port_a']
    assert controller._port_manager.cleared == 0
    assert controller._state_machines['port_b'].triggers == ['cycles_complete']
