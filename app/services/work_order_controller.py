"""
Work order controller for production (DB/PTP wiring).
"""
from __future__ import annotations

import logging
import math
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal, pyqtSlot

from app.database.operations import (
    ensure_work_order_master,
    validate_shop_order,
    get_work_order_progress,
    get_next_serial_number,
    get_local_queue_count,
    save_test_result,
    sync_local_cache,
)
from app.database.session import close_database, get_engine, initialize_database
from app.services import run_async
from app.core.config import save_config
from app.services.ptp_service import (
    load_ptp_from_db,
    load_ptp_from_dump,
    derive_test_setup,
    validate_ptp_params,
    build_pressure_visualization,
    convert_pressure,
    TestSetup,
)
from app.services.control_config import parse_control_config
from app.services.state.port_state_machine import PortStateMachine, PortState, PortSubstate
from app.services.sweep_utils import (
    narrow_bounds,
    ptp_limits_use_psia_scale,
    resolve_sweep_bounds,
    resolve_sweep_mode,
)
from app.services.test_executor import TestExecutor
from app.services.test_protocol import TestEvent
from app.services.admin_action_service import AdminActionService
from app.services.debug_action_service import DebugActionService
from app.services.port_runtime_state import PortRuntimeState
from app.services.ui_bridge import UIBridge
from app.hardware.port import Port, PortManager, PortId, PortReading
from app.services.measurement_source import get_measurement_settings, select_main_pressure_abs_psi
from app.services.pressure_domain import (
    infer_barometric_pressure,
    resolve_barometric_psi,
    is_gauge_unit_label,
    is_plausible_barometric_psi,
    resolve_alicat_setpoint_reference_for_test,
    resolve_display_reference,
    to_absolute_pressure,
    to_alicat_setpoint_psi,
    to_display_pressure,
)

logger = logging.getLogger(__name__)
LOW_PRESSURE_TRANSDUCER_LOCKOUT_TORR = 50.0
LOW_PRESSURE_TRANSDUCER_LOCKOUT_MESSAGE = (
    'This low-pressure part requires the transducer to be installed for this port.'
)


def _config_bool(value: Any, default: bool = False) -> bool:
    """Coerce YAML/env-style booleans without making 'false' truthy."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {'1', 'true', 'yes', 'on'}:
        return True
    if text in {'0', 'false', 'no', 'off'}:
        return False
    return default


class WorkOrderController(QObject):
    """Coordinates work order validation and PTP loading."""

    # ---- Cross-thread signals (worker thread -> main thread) ----
    _sig_pressure_reached = pyqtSignal(str)            # port_id
    _sig_trigger_error = pyqtSignal(str, str)          # port_id, message
    _sig_cycles_complete = pyqtSignal(str)             # port_id
    _sig_edges_captured = pyqtSignal(str, float, float)  # port_id, act_psi, deact_psi
    _sig_edge_detected = pyqtSignal(str, str, float)   # port_id, edge_type, pressure_psi
    _sig_cancelled = pyqtSignal(str)                   # port_id
    _sig_substate = pyqtSignal(str, str)               # port_id, substate
    _sig_hw_status_refresh = pyqtSignal()              # no args
    _sig_cycle_estimate = pyqtSignal(str, object, object, int)  # port_id, act, deact, count

    def __init__(self, ui_bridge: UIBridge, config: Dict[str, Any]) -> None:
        super().__init__()
        self._ui_bridge = ui_bridge
        self._config = config
        self._current_test_setup: Optional[TestSetup] = None
        
        # Initialize hardware
        self._port_manager = PortManager(config)
        
        self._latest_readings: Dict[str, PortReading] = {}
        self._latest_readings_lock = threading.Lock()
        self._runtime_state = PortRuntimeState.with_defaults()
        self._last_barometric_psi = self._runtime_state.last_barometric_psi
        self._barometric_warning_issued = self._runtime_state.barometric_warning_issued
        self._debug_sweep_lock = threading.Lock()
        self._debug_sweeps_in_progress: set[str] = set()
        self._debug_solenoid_mode = self._runtime_state.debug_solenoid_mode
        self._debug_alicat_mode = self._runtime_state.debug_alicat_mode
        self._debug_solenoid_last_route = self._runtime_state.debug_solenoid_last_route
        self._hw_serial_busy_ports: set[str] = set()
        self._last_worker_alicat_refresh_s: Dict[str, float] = {}
        self._worker_alicat_refresh_interval_s = 0.10
        solenoid_cfg = self._config.get("hardware", {}).get("solenoid", {})
        self._auto_vacuum_threshold_psi = float(
            solenoid_cfg.get("safe_vacuum_switch_threshold_psi", 2.0)
        )
        
        timing_cfg = config.get('timing', {}) if isinstance(config.get('timing'), dict) else {}
        ui_refresh_ms = int(timing_cfg.get('ui_refresh_interval_ms', 16))
        ui_refresh_ms = max(8, min(ui_refresh_ms, 100))

        # Poll hardware on the GUI thread — cross-thread signals with PortReading
        # payloads were not updating the UI reliably on the stand PC.
        self._readings_timer = QTimer(self)
        self._readings_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._readings_timer.timeout.connect(self._poll_live_readings)
        self._readings_timer.start(ui_refresh_ms)
        self._live_poll_log_interval_s = 5.0
        self._last_live_poll_log_s = 0.0

        # Periodic hardware status refresh for UI indicators
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_hardware_status)
        self._status_timer.start(1000)
        self._last_db_status: Optional[str] = None
        self._db_connection_status = 'Offline'
        self._db_last_write = '--'
        self._db_queue = '0'
        self._db_activity_status: Optional[str] = None
        self._db_activity_deadline = 0.0
        self._db_status_timer = QTimer(self)
        self._db_status_timer.timeout.connect(self._refresh_database_status)
        self._db_status_timer.start(5000)
        self._db_status_worker: Optional[object] = None
        self._db_sync_worker: Optional[object] = None
        self._max_test_pressure_display: Dict[str, Optional[float]] = {}
        local_cache_cfg = (
            config.get('database', {}).get('local_cache', {})
            if isinstance(config.get('database'), dict)
            else {}
        )
        sync_interval_sec = int(float(local_cache_cfg.get('sync_interval_sec', 60)))
        self._db_sync_timer = QTimer(self)
        self._db_sync_timer.timeout.connect(self._sync_local_cache)
        self._db_sync_timer.start(max(10, sync_interval_sec) * 1000)

        # State machines (one per port)
        self._state_machines: Dict[str, PortStateMachine] = {}
        self._test_executors: Dict[str, TestExecutor] = {}
        self._precision_owner_port: Optional[str] = None
        self._precision_wait_queue: List[str] = []
        self._precision_grant_events: Dict[str, threading.Event] = {}
        self._cycle_estimates_abs_psi = self._runtime_state.cycle_estimates_abs_psi
        # Track current measured values for preserving during partial updates
        self._current_measured_values = self._runtime_state.current_measured_values
        self._precision_zoom_active: Dict[str, bool] = {'port_a': False, 'port_b': False}
        self._base_viz: Optional[Dict[str, Any]] = None
        self._switch_presence = self._runtime_state.switch_presence
        self._manual_switch_latched = self._runtime_state.manual_switch_latched
        for pid in ('port_a', 'port_b'):
            sm = PortStateMachine(pid)
            sm.state_changed.connect(self._on_sm_state_changed)
            sm.substate_changed.connect(self._on_sm_substate_changed)
            sm.button_state_changed.connect(self._on_sm_button_changed)
            sm.test_result_ready.connect(self._on_sm_test_result)
            sm.error_occurred.connect(self._on_sm_error)
            self._state_machines[pid] = sm

        # Cross-thread signal connections (auto-queued for thread safety)
        self._sig_pressure_reached.connect(self._slot_pressure_reached)
        self._sig_trigger_error.connect(self._slot_trigger_error)
        self._sig_cycles_complete.connect(self._slot_cycles_complete)
        self._sig_edges_captured.connect(self._slot_edges_captured)
        self._sig_edge_detected.connect(self._slot_edge_detected)
        self._sig_cancelled.connect(self._slot_cancelled)
        self._sig_substate.connect(self._slot_substate)
        self._sig_hw_status_refresh.connect(self._refresh_hardware_status)
        self._sig_cycle_estimate.connect(self._slot_cycle_estimate)

        # Work-order / session signals
        self._ui_bridge.login_requested.connect(self._on_login_requested)
        self._ui_bridge.logout_requested.connect(self._on_logout_requested)
        self._ui_bridge.serial_increment_requested.connect(self._on_serial_increment)
        self._ui_bridge.serial_decrement_requested.connect(self._on_serial_decrement)
        self._ui_bridge.serial_manual_entry_requested.connect(self._on_serial_manual_entry)
        self._ui_bridge.barometric_pressure_updated.connect(self._on_barometric_pressure_updated)
        self._ui_bridge.debug_action_requested.connect(self._handle_debug_action)
        self._ui_bridge.admin_action_requested.connect(self._handle_admin_action)

        self._debug_actions = DebugActionService(
            port_manager=self._port_manager,
            get_pressure_unit=self._get_ui_pressure_unit,
            set_debug_alicat_mode=self._set_debug_alicat_mode,
            set_debug_solenoid_mode=self._set_debug_solenoid_mode,
            convert_display_to_absolute_psi=self._convert_debug_display_to_abs_psi,
            resolve_command_reference=self._resolve_alicat_command_reference,
            on_find_setpoint=self._start_find_setpoint,
            on_set_dio_direction=self._set_debug_dio_direction,
            on_read_dio_all=self._read_debug_dio_all,
        )
        self._admin_actions = AdminActionService(
            on_set_main_measurement_source=self._set_main_measurement_source,
            on_refresh_hardware=self._refresh_hardware_status,
            on_refresh_database=self._refresh_database_status,
            on_reconnect_hardware=self._reconnect_hardware,
            on_reconnect_database=self._reconnect_database,
            on_open_logs=self._open_logs,
            on_export_logs=self._export_logs,
            on_export_history=self._export_history,
            on_safety_override=self._safety_override,
        )

        # Test-control signals (from UI buttons)
        self._ui_bridge.start_pressurize_requested.connect(self._on_start_pressurize)
        self._ui_bridge.start_test_requested.connect(self._on_start_test)
        self._ui_bridge.cancel_requested.connect(self._on_cancel)
        self._ui_bridge.vent_requested.connect(self._on_vent)
        self._ui_bridge.record_success_requested.connect(self._on_record_success)
        self._ui_bridge.record_failure_requested.connect(self._on_record_failure)
        self._ui_bridge.retest_requested.connect(self._on_retest)
        
        # Initialize hardware and start polling (avoid blocking UI thread)
        threading.Thread(target=self._initialize_hardware, daemon=True).start()
        self._refresh_database_status()
        self._sync_local_cache()

    @staticmethod
    def _normalize_progress_counts(total: Any, completed: Any, *, context: str) -> tuple[int, int]:
        """Keep progress counts non-negative and avoid a 0-total display when rows already exist."""
        try:
            normalized_total = max(int(total), 0)
        except (TypeError, ValueError):
            normalized_total = 0

        try:
            normalized_completed = max(int(completed), 0)
        except (TypeError, ValueError):
            normalized_completed = 0

        if normalized_total == 0 and normalized_completed > 0:
            logger.warning(
                '%s: Work order quantity is missing/zero while %d results already exist; '
                'using completed count for progress display',
                context,
                normalized_completed,
            )
            normalized_total = normalized_completed
        elif normalized_total > 0 and normalized_completed > normalized_total:
            logger.warning(
                '%s: Completed count %d exceeds work order quantity %d',
                context,
                normalized_completed,
                normalized_total,
            )

        return normalized_completed, normalized_total

    @staticmethod
    def _timestamp_for_status() -> str:
        return time.strftime('%H:%M:%S')

    def _emit_database_status(self) -> None:
        status = self._db_connection_status
        if status == 'Connected' and self._db_activity_status:
            if time.monotonic() <= self._db_activity_deadline:
                status = self._db_activity_status
            else:
                self._db_activity_status = None

        self._ui_bridge.update_database_status(status, self._db_last_write, self._db_queue)

    def _set_database_connection_status(self, status: str) -> None:
        if self._last_db_status != status:
            self._last_db_status = status
        self._db_connection_status = status
        if status != 'Connected':
            self._db_activity_status = None
        self._emit_database_status()

    def _set_database_activity_status(
        self,
        status: str,
        *,
        last_write: Optional[str] = None,
        queue: Optional[str] = None,
        hold_seconds: float = 10.0,
    ) -> None:
        if last_write is not None:
            self._db_last_write = last_write
        if queue is not None:
            self._db_queue = queue
        self._db_activity_status = status
        self._db_activity_deadline = time.monotonic() + max(hold_seconds, 0.0)
        self._emit_database_status()

    def _on_login_requested(self, payload: Dict[str, Any]) -> None:
        login_start = time.perf_counter()
        operator_id = str(payload.get("OperatorID", "")).strip()
        shop_order = str(payload.get("ShopOrder", "")).strip()
        part_id = str(payload.get("PartID", "")).strip()
        sequence_id = str(payload.get("SequenceID", "")).strip()
        total = int(payload.get("OrderQTY") or payload.get("OrderQty") or 0)
        test_mode = bool(payload.get("TestMode"))
        wo_validated = bool(payload.get("WOValidated"))

        # In test mode, use defaults if fields are empty
        if test_mode:
            if not operator_id:
                operator_id = "TEST-OP"
            if not shop_order:
                shop_order = "TEST-ORDER"
            if not part_id:
                part_id = "TEST-MODE"
            if not sequence_id:
                sequence_id = "1"
            if total == 0:
                total = 3

        manual_entry = bool(payload.get("ManualEntry"))

        if test_mode:
            completed = 0
            passed = 0
            failed = 0
        elif manual_entry:
            # Manual entry — skip WO re-validation, still load real PTP
            logger.info(
                "Manual entry mode: skipping WO validation for '%s' "
                "(Part=%s, Seq=%s)",
                shop_order, part_id, sequence_id,
            )
            progress = get_work_order_progress(shop_order, part_id, sequence_id)
            completed = progress.get("completed", 0)
            passed = progress.get("passed", 0)
            failed = progress.get("failed", 0)
        else:
            if not (wo_validated and part_id and sequence_id):
                details = validate_shop_order(shop_order)
                if not details:
                    self._ui_bridge.show_error_message(
                        "Work Order",
                        f"Shop Order '{shop_order}' not found or DB unavailable.",
                    )
                    return

                part_id = part_id or str(details.get("PartID", "")).strip()
                sequence_id = sequence_id or str(details.get("SequenceID", "")).strip()
                total = details.get("OrderQTY") or details.get("OrderQty") or total

            progress = get_work_order_progress(shop_order, part_id, sequence_id)
            completed = progress.get("completed", 0)
            passed = progress.get("passed", 0)
            failed = progress.get("failed", 0)

        completed, total = self._normalize_progress_counts(
            total,
            completed,
            context=f'Login progress for {shop_order or "unknown work order"}',
        )

        workflow_type = _workflow_for_sequence(sequence_id)

        work_order = {
            "operator_id": operator_id,
            "shop_order": shop_order,
            "part_id": part_id,
            "sequence_id": sequence_id,
            "process_id": sequence_id,
            "workflow_type": workflow_type,
            "total": total,
            "completed": completed,
            "test_mode": test_mode,
        }

        self._ui_bridge.set_work_order(work_order)
        self._ui_bridge.update_progress(completed, total, passed, failed)

        ptp_start = time.perf_counter()
        self._load_ptp(part_id, sequence_id, test_mode=test_mode)
        logger.info(
            'Login setup: PTP load completed in %.3fs (part=%s seq=%s)',
            time.perf_counter() - ptp_start,
            part_id,
            sequence_id,
        )
        if not test_mode and self._current_test_setup is None:
            logger.error(
                'Login setup blocked: no valid PTP loaded for %s/%s',
                part_id,
                sequence_id,
            )
            self._ui_bridge.set_work_order({})
            self._ui_bridge.update_progress(0, 0, 0, 0)
            return
        config_for_master = self.__dict__.get('_config')
        if not test_mode and self._current_test_setup is not None and isinstance(config_for_master, dict):
            test_params = config_for_master.get('test_parameters', {})
            equipment_id = test_params.get('equipment_id', 'STINGER_01')
            activation_target = (
                self._current_test_setup.activation_target
                if self._current_test_setup is not None
                else None
            )
            ensure_work_order_master(
                shop_order=shop_order,
                part_id=part_id,
                sequence_id=sequence_id,
                order_qty=total,
                activation_target=activation_target,
                operator_id=operator_id,
                equipment_id=equipment_id,
            )
        
        # Configure ports from PTP if available. PTP owns logical NO/NC/COM;
        # stand config only describes which DB9 pins are physically sensed.
        switch_config_errors: List[str] = []
        if self._current_test_setup:
            ptp_params = self._current_test_setup.raw
            for port_id in [PortId.PORT_A, PortId.PORT_B]:
                port = self._port_manager.get_port(port_id)
                if port:
                    if not port.configure_from_ptp(ptp_params):
                        resolution = getattr(port, 'last_switch_resolution', None)
                        details = (
                            '; '.join(getattr(resolution, 'errors', ()) or ())
                            if resolution is not None
                            else 'unknown PTP switch resolution error'
                        )
                        switch_config_errors.append(f'{port_id.value}: {details}')

        # Initialize state machines with workflow type and transition to IDLE
        for pid, sm in self._state_machines.items():
            sm.set_workflow_type(workflow_type)
            sm.reset_for_new_unit()
            # Transition from END -> INIT -> IDLE, or INIT -> IDLE
            if sm.current_state == PortState.END.value:
                # First transition from END to INIT
                sm.trigger('logout_complete')
            if switch_config_errors:
                sm.trigger(
                    'error',
                    message='PTP switch configuration failed. Testing is blocked.',
                )
                continue
            # Now transition from INIT to IDLE
            if sm.current_state == PortState.INIT.value:
                sm.trigger('initialize_complete')

        if switch_config_errors:
            message = 'PTP switch configuration failed:\n- ' + '\n- '.join(switch_config_errors)
            logger.error(message)
            self._ui_bridge.show_error_message('PTP Switch Configuration', message)
            return
        
        if test_mode:
            self._allocate_test_serials()
        else:
            self._allocate_initial_serials(shop_order, part_id, sequence_id)

        logger.info(
            'Login setup complete in %.3fs (operator=%s shop_order=%s workflow=%s)',
            time.perf_counter() - login_start,
            operator_id,
            shop_order,
            workflow_type,
        )

    def _initialize_hardware(self) -> None:
        """Initialize hardware ports and start polling."""
        init_start = time.perf_counter()
        ports_start = time.perf_counter()
        if not self._port_manager.initialize_ports():
            logger.error("Failed to initialize hardware ports")
            return
        logger.info('Hardware init: port objects initialized in %.3fs', time.perf_counter() - ports_start)
        
        connect_start = time.perf_counter()
        if not self._port_manager.connect_all():
            logger.warning("Some hardware connections failed, continuing anyway")
        logger.info('Hardware init: connect_all completed in %.3fs', time.perf_counter() - connect_start)
        self._sig_hw_status_refresh.emit()
        
        # Start polling loop
        poll_start = time.perf_counter()
        self._port_manager.start_polling()
        self._port_manager.set_alicat_poll_profile(None)
        logger.info(
            'Hardware polling started in %.3fs (total hardware init %.3fs)',
            time.perf_counter() - poll_start,
            time.perf_counter() - init_start,
        )

    def _request_precision_slot(self, port_id: str) -> bool:
        """Try to acquire precision-sweep exclusivity for a port."""
        if self._precision_owner_port == port_id:
            return True
        if (
            self._precision_owner_port is None
            and not self._precision_wait_queue
            and not self._has_sibling_still_cycling(port_id)
        ):
            self._precision_owner_port = port_id
            self._port_manager.set_alicat_poll_profile(port_id)
            self._signal_precision_grant(port_id)
            logger.info('%s: Precision slot granted immediately', port_id)
            return True
        if port_id not in self._precision_wait_queue:
            self._precision_wait_queue.append(port_id)
            logger.info(
                '%s: Waiting for precision slot (owner=%s queue=%s)',
                port_id,
                self._precision_owner_port,
                self._precision_wait_queue,
            )
            if self._ui_bridge:
                self._ui_bridge.update_substate(port_id, 'cycling.waiting_precision_slot', {})
        self._promote_waiting_precision_port()
        return False

    def _has_sibling_still_cycling(self, port_id: str) -> bool:
        """True when another port still needs full-speed cycling reads."""
        for sibling_id, sm in self._state_machines.items():
            if sibling_id == port_id:
                continue
            if sibling_id in self._precision_wait_queue:
                continue
            if self._precision_owner_port == sibling_id:
                continue
            if getattr(sm, 'current_state', None) != PortState.CYCLING.value:
                continue
            executor = self._test_executors.get(sibling_id)
            if executor is None or executor.is_running:
                return True
        return False

    def _remove_precision_waiter(self, port_id: str) -> None:
        if port_id not in self._precision_wait_queue:
            return
        self._precision_wait_queue = [pid for pid in self._precision_wait_queue if pid != port_id]
        logger.info('%s: Removed from precision wait queue', port_id)
        self._signal_precision_grant(port_id)

    def _release_precision_slot(self, port_id: str, reason: str) -> None:
        """Release precision ownership for a port and restore normal polling."""
        self._remove_precision_waiter(port_id)
        if self._precision_owner_port != port_id:
            return
        self._precision_owner_port = None
        self._port_manager.set_alicat_poll_profile(None)
        logger.info('%s: Precision slot released (%s)', port_id, reason)
        self._promote_waiting_precision_port()

    def _promote_waiting_precision_port(self) -> None:
        """Grant precision to next waiting port (first-ready-first-served)."""
        if self._precision_owner_port is not None:
            return
        while self._precision_wait_queue:
            next_port = self._precision_wait_queue.pop(0)
            sm = self._state_machines.get(next_port)
            if not sm:
                continue
            if not sm.can_trigger('cycles_complete'):
                logger.info(
                    '%s: Skipping queued precision promotion (state=%s)',
                    next_port,
                    sm.current_state,
                )
                continue
            if self._has_sibling_still_cycling(next_port):
                self._precision_wait_queue.insert(0, next_port)
                logger.info(
                    '%s: Holding queued precision promotion until sibling cycling completes',
                    next_port,
                )
                return
            self._precision_owner_port = next_port
            self._port_manager.set_alicat_poll_profile(next_port)
            self._signal_precision_grant(next_port)
            logger.info('%s: Precision slot granted from queue', next_port)
            sm.trigger('cycles_complete')
            return

    def _signal_precision_grant(self, port_id: str) -> None:
        event = self._precision_grant_events.get(port_id)
        if event:
            event.set()

    def _reset_precision_coordination(self) -> None:
        """Clear precision owner/queue state and return normal polling profile."""
        self._precision_owner_port = None
        self._precision_wait_queue.clear()
        for event in self._precision_grant_events.values():
            event.set()
        self._precision_grant_events.clear()
        self._port_manager.set_alicat_poll_profile(None)
    
    def _hardware_poll_labjack_only(self) -> bool:
        """True when Alicat serial is owned by a worker — keep GUI polls LabJack-only."""
        if self._hw_serial_busy_ports:
            return True
        return any(
            executor.is_running
            for executor in self._test_executors.values()
        )

    def _poll_live_readings(self) -> None:
        """Read transducer + Alicat on the GUI thread and push to the UI."""
        if not self._port_manager.is_hardware_ready:
            return
        readings = self._port_manager.poll_once(
            labjack_only=self._hardware_poll_labjack_only(),
        )
        if not readings:
            return
        self._apply_poll_readings(readings)

        now = time.perf_counter()
        if now - self._last_live_poll_log_s >= self._live_poll_log_interval_s:
            self._last_live_poll_log_s = now
            for port_id, reading in readings.items():
                port_key = port_id.value if isinstance(port_id, PortId) else str(port_id)
                transducer_psi = (
                    reading.transducer.pressure if reading.transducer is not None else None
                )
                alicat_psi = reading.alicat.pressure if reading.alicat is not None else None
                logger.debug(
                    'Live poll %s: transducer=%s alicat=%s',
                    port_key,
                    f'{transducer_psi:.3f}' if transducer_psi is not None else '--',
                    f'{alicat_psi:.3f}' if alicat_psi is not None else '--',
                )

    def _apply_poll_readings(self, readings: Dict[PortId, PortReading]) -> None:
        """Apply one poll snapshot (transducer + cached Alicat)."""
        for port_id, reading in readings.items():
            port_id_str = port_id.value if isinstance(port_id, PortId) else str(port_id)
            with self._latest_readings_lock:
                self._latest_readings[port_id_str] = reading
            self._apply_debug_solenoid_auto(port_id_str, reading)
            self._observe_max_pressure_for_database(port_id_str, reading)
            self._handle_switch_presence(port_id_str, reading)
            self._ui_bridge.update_pressure(port_id_str, reading)
            if reading.dio is not None:
                self._ui_bridge.update_debug_dio(port_id_str, reading.dio)

    def _observe_max_pressure_for_database(self, port_id: str, reading: PortReading) -> None:
        """Track the maximum display/test-reference pressure for legacy report fields."""
        setup = self._current_test_setup
        if not setup:
            return
        executor = self._test_executors.get(port_id)
        sm = self._state_machines.get(port_id)
        active_states = {
            PortState.PRESSURIZING.value,
            PortState.CYCLING.value,
            PortState.PRECISION_TEST.value,
        }
        if not (executor and executor.is_running) and not (
            sm and sm.current_state in active_states
        ):
            return
        try:
            pressure_abs_psi, _source = select_main_pressure_abs_psi(
                reading,
                get_measurement_settings(self._config),
                barometric_psi=self._get_barometric_pressure(port_id),
            )
        except Exception:
            logger.debug('%s: Unable to observe max pressure for DB', port_id, exc_info=True)
            return
        if pressure_abs_psi is None or not math.isfinite(pressure_abs_psi):
            return

        unit_label = self._ui_bridge.get_pressure_unit() if self._ui_bridge else setup.units_label or 'PSI'
        pressure_display = self._to_display_pressure(
            port_id,
            float(pressure_abs_psi),
            unit_label,
            setup.pressure_reference,
        )
        current = self._max_test_pressure_display.get(port_id)
        if current is None or pressure_display > current:
            self._max_test_pressure_display[port_id] = pressure_display

    def _has_switch_presence(self, port_id: str) -> bool:
        return bool(self._switch_presence.get(port_id, False))

    def _handle_switch_presence(self, port_id: str, reading: PortReading) -> None:
        switch = getattr(reading, 'switch', None)
        connected = bool(switch and (switch.no_active or switch.nc_active))
        self._switch_presence[port_id] = connected

        sm = self._state_machines.get(port_id)
        if not sm:
            return

        if sm.current_state != PortState.MANUAL_ADJUST.value:
            self._manual_switch_latched[port_id] = False
            return

        if connected and not self._manual_switch_latched.get(port_id, False):
            if sm.can_trigger('switch_changed'):
                sm.trigger('switch_changed')
            self._manual_switch_latched[port_id] = True
        elif not connected:
            self._manual_switch_latched[port_id] = False

    def _refresh_hardware_status(self) -> None:
        """Emit hardware status to UI for status indicators."""
        try:
            status = self._port_manager.get_all_status()
        except Exception as exc:
            logger.warning("Failed to read hardware status: %s", exc)
            status = {}
        status['precision_owner'] = self._precision_owner_port or 'none'
        status['precision_queue'] = list(self._precision_wait_queue)
        status['alicat_poll_divisors'] = self._port_manager.get_alicat_poll_divisors()
        status['precision_poll'] = self._port_manager.get_precision_poll_status()
        self._ui_bridge.update_hardware_status(status)

    def _refresh_database_status(self) -> None:
        """Emit database connectivity status to the UI."""
        if self._db_status_worker is not None:
            return

        def _check() -> tuple[str, str, str, Optional[Exception]]:
            queue = str(get_local_queue_count())
            engine = get_engine()
            if engine is None:
                return "Offline", "--", queue, None
            try:
                with engine.connect() as conn:
                    conn.exec_driver_sql("SELECT 1")
                return "Connected", "--", queue, None
            except Exception as exc:
                return "Disconnected", "--", queue, exc

        def _on_done(result: Any, error: Optional[Exception]) -> None:
            self._db_status_worker = None
            if error is not None:
                status, last_write, queue, exc = "Disconnected", "--", "0", error
            else:
                status, last_write, queue, exc = result
            if exc and self._last_db_status != status:
                logger.warning("Database connectivity check failed: %s", exc)
            if last_write != '--':
                self._db_last_write = last_write
            self._db_queue = queue
            self._set_database_connection_status(status)

        self._db_status_worker = run_async(_check, _on_done)

    def _sync_local_cache(self) -> None:
        """Try to upload queued local rows without blocking the UI."""
        if self._db_sync_worker is not None:
            return

        def _sync() -> Dict[str, int]:
            return sync_local_cache()

        def _on_done(result: Any, error: Optional[Exception]) -> None:
            self._db_sync_worker = None
            if error is not None:
                logger.warning('Local DB sync failed: %s', error)
                self._set_database_activity_status(
                    'Sync Failed',
                    queue=str(get_local_queue_count()),
                    hold_seconds=10.0,
                )
                return
            sync_result = result if isinstance(result, dict) else {}
            queue = str(get_local_queue_count())
            status = 'Synced' if sync_result.get('synced', 0) else self._db_connection_status
            if sync_result.get('conflicts', 0):
                status = 'Sync Conflict'
            elif sync_result.get('errors', 0):
                status = 'Sync Failed'
            if status not in {'Connected', 'Offline', 'Disconnected'}:
                self._set_database_activity_status(
                    status,
                    last_write=self._timestamp_for_status() if sync_result.get('synced', 0) else None,
                    queue=queue,
                    hold_seconds=10.0,
                )
            else:
                self._db_queue = queue
                self._emit_database_status()

        self._db_sync_worker = run_async(_sync, _on_done)
    
    def _on_logout_requested(self) -> None:
        """Handle logout/end work order - reset all ports and clear work order data."""
        self._reset_precision_coordination()
        # Cancel any running test executors and reset ports
        for port_id in ('port_a', 'port_b'):
            # Cancel any running executor first
            executor = self._test_executors.get(port_id)
            if executor and executor.is_running:
                executor.request_cancel()
            
            # Vent the port hardware
            self._vent_port(port_id)
            
            # Reset state machine to END state
            sm = self._state_machines.get(port_id)
            if sm:
                # Trigger end_work_order which will vent and transition to END state
                sm.trigger('end_work_order')
        
        # Clear work order data
        self._current_test_setup = None
        self._base_viz = None
        for port_id in ('port_a', 'port_b'):
            self._precision_zoom_active[port_id] = False
            if port_id in self._current_measured_values:
                self._current_measured_values[port_id]['activation'] = None
                self._current_measured_values[port_id]['deactivation'] = None
            self._cycle_estimates_abs_psi.pop(port_id, None)
            self._ui_bridge.update_pressure_viz(port_id, {})
        self._ui_bridge.set_work_order({})
        self._ui_bridge.update_progress(0, 0, 0, 0)
        self._ui_bridge.update_ptp_details({})
        
        # Release serial numbers
        self._ui_bridge.release_serial('port_a')
        self._ui_bridge.release_serial('port_b')

    def _load_ptp(self, part_id: str, sequence_id: str, test_mode: bool = False) -> None:
        params = {}
        source = "unknown"
        if not test_mode:
            params = load_ptp_from_db(part_id, sequence_id)
            if params:
                source = "database"
        if not params:
            params = load_ptp_from_dump(part_id, sequence_id)
            if params:
                source = "dump"

        # In test mode, use default PTP if none found
        if not params and test_mode:
            params = _get_default_test_ptp()
            source = "default"
            logger.info("Using default test mode PTP parameters")

        if not params:
            if not test_mode:
                self._ui_bridge.show_error_message(
                    "PTP",
                    f"No PTP parameters found for {part_id}/{sequence_id}.",
                )
            self._current_test_setup = None
            self._ui_bridge.update_ptp_details({})
            return

        is_valid, errors = validate_ptp_params(params)
        if not is_valid:
            if not test_mode:
                message = "PTP validation failed:\n- " + "\n- ".join(errors)
                self._ui_bridge.show_error_message("PTP Validation", message)
            else:
                logger.warning("PTP validation failed in test mode: %s", errors)
            self._current_test_setup = None
            self._ui_bridge.update_ptp_details({})
            return

        self._current_test_setup = derive_test_setup(part_id, sequence_id, params)
        logger.info(
            "Loaded PTP for %s/%s (units=%s, direction=%s, reference=%s)",
            part_id,
            sequence_id,
            self._current_test_setup.units_label,
            self._current_test_setup.activation_direction,
            self._current_test_setup.pressure_reference,
        )
        self._ui_bridge.update_ptp_details(
            {
                "part_id": part_id,
                "sequence_id": sequence_id,
                "source": source,
                "units_label": self._current_test_setup.units_label,
                "pressure_reference": self._current_test_setup.pressure_reference,
                "params": self._current_test_setup.raw,
            }
        )
        self._apply_ptp_to_ui()
        self._configure_alicat_units_async()

    def _allocate_initial_serials(self, shop_order: str, part_id: str, sequence_id: str) -> None:
        in_progress = self._ui_bridge.get_in_progress_serials()

        serial_a = get_next_serial_number(
            shop_order, part_id, sequence_id, in_progress_serials=in_progress, start_from=1
        )
        self._ui_bridge.allocate_serial("port_a", serial_a)
        in_progress.add(serial_a)

        serial_b = get_next_serial_number(
            shop_order, part_id, sequence_id, in_progress_serials=in_progress, start_from=2
        )
        self._ui_bridge.allocate_serial("port_b", serial_b)

    def _allocate_test_serials(self) -> None:
        self._ui_bridge.allocate_serial("port_a", 1)
        self._ui_bridge.allocate_serial("port_b", 2)

    def _apply_ptp_to_ui(
        self,
        atmosphere_override: Optional[float] = None,
        preserve_unit: bool = False,
    ) -> None:
        """Apply PTP to UI, optionally overriding atmosphere with barometric reading."""
        if not self._current_test_setup:
            return
        if preserve_unit:
            display_units = self._ui_bridge.get_pressure_unit()
        else:
            setup = self._current_test_setup
            units_label = setup.units_label or "PSI"
            pressure_ref = (setup.pressure_reference or "").strip().lower()
            baro = atmosphere_override if atmosphere_override is not None else 14.7
            _min_psi, _max_psi = resolve_sweep_bounds(setup, {})
            if ptp_limits_use_psia_scale(setup, {}, baro):
                pressure_ref = "absolute"
                if units_label.upper() == "PSI":
                    units_label = "PSIA"
            elif units_label.upper() == "PSI" and pressure_ref == "gauge":
                units_label = "PSIG"
            elif units_label.upper() == "PSI" and pressure_ref == "absolute":
                units_label = "PSIA"
            self._ui_bridge.set_pressure_unit(units_label)
            self._ui_bridge.set_display_reference(pressure_ref or None)
            display_units = self._ui_bridge.get_pressure_unit()

        viz = build_pressure_visualization(
            self._current_test_setup,
            self._config.get("ui", {}),
            atmosphere_override=atmosphere_override,
            display_units_override=display_units,
        )
        self._base_viz = viz
        for port_id in ("port_a", "port_b"):
            if self._precision_zoom_active.get(port_id):
                continue
            self._cycle_estimates_abs_psi[port_id] = {
                'activation': None,
                'deactivation': None,
                'count': 0,
            }
            self._ui_bridge.update_pressure_viz(port_id, viz)

    def _configure_alicat_units_async(self) -> None:
        if not self._current_test_setup:
            return
        units_code = self._current_test_setup.units_code
        if not units_code:
            return
        def _configure() -> None:
            for port_id in [PortId.PORT_A, PortId.PORT_B]:
                port = self._port_manager.get_port(port_id)
                if port:
                    ok = port.alicat.configure_units_from_ptp(units_code)
                    if not ok:
                        logger.warning(
                            'Alicat units verify failed during login for %s (requested code=%s)',
                            port_id.value,
                            units_code,
                        )

        threading.Thread(target=_configure, daemon=True).start()
    
    def update_atmosphere_from_barometric(self, barometric_pressure: float) -> None:
        """Update pressure visualization with barometric pressure from Alicat."""
        if self._current_test_setup:
            self._apply_ptp_to_ui(
                atmosphere_override=barometric_pressure,
                preserve_unit=True,
            )

    def _on_serial_increment(self, port_id: str) -> None:
        self._bump_serial(port_id, delta=1)

    def _on_serial_decrement(self, port_id: str) -> None:
        self._bump_serial(port_id, delta=-1)

    def _on_serial_manual_entry(self, port_id: str, serial: int) -> None:
        if serial < 1:
            return
        self._ui_bridge.allocate_serial(port_id, serial)

    def _bump_serial(self, port_id: str, delta: int) -> None:
        try:
            current = self._ui_bridge._port_serials.get(port_id, 1)
            serial = self._next_available_serial_for_port(port_id, current + delta, delta)
            self._ui_bridge.allocate_serial(port_id, serial)
        except Exception:
            self._ui_bridge.allocate_serial(port_id, max(1, 1 + delta))

    def _next_available_serial_for_port(self, port_id: str, candidate: int, step: int = 1) -> int:
        """Return the next positive serial not currently assigned to the other port."""
        serial = max(1, int(candidate))
        direction = 1 if step >= 0 else -1
        other_serials = {
            serial_value
            for other_port, serial_value in self._ui_bridge._port_serials.items()
            if other_port != port_id
        }
        while serial in other_serials:
            serial = max(1, serial + direction)
            if serial == 1 and direction < 0:
                break
        return serial

    def get_current_test_setup(self) -> Optional[TestSetup]:
        return self._current_test_setup
    
    def _on_barometric_pressure_updated(self, port_id: str, barometric_pressure: float) -> None:
        """Handle barometric pressure update from Alicat - update atmosphere reference."""
        self._last_barometric_psi[port_id] = barometric_pressure
        # Use barometric pressure from any port (they should be similar)
        # Update visualization for all ports with the barometric reading
        self.update_atmosphere_from_barometric(barometric_pressure)

    def _get_latest_reading(self, port_id: str) -> Optional[PortReading]:
        """Return a reading for test/edge logic.

        While a test runs, read LabJack sensors on the caller thread instead of
        the GUI poll cache. Cached snapshots can carry a stale Alicat value when
        polls are LabJack-only, which makes ramps appear finished instantly.
        """
        executor = self._test_executors.get(port_id)
        if executor and executor.is_running:
            port = self._port_manager.get_port(port_id)
            if port is not None:
                try:
                    self._refresh_alicat_for_worker_if_due(port_id, port)
                    return port.read_precision_fast()
                except Exception as exc:
                    logger.warning(
                        '%s: Live precision read failed, using cached poll: %s',
                        port_id,
                        exc,
                    )
        with self._latest_readings_lock:
            return self._latest_readings.get(port_id)

    def _refresh_alicat_for_worker_if_due(self, port_id: str, port: Any) -> None:
        """Refresh cached Alicat pressure from the worker when tests use Alicat as source."""
        measurement_cfg = self._config.get('hardware', {}).get('measurement', {})
        preferred_source = (
            str(measurement_cfg.get('preferred_source', 'auto')).strip().lower()
            if isinstance(measurement_cfg, dict)
            else 'auto'
        )
        port_cfg = self._config.get('hardware', {}).get('labjack', {}).get(port_id, {})
        transducer_installed = bool(port_cfg.get('transducer_installed', True))
        if preferred_source != 'alicat' and transducer_installed:
            return

        now = time.perf_counter()
        last = self._last_worker_alicat_refresh_s.get(port_id, 0.0)
        if now - last < self._worker_alicat_refresh_interval_s:
            return
        self._last_worker_alicat_refresh_s[port_id] = now
        port.refresh_alicat()

    def _start_find_setpoint(self, port_id: str, payload: Dict[str, Any]) -> None:
        with self._debug_sweep_lock:
            if port_id in self._debug_sweeps_in_progress:
                self._ui_bridge.show_info_message(
                    "Find Setpoint",
                    f"{port_id.upper().replace('_', ' ')} already running a sweep.",
                )
                return
            self._debug_sweeps_in_progress.add(port_id)

        threading.Thread(
            target=self._run_find_setpoint,
            args=(port_id, payload),
            daemon=True,
        ).start()

    def _run_find_setpoint(self, port_id: str, payload: Dict[str, Any]) -> None:
        port = None
        try:
            port = self._port_manager.get_port(port_id)
            if not port:
                self._ui_bridge.show_error_message(
                    "Find Setpoint",
                    f"Port {port_id} not found.",
                )
                return

            mode_override = str(payload.get("mode") or "auto").strip().lower()
            sweep_mode = self._resolve_sweep_mode(port_id, mode_override)
            bounds = self._resolve_sweep_bounds(port_id)
            if bounds is None:
                self._ui_bridge.show_error_message(
                    "Find Setpoint",
                    f"No sweep bounds available for {port_id}.",
                )
                return

            min_psi, max_psi = bounds
            atmosphere_psi = self._determine_atmosphere_psi(
                port_id,
                self._current_test_setup.pressure_reference if self._current_test_setup else None,
            )
            if sweep_mode == "pressure":
                min_psi = min(min_psi, atmosphere_psi)
            else:
                max_psi = max(max_psi, atmosphere_psi)
            if min_psi >= max_psi:
                self._ui_bridge.show_error_message(
                    "Find Setpoint",
                    f"Invalid sweep range for {port_id}: {min_psi:.3f} to {max_psi:.3f} PSI.",
                )
                return

            slow_rate, medium_rate, fast_rate = self._resolve_sweep_rates()
            executor = self._create_debug_sweep_executor(port_id, port)

            self._set_active_test_route(port, port_id, sweep_mode, "find setpoint")
            port.alicat.cancel_hold()

            direction = 1 if sweep_mode == "pressure" else -1
            result = None
            pass_bounds = (min_psi, max_psi)
            pass_plan = self._build_find_setpoint_pass_plan(slow_rate, medium_rate, fast_rate)
            for rate_psi_per_sec, narrowing in pass_plan:
                if narrowing is not None:
                    if result is None:
                        break
                    factor, min_pad = narrowing
                    pass_bounds = narrow_bounds(
                        result[0],
                        result[1],
                        min_psi,
                        max_psi,
                        factor=factor,
                        min_pad=min_pad,
                    )
                result = executor.run_debug_sweep_pass(pass_bounds, direction, rate_psi_per_sec)
                if result is None:
                    break

            if not result:
                self._ui_bridge.show_error_message(
                    "Find Setpoint",
                    f"No edges detected for {port_id}.",
                )
                return

            activation_psi, deactivation_psi = result
            unit_label = self._ui_bridge.get_pressure_unit() if self._ui_bridge else "PSI"
            activation_display = self._to_display_pressure(
                port_id,
                activation_psi,
                unit_label,
                self._current_test_setup.pressure_reference if self._current_test_setup else None,
            )
            deactivation_display = self._to_display_pressure(
                port_id,
                deactivation_psi,
                unit_label,
                self._current_test_setup.pressure_reference if self._current_test_setup else None,
            )

            self._ui_bridge.update_pressure_viz(
                port_id,
                {
                    "measured_activation": activation_display,
                    "measured_deactivation": deactivation_display,
                },
            )
            self._ui_bridge.show_info_message(
                "Find Setpoint",
                "\n".join([
                    f"{port_id.upper().replace('_', ' ')} sweep complete.",
                    f"Activation: {activation_display:.3f} {unit_label}",
                    f"Deactivation: {deactivation_display:.3f} {unit_label}",
                ]),
            )

        except Exception as exc:
            logger.error("Find setpoint failed for %s: %s", port_id, exc, exc_info=True)
            self._ui_bridge.show_error_message("Find Setpoint", str(exc))
        finally:
            if port is not None:
                try:
                    port.alicat.hold_valve()
                except Exception:
                    pass
            with self._debug_sweep_lock:
                self._debug_sweeps_in_progress.discard(port_id)

    @staticmethod
    def _build_find_setpoint_pass_plan(
        slow_rate: float,
        medium_rate: float,
        fast_rate: float,
    ) -> list[tuple[float, Optional[tuple[float, float]]]]:
        return [
            (fast_rate, None),
            (medium_rate, (0.5, 0.5)),
            (slow_rate, (0.2, 0.2)),
        ]

    def _resolve_sweep_mode(self, port_id: str, override: str) -> str:
        override = (override or "auto").strip().lower()
        if override in {"pressure", "vacuum"}:
            return override

        setup = self._current_test_setup
        # Vacuum vs pressure uses barometric absolute PSI, not gauge-zero atmosphere.
        return resolve_sweep_mode(setup, atmosphere_psi=self._get_barometric_pressure(port_id))

    def _determine_atmosphere_psi(self, port_id: str, pressure_reference: Optional[str]) -> float:
        setup = self._current_test_setup
        baro = self._get_barometric_pressure(port_id)
        if setup and ptp_limits_use_psia_scale(setup, {}, baro):
            return baro
        if str(pressure_reference or "").strip().lower() == "absolute":
            return baro
        return 0.0

    def _get_barometric_pressure(self, port_id: str) -> float:
        reading = self._get_latest_reading(port_id)
        inferred = infer_barometric_pressure(reading)
        last_value = self._last_barometric_psi.get(port_id)
        if not is_plausible_barometric_psi(last_value):
            last_value = next(
                (
                    value
                    for other_port, value in self._last_barometric_psi.items()
                    if other_port != port_id and is_plausible_barometric_psi(value)
                ),
                None,
            )
        if inferred is not None and not is_plausible_barometric_psi(inferred):
            if not self._barometric_warning_issued.get(port_id, False):
                logger.warning(
                    '%s: Ignoring implausible barometric inference %.4f PSI; using %.4f PSI',
                    port_id,
                    inferred,
                    resolve_barometric_psi(
                        reading,
                        last_value=last_value,
                    ),
                )
                self._barometric_warning_issued[port_id] = True
        elif (
            inferred is not None
            and is_plausible_barometric_psi(last_value)
            and abs(float(inferred) - float(last_value)) > 1.0
        ):
            logger.warning(
                '%s: Ignoring barometric jump %.4f -> %.4f PSI; using last good value',
                port_id,
                last_value,
                inferred,
            )

        baro = resolve_barometric_psi(
            reading,
            last_value=last_value,
        )
        self._last_barometric_psi[port_id] = baro
        return baro

    def _infer_barometric_pressure_from_reading(self, reading: Optional[PortReading]) -> Optional[float]:
        return infer_barometric_pressure(reading)

    def _is_gauge_unit_label(self, unit_label: Optional[str]) -> bool:
        return is_gauge_unit_label(unit_label)

    def _resolve_display_reference(self, unit_label: Optional[str]) -> str:
        setup = self._current_test_setup
        setup_reference = setup.pressure_reference if setup else None
        return resolve_display_reference(unit_label, setup_reference)

    def _infer_alicat_setpoint_reference(self, port_id: str, barometric_psi: float) -> str:
        setup = self._current_test_setup
        ptp_ref = setup.pressure_reference if setup else None
        alicat_cfg = self._config.get('hardware', {}).get('alicat', {})
        port_cfg = alicat_cfg.get(port_id, {})
        config_ref = port_cfg.get('setpoint_reference')
        return resolve_alicat_setpoint_reference_for_test(
            ptp_pressure_reference=ptp_ref,
            ptp_units_label=setup.units_label if setup else None,
            config_reference=str(config_ref) if config_ref is not None else None,
            reading=self._get_latest_reading(port_id),
            barometric_psi=barometric_psi,
        )

    def _to_absolute_pressure(self, port_id: str, value_psi: float, pressure_reference: Optional[str]) -> float:
        baro = self._get_barometric_pressure(port_id)
        setup = self._current_test_setup
        if setup and ptp_limits_use_psia_scale(setup, {}, baro):
            return float(value_psi)
        return to_absolute_pressure(value_psi, pressure_reference, baro)

    def _to_display_pressure(
        self,
        port_id: str,
        value_psi: float,
        unit_label: Optional[str],
        pressure_reference: Optional[str] = None,
    ) -> float:
        baro = self._get_barometric_pressure(port_id)
        value_abs = value_psi
        effective_reference = pressure_reference
        if str(pressure_reference or '').strip().lower() == 'gauge':
            setup = self._current_test_setup
            if setup and ptp_limits_use_psia_scale(setup, {}, baro):
                value_abs = float(value_psi)
                effective_reference = 'absolute'
            else:
                value_abs = to_absolute_pressure(
                    value_psi,
                    pressure_reference='gauge',
                    barometric_psi=baro,
                )
        converted = to_display_pressure(
            value_abs_psi=value_abs,
            unit_label=unit_label or 'PSI',
            barometric_psi=self._get_barometric_pressure(port_id),
            pressure_reference=effective_reference,
        )
        return float(converted if converted is not None else value_abs)

    def _set_debug_dio_direction(self, port_id: str, payload: Dict[str, Any]) -> None:
        port = self._port_manager.get_port(port_id)
        if not port:
            return
        dio = payload.get("dio")
        if dio is None:
            return
        try:
            dio_index = int(dio)
        except (TypeError, ValueError):
            return
        is_output = bool(payload.get("is_output", False))
        output_state = payload.get("output_state")
        if output_state is not None:
            output_state = 1 if int(output_state) else 0
        success = port.daq.set_dio_direction(dio_index, is_output, output_state)
        if success:
            logger.info(
                "%s: DIO%d set to %s",
                port_id,
                dio_index,
                "output" if is_output else "input",
            )
            self._read_debug_dio_all(port_id)
        else:
            self._ui_bridge.show_error_message(
                "Debug DIO",
                f"Failed to set DIO{dio_index} on {port_id}.",
            )

    def _read_debug_dio_all(self, port_id: str) -> None:
        port = self._port_manager.get_port(port_id)
        if not port:
            return
        values = port.daq.read_dio_values(max_dio=22)
        if values is None:
            self._ui_bridge.show_error_message(
                "Debug DIO",
                f"Failed to read DIO values for {port_id}.",
            )
            return
        self._ui_bridge.update_debug_dio(port_id, values)

    def _set_debug_solenoid_mode(self, port_id: str, mode: str) -> None:
        normalized = str(mode or "atmosphere").strip().lower()
        if normalized not in {"auto", "atmosphere", "vacuum"}:
            normalized = "atmosphere"

        if self._debug_alicat_mode.get(port_id) == "vent" and normalized != "atmosphere":
            logger.info(
                "%s: Ignoring solenoid mode '%s' while in vent mode; forcing atmosphere",
                port_id,
                normalized,
            )
            normalized = "atmosphere"

        self._debug_solenoid_mode[port_id] = normalized

        port = self._port_manager.get_port(port_id)
        if not port:
            return

        if normalized == "auto":
            self._apply_debug_solenoid_auto(port_id, self._get_latest_reading(port_id), force=True)
            return

        to_vacuum = normalized == "vacuum"
        if to_vacuum:
            connect = getattr(port, 'connect_test_route', None)
            success = connect() if callable(connect) else port.set_solenoid(True)
        else:
            success = port.set_solenoid(False)
        if success:
            self._debug_solenoid_last_route[port_id] = to_vacuum
        logger.info(
            "%s: Solenoid debug mode set to %s",
            port_id,
            normalized,
        )

    def _extract_gauge_pressure_psi(self, port_id: str, reading: Optional[PortReading]) -> Optional[float]:
        if reading is None or reading.alicat is None:
            return None

        if reading.alicat.gauge_pressure is not None:
            return float(reading.alicat.gauge_pressure)

        if reading.alicat.pressure is None:
            return None
        barometric = self._get_barometric_pressure(port_id)
        return float(reading.alicat.pressure - barometric)

    def _apply_debug_solenoid_auto(
        self,
        port_id: str,
        reading: Optional[PortReading],
        force: bool = False,
    ) -> None:
        if self._debug_solenoid_mode.get(port_id) != "auto":
            return

        port = self._port_manager.get_port(port_id)
        if not port:
            return

        target_to_vacuum = True
        last_route = self._debug_solenoid_last_route.get(port_id)
        if not force and last_route is not None and last_route == target_to_vacuum:
            return

        connect = getattr(port, 'connect_test_route', None)
        success = connect() if callable(connect) else port.set_solenoid(True)
        if success:
            self._debug_solenoid_last_route[port_id] = target_to_vacuum
            logger.debug(
                "%s: Auto solenoid -> active test route",
                port_id,
            )
        else:
            self._debug_solenoid_last_route[port_id] = False
            logger.warning(
                "%s: Auto solenoid failed to connect active test route",
                port_id,
            )

    def _resolve_sweep_bounds(self, port_id: str) -> Optional[tuple[float, float]]:
        labjack_cfg = self._config.get("hardware", {}).get("labjack", {})
        port_cfg = labjack_cfg.get(port_id, {})
        return resolve_sweep_bounds(self._current_test_setup, port_cfg)

    def _resolve_sweep_rates(self) -> tuple[float, float, float]:
        control_cfg = parse_control_config(self._config)
        slow_torr_per_sec = control_cfg.ramps.precision_sweep_rate_torr_per_sec
        slow_rate = convert_pressure(slow_torr_per_sec, "Torr", "PSI")
        medium_rate = slow_rate * 3.0
        fast_rate = slow_rate * 10.0
        return slow_rate, medium_rate, fast_rate

    def _create_debug_sweep_executor(self, port_id: str, port: Port) -> TestExecutor:
        setup = self._current_test_setup
        if setup is None:
            raise RuntimeError('No active test setup for debug sweep')

        return TestExecutor(
            port_id=port_id,
            port=port,
            test_setup=setup,
            config=self._config,
            get_latest_reading=self._get_latest_reading,
            get_barometric_psi=self._get_barometric_pressure,
        )

    # ------------------------------------------------------------------
    # State machine signal forwarding
    # ------------------------------------------------------------------

    def _on_sm_state_changed(self, port_id: str, state: str, data: dict) -> None:
        self._ui_bridge.update_state(port_id, state, data)
        if state == PortState.PRECISION_TEST.value:
            self._apply_precision_zoom(port_id)

    def _on_sm_substate_changed(self, port_id: str, substate: str, data: dict) -> None:
        self._ui_bridge.update_substate(port_id, substate, data)

    def _on_sm_button_changed(self, port_id: str, data: dict) -> None:
        self._ui_bridge.update_buttons(port_id, data)

    def _on_sm_test_result(self, port_id: str, result: dict) -> None:
        self._ui_bridge.test_result_ready.emit(port_id, result)

    def _on_sm_error(self, port_id: str, message: str) -> None:
        normalized = (message or '').strip().lower()
        if 'no edges detected during precision sweep' in normalized:
            logger.warning('%s: Precision sweep failed without edges (non-modal)', port_id)
            return
        logger.warning('%s: Test error (non-modal): %s', port_id, message)

    # ------------------------------------------------------------------
    # Test-control signal handlers
    # ------------------------------------------------------------------

    def _on_start_pressurize(self, port_id: str) -> None:
        sm = self._state_machines.get(port_id)
        if not sm:
            return
        if self._is_low_pressure_transducer_locked_out(port_id):
            self._show_low_pressure_transducer_lockout(port_id)
            return
        if not self._has_switch_presence(port_id):
            logger.info('%s: Pressurize blocked until switch presence is detected', port_id)
            return
        if sm.trigger('start_pressurize'):
            self._start_pressurize_hw(port_id)

    def _on_start_test(self, port_id: str) -> None:
        sm = self._state_machines.get(port_id)
        if not sm:
            return
        if self._is_low_pressure_transducer_locked_out(port_id):
            self._show_low_pressure_transducer_lockout(port_id)
            return
        if not self._has_switch_presence(port_id):
            logger.info('%s: Test start blocked until switch presence is detected', port_id)
            return
        if sm.trigger('start_test'):
            existing = self._test_executors.get(port_id)
            if existing and existing.is_running:
                logger.warning('%s: Test already running — ignoring duplicate start', port_id)
                return
            self._precision_grant_events.pop(port_id, None)
            self._launch_test_executor(port_id)
            return
        logger.warning(
            '%s: Test start ignored in state %s (switch_ready=%s)',
            port_id,
            sm.current_state,
            getattr(sm, '_switch_has_changed', None),
        )
        if self._ui_bridge:
            self._ui_bridge.show_info_message(
                'Test',
                (
                    f'{port_id.upper().replace("_", " ")}: start test not allowed in '
                    f'{sm.current_state}. For QAL15, twist the SEI until the switch '
                    'changes, then press Test again.'
                ),
            )

    def _on_cancel(self, port_id: str) -> None:
        self._restore_normal_viz(port_id)
        was_owner = self._precision_owner_port == port_id
        self._remove_precision_waiter(port_id)
        executor = self._test_executors.get(port_id)
        if executor and executor.is_running:
            logger.info('%s: Cancel requested — stopping test executor', port_id)
            executor.request_cancel()
            sm = self._state_machines.get(port_id)
            if sm and not sm.trigger('cancel'):
                sm.trigger('reset')
            if was_owner:
                self._release_precision_slot(port_id, reason='cancel')
            # Executor thread calls _safe_vent(); avoid racing exhaust here.
            return
        sm = self._state_machines.get(port_id)
        if sm:
            if not sm.trigger('cancel'):
                sm.trigger('reset')
        self._vent_port(port_id)
        if was_owner:
            self._release_precision_slot(port_id, reason='cancel')

    def _on_vent(self, port_id: str) -> None:
        executor = self._test_executors.get(port_id)
        if executor and executor.is_running:
            logger.info('%s: Vent requested during test — cancelling executor first', port_id)
            executor.request_cancel()
            sm = self._state_machines.get(port_id)
            if sm:
                sm.trigger('vent')
            return
        sm = self._state_machines.get(port_id)
        if sm:
            sm.trigger('vent')
        self._vent_port(port_id)

    def _is_no_switch_decision(self, sm: PortStateMachine) -> bool:
        """Return True when the port is waiting on the no-switch retry/fail choice."""
        return (
            sm.current_state == PortState.ERROR.value
            and sm.current_substate == PortSubstate.ERROR_NO_SWITCH.value
        )

    def _on_record_success(self, port_id: str) -> None:
        self._restore_normal_viz(port_id)
        sm = self._state_machines.get(port_id)
        if not sm:
            return
        self._save_result(port_id, force_pass=True)
        sm.trigger('record_success')
        self._advance_serial(port_id)

    def _on_record_failure(self, port_id: str) -> None:
        self._restore_normal_viz(port_id)
        sm = self._state_machines.get(port_id)
        if not sm:
            return
        if self._is_no_switch_decision(sm):
            result = self._save_result(
                port_id,
                force_pass=False,
                allow_null_measurements=True,
            )
            if result == 'failed':
                return
            if not sm.trigger('fail_no_switch'):
                sm.trigger('reset')
            self._advance_serial(port_id)
            return
        self._save_result(port_id, force_pass=False)
        sm.trigger('record_failure')
        self._advance_serial(port_id)

    def _on_retest(self, port_id: str) -> None:
        self._restore_normal_viz(port_id)
        sm = self._state_machines.get(port_id)
        if not sm:
            return
        if self._is_low_pressure_transducer_locked_out(port_id):
            self._show_low_pressure_transducer_lockout(port_id)
            return
        if self._is_no_switch_decision(sm):
            result = self._save_result(
                port_id,
                force_pass=False,
                allow_null_measurements=True,
            )
            if result == 'failed':
                return
            if sm.trigger('retry_no_switch'):
                if sm.current_state == PortState.CYCLING.value:
                    self._launch_test_executor(port_id)
                elif sm.current_state == PortState.PRESSURIZING.value:
                    self._start_pressurize_hw(port_id)
            return
        if sm.trigger('retest'):
            # Retest transitions to CYCLING (QAL16/17) or PRESSURIZING (QAL15)
            if sm.current_state == PortState.CYCLING.value:
                self._launch_test_executor(port_id)
            elif sm.current_state == PortState.PRESSURIZING.value:
                self._start_pressurize_hw(port_id)

    def _activation_target_torr(self) -> Optional[float]:
        setup = self._current_test_setup
        if not setup or setup.activation_target is None:
            return None
        try:
            return convert_pressure(
                float(setup.activation_target),
                setup.units_label or 'PSI',
                'Torr',
            )
        except (TypeError, ValueError):
            return None

    def _is_low_pressure_transducer_locked_out(self, port_id: str) -> bool:
        activation_torr = self._activation_target_torr()
        if activation_torr is None or activation_torr >= LOW_PRESSURE_TRANSDUCER_LOCKOUT_TORR:
            return False

        port_cfg = (
            self._config
            .get('hardware', {})
            .get('labjack', {})
            .get(port_id, {})
        )
        return not bool(port_cfg.get('transducer_installed', False))

    def _show_low_pressure_transducer_lockout(self, port_id: str) -> None:
        logger.warning(
            '%s: %s',
            port_id,
            LOW_PRESSURE_TRANSDUCER_LOCKOUT_MESSAGE,
        )
        self._ui_bridge.show_error_message(
            'Transducer Required',
            LOW_PRESSURE_TRANSDUCER_LOCKOUT_MESSAGE,
        )

    @staticmethod
    def _pressurize_target_reached(
        pressure_abs_psi: float,
        target_abs_psi: float,
        direction: int,
        tolerance_psi: float,
    ) -> bool:
        if abs(pressure_abs_psi - target_abs_psi) <= tolerance_psi:
            return True
        if direction > 0:
            return pressure_abs_psi >= target_abs_psi
        return pressure_abs_psi <= target_abs_psi

    @staticmethod
    def _switch_is_activated(reading: Optional[PortReading]) -> Optional[bool]:
        switch = getattr(reading, 'switch', None) if reading is not None else None
        if switch is None:
            return None
        return bool(switch.switch_activated)

    def _manual_pressurize_target_switch_state(
        self,
        port_id: str,
        sweep_mode: str,
    ) -> bool:
        """Switch state that means QAL15 manual pressurize has crossed activation."""
        if sweep_mode == 'vacuum':
            port = self._port_manager.get_port(port_id)
            daq = getattr(port, 'daq', None) if port is not None else None
            if daq is not None:
                if bool(getattr(daq, 'switch_nc_derived_from_no', False)):
                    return False
                if bool(getattr(daq, 'switch_no_derived_from_nc', False)):
                    return True

            port_cfg = self._config.get('hardware', {}).get('labjack', {}).get(port_id, {})
            trips_on_no_open = port_cfg.get(
                'vacuum_switch_trips_on_no_open',
                port_cfg.get('switch_nc_derived_from_no'),
            )
            if _config_bool(trips_on_no_open):
                return False
        return True

    def _resolve_qal15_pressurize_target_psi(
        self,
        setup: TestSetup,
        bounds: tuple[float, float],
        atmosphere_psi: float,
    ) -> float:
        """Target well past the PTP band on the activation side."""
        min_psi, max_psi = bounds
        overshoot_torr = float(
            self._config.get('control', {}).get('manual_pressurize_overshoot_torr', 120.0)
        )
        overshoot_psi = max(
            convert_pressure(overshoot_torr, 'Torr', 'PSI'),
            (max_psi - min_psi) * 0.25,
            convert_pressure(25.0, 'Torr', 'PSI'),
        )
        direction = str(setup.activation_direction or '').strip().lower()
        activation_psi = convert_pressure(
            setup.activation_target,
            setup.units_label or 'PSI',
            'PSI',
        )
        if direction.startswith('increas'):
            target = max(activation_psi, max_psi) + overshoot_psi
            if ptp_limits_use_psia_scale(setup, {}, atmosphere_psi):
                atmosphere_margin = convert_pressure(10.0, 'Torr', 'PSI')
                return min(target, atmosphere_psi - atmosphere_margin)
            return target
        # Decreasing switches actuate while pressure falls, so manual
        # pressurize must first move above the reset/deactivation side.
        return max(activation_psi, max_psi) + overshoot_psi

    # ------------------------------------------------------------------
    # Hardware-level test operations
    # ------------------------------------------------------------------

    def _start_pressurize_hw(self, port_id: str) -> None:
        """Start pressurization in a background thread."""
        def _pressurize() -> None:
            self._hw_serial_busy_ports.add(port_id)
            try:
                port = self._port_manager.get_port(port_id)
                if not port:
                    return
                setup = self._current_test_setup
                if not setup:
                    return

                # Determine target pressure
                bounds = self._resolve_sweep_bounds(port_id)
                if not bounds:
                    return
                sweep_mode = self._resolve_sweep_mode(port_id, 'auto')

                # QAL15: go well past the PTP band on the actuation side so
                # badly adjusted switches can still be found before the
                # operator does SEI adjustment.
                sm = self._state_machines.get(port_id)
                is_qal15 = sm and sm._workflow_type == 'QAL15'

                if is_qal15 and setup.activation_target is not None:
                    # For gauge reference atmosphere is 0; for absolute
                    # it is the live barometric reading.
                    atmosphere_psi = self._determine_atmosphere_psi(
                        port_id, setup.pressure_reference,
                    )
                    target_psi = self._resolve_qal15_pressurize_target_psi(
                        setup,
                        bounds,
                        atmosphere_psi,
                    )

                    # Clamp to hardware transducer limits (in the PTP
                    # reference frame, i.e. gauge for gauge parts).
                    labjack_cfg = self._config.get('hardware', {}).get('labjack', {})
                    port_cfg = labjack_cfg.get(port_id, {})
                    hw_min = float(port_cfg.get('transducer_pressure_min', 0.0))
                    hw_max = float(port_cfg.get('transducer_pressure_max', 115.0))
                    # Config limits are absolute; convert to the same
                    # reference frame as the PTP values.
                    if str(setup.pressure_reference or '').strip().lower() == 'gauge':
                        baro = self._get_barometric_pressure(port_id)
                        hw_min_ref = hw_min - baro
                        hw_max_ref = hw_max - baro
                    else:
                        hw_min_ref = hw_min
                        hw_max_ref = hw_max

                    target_psi = max(hw_min_ref, min(target_psi, hw_max_ref))

                    logger.info(
                        '%s: QAL15 pressurize target = %.2f PSI '
                        '(direction=%s, bounds=%.2f..%.2f, atmosphere=%.2f, clamped to %.2f..%.2f)',
                        port_id,
                        target_psi,
                        setup.activation_direction,
                        bounds[0],
                        bounds[1],
                        atmosphere_psi,
                        hw_min_ref,
                        hw_max_ref,
                    )
                else:
                    # QAL16/17: use far side of PTP sweep bounds
                    target_psi = bounds[1] if sweep_mode == 'pressure' else bounds[0]

                # QAL15 sequence (300) should move quickly into the manual
                # adjust window.  Alicat treats a zero ramp rate as no ramp,
                # not an instant jump, so use a high positive rate instead.
                if is_qal15:
                    pressurize_rate = max(
                        self._resolve_sweep_rates()[2],
                        convert_pressure(300.0, 'Torr', 'PSI'),
                    )
                else:
                    pressurize_rate = self._resolve_sweep_rates()[2]

                target_abs = self._to_absolute_pressure(
                    port_id, target_psi,
                    setup.pressure_reference,
                )
                barometric_psi = self._get_barometric_pressure(port_id)
                alicat_ref = self._infer_alicat_setpoint_reference(port_id, barometric_psi)
                command_psi = to_alicat_setpoint_psi(
                    target_abs,
                    barometric_psi=barometric_psi,
                    setpoint_reference=alicat_ref,
                )
                logger.info(
                    '%s: Setting pressure setpoint command=%.4f PSI (target_abs=%.4f PTP=%.4f ref=%s)',
                    port_id,
                    command_psi,
                    target_abs,
                    target_psi,
                    alicat_ref,
                )
                staging_rate = max(pressurize_rate, 50.0)
                if not port.alicat.set_ramp_rate(staging_rate):
                    logger.warning(
                        '%s: High atmosphere staging ramp %.4f PSI/s rejected; falling back to %.4f PSI/s',
                        port_id,
                        staging_rate,
                        pressurize_rate,
                    )
                    staging_rate = pressurize_rate
                    if not port.alicat.set_ramp_rate(staging_rate):
                        logger.error(
                            '%s: Failed to set atmosphere staging ramp rate to %.4f PSI/s',
                            port_id,
                            staging_rate,
                        )
                        return
                atmosphere_command = to_alicat_setpoint_psi(
                    barometric_psi,
                    barometric_psi=barometric_psi,
                    setpoint_reference=alicat_ref,
                )
                if not port.set_pressure(atmosphere_command):
                    logger.error('%s: Failed to stage Alicat setpoint at atmosphere', port_id)
                    return
                port.alicat.cancel_hold()
                time.sleep(min(3.0, max(0.35, barometric_psi / max(staging_rate, 0.1) + 0.1)))

                logger.info('%s: Connecting active test route for %s pressurize', port_id, sweep_mode)
                if not self._set_active_test_route(port, port_id, sweep_mode, "manual pressurize"):
                    return
                if not port.alicat.set_ramp_rate(pressurize_rate):
                    logger.error('%s: Failed to set ramp rate to %.4f PSI/s', port_id, pressurize_rate)
                    return
                if not port.set_pressure(command_psi):
                    logger.error('%s: Failed to set pressure setpoint', port_id)
                    return
                if not port.alicat.cancel_hold():
                    logger.warning('%s: Failed to cancel Alicat hold', port_id)

                # Wait for pressure to reach the actual commanded target.  Use
                # the target-vs-atmosphere direction instead of the broad sweep
                # mode so QAL15 absolute-scale mmHg pre-pressurize can finish
                # after intentionally pulling below the activation setpoint.
                start = time.perf_counter()
                wait_reference_psi = self._get_barometric_pressure(port_id)
                direction = 1 if target_abs >= wait_reference_psi else -1
                reach_tolerance_psi = max(
                    0.15,
                    abs(target_abs) * 0.03,
                    convert_pressure(10.0, 'Torr', 'PSI'),
                )
                if is_qal15:
                    timeout = float(
                        self._config.get('control', {})
                        .get('manual_pressurize_timeout_sec', 20.0)
                    )
                else:
                    timeout = float(
                        self._config.get('control', {})
                        .get('edge_detection', {})
                        .get('timeout_sec', 60.0)
                    )
                measurement_settings = get_measurement_settings(self._config)
                reached_target = False
                switch_actuated = False
                target_switch_state = self._manual_pressurize_target_switch_state(
                    port_id,
                    sweep_mode,
                )
                initial_switch_state: Optional[bool] = None
                target_switch_since: Optional[float] = None
                last_pressure_abs_psi = None
                last_source_used = None
                while time.perf_counter() - start < timeout:
                    reading = self._get_latest_reading(port_id)
                    if reading is not None:
                        current_switch_active = self._switch_is_activated(reading)
                        if initial_switch_state is None:
                            initial_switch_state = current_switch_active
                        if (
                            is_qal15
                            and current_switch_active == target_switch_state
                        ):
                            now = time.perf_counter()
                            if target_switch_since is None:
                                target_switch_since = now
                            elif (
                                current_switch_active != initial_switch_state
                                or now - target_switch_since >= 0.20
                            ):
                                switch_actuated = True
                                break
                        else:
                            target_switch_since = None
                        pressure_abs_psi, _source_used = select_main_pressure_abs_psi(
                            reading=reading,
                            settings=measurement_settings,
                            barometric_psi=self._get_barometric_pressure(port_id),
                        )
                        last_pressure_abs_psi = pressure_abs_psi
                        last_source_used = _source_used
                        if pressure_abs_psi is None:
                            time.sleep(0.05)
                            continue
                        if self._pressurize_target_reached(
                            pressure_abs_psi,
                            target_abs,
                            direction,
                            reach_tolerance_psi,
                        ):
                            reached_target = True
                            break
                    time.sleep(0.05)

                if switch_actuated:
                    logger.info(
                        '%s: QAL15 pressurize completed on switch transition to %s',
                        port_id,
                        target_switch_state,
                    )
                elif reached_target:
                    logger.info(
                        '%s: Pressurize target reached at %.4f PSI via %s',
                        port_id,
                        last_pressure_abs_psi if last_pressure_abs_psi is not None else float('nan'),
                        last_source_used or 'unknown',
                    )
                elif is_qal15:
                    logger.warning(
                        '%s: QAL15 pressurize did not confirm target within %.1fs '
                        '(last=%s PSI via %s, target=%.4f PSI); continuing to manual adjust',
                        port_id,
                        timeout,
                        f'{last_pressure_abs_psi:.4f}' if last_pressure_abs_psi is not None else '--',
                        last_source_used or 'unknown',
                        target_abs,
                    )

                self._sig_pressure_reached.emit(port_id)
            except Exception as exc:
                logger.error('Pressurize failed for %s: %s', port_id, exc, exc_info=True)
                self._sig_trigger_error.emit(port_id, str(exc))
            finally:
                self._hw_serial_busy_ports.discard(port_id)

        threading.Thread(target=_pressurize, daemon=True).start()

    def _launch_test_executor(self, port_id: str) -> None:
        """Create and start a TestExecutor for the given port."""
        current_executor = self._test_executors.get(port_id)
        if current_executor and current_executor.is_running:
            msg = f'{port_id} test is already running'
            logger.warning(msg)
            self._sig_trigger_error.emit(port_id, msg)
            return

        port = self._port_manager.get_port(port_id)
        if not port:
            logger.error('Cannot launch test executor: port %s not found', port_id)
            return
        setup = self._current_test_setup
        if not setup:
            logger.error('Cannot launch test executor: no test setup loaded')
            return
        
        # Reset measured values for new test
        if port_id in self._current_measured_values:
            self._current_measured_values[port_id]['activation'] = None
            self._current_measured_values[port_id]['deactivation'] = None
        self._max_test_pressure_display[port_id] = None
        self._cycle_estimates_abs_psi.pop(port_id, None)
        if self._ui_bridge:
            self._ui_bridge.update_pressure_viz(
                port_id,
                {
                    'measured_activation': None,
                    'measured_deactivation': None,
                    'estimated_activation': None,
                    'estimated_deactivation': None,
                    'estimated_sample_count': 0,
                },
            )

        def _on_cycling_complete() -> None:
            self._sig_cycles_complete.emit(port_id)

        precision_grant_event = threading.Event()
        self._precision_grant_events[port_id] = precision_grant_event

        def _wait_for_precision_slot() -> bool:
            while not precision_grant_event.wait(0.05):
                current = self._test_executors.get(port_id)
                if current is not executor or executor.cancel_requested:
                    return False
            return self._precision_owner_port == port_id and not executor.cancel_requested

        def _on_edges_captured(act_psi: float, deact_psi: float) -> None:
            self._sig_edges_captured.emit(port_id, act_psi, deact_psi)

        def _on_edge_detected(edge_type: str, pressure_psi: float) -> None:
            """Callback for immediate edge detection during precision test."""
            self._sig_edge_detected.emit(port_id, edge_type, pressure_psi)

        def _on_error(message: str) -> None:
            self._sig_trigger_error.emit(port_id, message)

        def _on_event(event: TestEvent) -> None:
            logger.info('%s: test_event=%s data=%s', event.port_id, event.event_type, event.data)

        def _on_cancelled() -> None:
            self._sig_cancelled.emit(port_id)

        def _on_substate(substate: str) -> None:
            self._sig_substate.emit(port_id, substate)

        def _on_cycle_estimate(
            activation_psi: Optional[float],
            deactivation_psi: Optional[float],
            sample_count: int,
        ) -> None:
            self._sig_cycle_estimate.emit(port_id, activation_psi, deactivation_psi, sample_count)

        executor = TestExecutor(
            port_id=port_id,
            port=port,
            test_setup=setup,
            config=self._config,
            get_latest_reading=self._get_latest_reading,
            get_barometric_psi=self._get_barometric_pressure,
            on_cycling_complete=_on_cycling_complete,
            on_substate_update=_on_substate,
            on_edges_captured=_on_edges_captured,
            on_edge_detected=_on_edge_detected,
            on_cycle_estimate=_on_cycle_estimate,
            on_error=_on_error,
            on_cancelled=_on_cancelled,
            on_event=_on_event,
            wait_for_precision_slot=_wait_for_precision_slot,
        )
        self._test_executors[port_id] = executor
        executor.start()

    # ------------------------------------------------------------------
    # Cross-thread signal handler slots (run on main thread)
    # ------------------------------------------------------------------

    def _slot_pressure_reached(self, port_id: str) -> None:
        sm = self._state_machines.get(port_id)
        if sm:
            sm.trigger('pressure_reached')

    def _slot_trigger_error(self, port_id: str, message: str) -> None:
        """Handle test error - transition state machine to error state."""
        sm = self._state_machines.get(port_id)
        if not sm:
            logger.warning('%s: Cannot trigger error - state machine not found', port_id)
            return
        was_precision = sm.current_state == PortState.PRECISION_TEST.value
        normalized = (message or '').strip().lower()

        no_switch_failure = (
            'no_switch_detected' in normalized
            or 'no switch detected' in normalized
        )
        if no_switch_failure:
            logger.warning('%s: No-switch test failure: %s', port_id, message)
            if sm.can_trigger('error'):
                sm.trigger('error', message=message)
            else:
                logger.warning(
                    '%s: Cannot enter no-switch error from state %s',
                    port_id,
                    sm.current_state,
                )
            self._vent_port(port_id)
            if was_precision:
                self._release_precision_slot(port_id, reason='no-switch-failure')
            return

        # Treat edge/switch-miss as a recoverable test failure:
        # vent to atmosphere, return to IDLE, and notify operator.
        recoverable_failure = (
            'edge_not_found' in normalized
            or 'activation edge not detected' in normalized
            or 'deactivation edge not detected' in normalized
            or 'target_timeout' in normalized
            or 'route_failure' in normalized
        )
        if recoverable_failure:
            logger.warning('%s: Recoverable test failure: %s', port_id, message)
            if sm.can_trigger('cancel'):
                sm.trigger('cancel')
            elif sm.can_trigger('vent'):
                sm.trigger('vent')
            self._vent_port(port_id)
            if was_precision:
                self._release_precision_slot(port_id, reason='recoverable-failure')
            if self._ui_bridge:
                self._ui_bridge.show_info_message(
                    'Test Failed',
                    f'{port_id.upper().replace("_", " ")}: {message}',
                )
            return
        
        # Only transition to error if we're in a state that allows it
        # If already in error state, just update the message
        if sm.current_state == PortState.ERROR.value:
            logger.debug('%s: Already in error state, updating error message', port_id)
            sm._last_error = message
            return
        
        # Check if error transition is valid from current state
        if not sm.can_trigger('error'):
            logger.warning(
                '%s: Cannot trigger error from state %s - venting port instead',
                port_id,
                sm.current_state,
            )
            # If we can't transition to error, at least vent the port
            self._vent_port(port_id)
            if was_precision:
                self._release_precision_slot(port_id, reason='error-no-transition')
            return
        
        # Trigger error transition
        logger.info('%s: Triggering error transition: %s', port_id, message)
        sm.trigger('error', message=message)
        if was_precision:
            self._release_precision_slot(port_id, reason='error')
        
        # Ensure port is vented after error state is set
        # (TestExecutor should have already vented, but ensure it's done)
        self._vent_port(port_id)

    def _slot_cycles_complete(self, port_id: str) -> None:
        sm = self._state_machines.get(port_id)
        if not sm:
            return
        if sm.can_trigger('cycles_complete'):
            if self._request_precision_slot(port_id):
                sm.trigger('cycles_complete')
            return
        logger.debug(
            '%s: Ignoring late cycles_complete while in state %s',
            port_id,
            sm.current_state,
        )

    def _slot_edge_detected(self, port_id: str, edge_type: str, pressure_psi: float) -> None:
        """Handle immediate edge detection during precision test - update graph indicator right away."""
        setup = self._current_test_setup
        if not setup or not self._ui_bridge:
            return
        
        unit_label = self._ui_bridge.get_pressure_unit()
        pressure_display = self._to_display_pressure(
            port_id, pressure_psi, unit_label,
            setup.pressure_reference,
        )
        
        # Update tracked values
        if port_id in self._current_measured_values:
            if edge_type == 'activation':
                self._current_measured_values[port_id]['activation'] = pressure_display
                logger.info('%s: Activation detected at %.4f PSI (display: %.4f %s)', 
                           port_id, pressure_psi, pressure_display, unit_label)
            elif edge_type == 'deactivation':
                self._current_measured_values[port_id]['deactivation'] = pressure_display
                logger.info('%s: Deactivation detected at %.4f PSI (display: %.4f %s)', 
                           port_id, pressure_psi, pressure_display, unit_label)
        
        # Update UI with both values (preserving existing one if only one is new)
        current = self._current_measured_values.get(port_id, {})
        viz_data = {
            'measured_activation': current.get('activation'),
            'measured_deactivation': current.get('deactivation'),
        }
        self._ui_bridge.update_pressure_viz(port_id, viz_data)

    def _slot_edges_captured(self, port_id: str, act_psi: float, deact_psi: float) -> None:
        sm = self._state_machines.get(port_id)
        if sm and sm.can_trigger('edges_captured'):
            in_spec = self._evaluate_precision_in_spec(port_id, act_psi, deact_psi)
            sm.set_measurements(act_psi, deact_psi, in_spec=in_spec)
            sm.trigger('edges_captured')
        elif sm:
            logger.debug(
                '%s: Ignoring late edges_captured while in state %s',
                port_id,
                sm.current_state,
            )
        # Always try release in case completion arrives after a local cancellation.
        self._release_precision_slot(port_id, reason='edges-captured-signal')
        # Update pressure viz with measured points (final update with both values)
        setup = self._current_test_setup
        if setup and self._ui_bridge:
            unit_label = self._ui_bridge.get_pressure_unit()
            act_display = self._to_display_pressure(
                port_id, act_psi, unit_label,
                setup.pressure_reference,
            )
            deact_display = self._to_display_pressure(
                port_id, deact_psi, unit_label,
                setup.pressure_reference,
            )
            # Update tracked values
            if port_id in self._current_measured_values:
                self._current_measured_values[port_id]['activation'] = act_display
                self._current_measured_values[port_id]['deactivation'] = deact_display
            
            self._ui_bridge.update_pressure_viz(
                port_id,
                {
                    'measured_activation': act_display,
                    'measured_deactivation': deact_display,
                },
            )

    def _evaluate_precision_in_spec(self, port_id: str, act_psi: float, deact_psi: float) -> bool:
        setup = self._current_test_setup
        if not setup:
            logger.warning('%s: Missing setup for in-spec evaluation', port_id)
            return False

        mapped = self._resolve_mapped_acceptance_bands_psi(port_id, setup)
        if mapped is None:
            logger.warning('%s: Missing/invalid acceptance bands for in-spec evaluation', port_id)
            return False

        activation_band, deactivation_band = mapped
        activation_ok = self._is_value_within_band(act_psi, activation_band)
        deactivation_ok = self._is_value_within_band(deact_psi, deactivation_band)
        in_spec = activation_ok and deactivation_ok

        logger.info(
            '%s: In-spec eval act=%.4f in [%.4f, %.4f]=%s deact=%.4f in [%.4f, %.4f]=%s => %s',
            port_id,
            act_psi,
            activation_band[0],
            activation_band[1],
            activation_ok,
            deact_psi,
            deactivation_band[0],
            deactivation_band[1],
            deactivation_ok,
            in_spec,
        )
        return in_spec

    def _resolve_mapped_acceptance_bands_psi(
        self,
        port_id: str,
        setup: TestSetup,
    ) -> Optional[tuple[tuple[float, float], tuple[float, float]]]:
        direction = (setup.activation_direction or '').strip().lower()
        if direction == 'decreasing':
            activation_band_raw = setup.bands.get('decreasing')
            deactivation_band_raw = setup.bands.get('increasing')
        else:
            activation_band_raw = setup.bands.get('increasing')
            deactivation_band_raw = setup.bands.get('decreasing')

        fallback_low, fallback_high = self._resolve_eval_bounds_psi(port_id, setup)

        activation_band = self._band_to_psi(
            activation_band_raw,
            setup.units_label,
            fallback_low,
            fallback_high,
            setup.pressure_reference,
        )
        deactivation_band = self._band_to_psi(
            deactivation_band_raw,
            setup.units_label,
            fallback_low,
            fallback_high,
            setup.pressure_reference,
        )
        if activation_band is None or deactivation_band is None:
            return None
        return activation_band, deactivation_band

    def _resolve_eval_bounds_psi(self, port_id: str, setup: TestSetup) -> tuple[float, float]:
        labjack_cfg = self._config.get('hardware', {}).get('labjack', {})
        port_cfg = labjack_cfg.get(port_id, {})
        min_psi, max_psi = resolve_sweep_bounds(setup, port_cfg)
        return min(min_psi, max_psi), max(min_psi, max_psi)

    def _band_to_psi(
        self,
        band: Optional[Dict[str, Optional[float]]],
        units_label: Optional[str],
        fallback_low_psi: float,
        fallback_high_psi: float,
        pressure_reference: Optional[str] = None,
        barometric_psi: float = 14.7,
    ) -> Optional[tuple[float, float]]:
        if not band:
            return None
        lower = band.get('lower')
        upper = band.get('upper')
        if lower is None or upper is None:
            return None
        lower_psi = convert_pressure(lower, units_label or 'PSI', 'PSI')
        upper_psi = convert_pressure(upper, units_label or 'PSI', 'PSI')

        if not math.isfinite(lower_psi):
            lower_psi = fallback_low_psi
        if not math.isfinite(upper_psi):
            upper_psi = fallback_high_psi
        if not (math.isfinite(lower_psi) and math.isfinite(upper_psi)):
            return None
        if lower_psi <= upper_psi:
            return (lower_psi, upper_psi)
        return (upper_psi, lower_psi)

    @staticmethod
    def _is_value_within_band(value: float, band: tuple[float, float]) -> bool:
        low, high = band
        if not math.isfinite(value):
            return False
        return low <= value <= high

    def _slot_cancelled(self, port_id: str) -> None:
        logger.info('%s: Test cancelled', port_id)
        self._remove_precision_waiter(port_id)
        self._release_precision_slot(port_id, reason='executor-cancelled')

    def _slot_substate(self, port_id: str, substate: str) -> None:
        if self._ui_bridge:
            self._ui_bridge.update_substate(port_id, substate, {})

    @pyqtSlot(str, object, object, int)
    def _slot_cycle_estimate(
        self,
        port_id: str,
        activation_psi: Optional[float],
        deactivation_psi: Optional[float],
        sample_count: int,
    ) -> None:
        self._cycle_estimates_abs_psi[port_id] = {
            'activation': activation_psi,
            'deactivation': deactivation_psi,
            'count': sample_count,
        }
        setup = self._current_test_setup
        unit_label = self._ui_bridge.get_pressure_unit() if self._ui_bridge else 'PSI'
        pressure_reference = setup.pressure_reference if setup else None

        activation_display = (
            self._to_display_pressure(
                port_id,
                activation_psi,
                unit_label,
                pressure_reference,
            )
            if activation_psi is not None
            else None
        )
        deactivation_display = (
            self._to_display_pressure(
                port_id,
                deactivation_psi,
                unit_label,
                pressure_reference,
            )
            if deactivation_psi is not None
            else None
        )
        self._ui_bridge.update_pressure_viz(
            port_id,
            {
                'estimated_activation': activation_display,
                'estimated_deactivation': deactivation_display,
                'estimated_sample_count': sample_count,
            },
        )

    def _apply_precision_zoom(self, port_id: str) -> None:
        """Zoom the pressure bar to the area of interest for precision testing.

        Uses the stored base viz dict (already in display units) to avoid
        reference-frame conversion issues.  Preserves bands and estimated
        points so partial-update side-effects don't clear them.
        """
        if not self._base_viz or not self._ui_bridge:
            return

        display_values: list[float] = []

        for band_key in ('activation_band', 'deactivation_band'):
            band = self._base_viz.get(band_key)
            if band:
                for v in band:
                    if v is not None and math.isfinite(v):
                        display_values.append(v)

        estimates = self._cycle_estimates_abs_psi.get(port_id, {})
        setup = self._current_test_setup
        unit_label = self._ui_bridge.get_pressure_unit()
        pressure_reference = setup.pressure_reference if setup else None
        for key in ('activation', 'deactivation'):
            val = estimates.get(key)
            if val is not None and math.isfinite(val):
                display_val = self._to_display_pressure(
                    port_id, val, unit_label, pressure_reference,
                )
                display_values.append(display_val)

        if len(display_values) < 2:
            return

        raw_min = min(display_values)
        raw_max = max(display_values)
        span = raw_max - raw_min
        buffer = max(span * 0.20, 1.0)
        zoomed_min = raw_min - buffer
        zoomed_max = raw_max + buffer

        self._precision_zoom_active[port_id] = True
        zoom_viz: Dict[str, Any] = {
            'min_psi': zoomed_min,
            'max_psi': zoomed_max,
            'show_atmosphere_reference': False,
            'activation_band': self._base_viz.get('activation_band'),
            'deactivation_band': self._base_viz.get('deactivation_band'),
        }
        self._ui_bridge.update_pressure_viz(port_id, zoom_viz)
        logger.info(
            '%s: Precision zoom applied: %.2f–%.2f %s',
            port_id, zoomed_min, zoomed_max, unit_label,
        )

    def _restore_normal_viz(self, port_id: str) -> None:
        """Restore the normal (full-range) pressure bar visualization for a port."""
        if not self._precision_zoom_active.get(port_id):
            return
        self._precision_zoom_active[port_id] = False
        if not self._ui_bridge:
            return
        if self._base_viz:
            self._ui_bridge.update_pressure_viz(port_id, self._base_viz)
            logger.info('%s: Precision zoom cleared, normal viz restored', port_id)

    def _set_active_test_route(
        self,
        port: Port,
        port_id: str,
        sweep_mode: str,
        context: str,
    ) -> bool:
        """Connect the DUT to the Alicat-controlled test line for active moves."""
        if sweep_mode == 'pressure':
            connect = getattr(port, 'connect_test_route', None)
            success = connect() if callable(connect) else port.set_solenoid(True)
        else:
            success = port.set_solenoid(True)
        if not success:
            logger.error(
                '%s: Failed to connect active test route for %s (%s)',
                port_id,
                context,
                sweep_mode,
            )
        return bool(success)

    # ------------------------------------------------------------------

    def _vent_port(self, port_id: str) -> None:
        """Vent a port to atmosphere."""
        port = self._port_manager.get_port(port_id)
        if port:
            try:
                port.vent_to_atmosphere()
            except Exception as exc:
                logger.error('Vent failed for %s: %s', port_id, exc)

    def _save_result(
        self,
        port_id: str,
        force_pass: bool,
        *,
        allow_null_measurements: bool = False,
    ) -> str:
        """Save the test result to the database and surface the outcome to the UI."""
        sm = self._state_machines.get(port_id)
        wo = self._ui_bridge._current_work_order if self._ui_bridge else None
        if not sm or not wo:
            return 'skipped'

        # Get measurements from state machine
        act = sm._increasing_activation
        deact = sm._decreasing_deactivation

        if (act is None or deact is None) and not allow_null_measurements:
            logger.warning('%s: Cannot save result - missing measurements', port_id)
            return 'skipped'

        # Convert to display units for storage
        unit_label = self._ui_bridge.get_pressure_unit() if self._ui_bridge else 'PSI'
        setup = self._current_test_setup
        pressure_ref = setup.pressure_reference if setup else None
        act_display = (
            self._to_display_pressure(port_id, act, unit_label, pressure_ref)
            if act is not None
            else None
        )
        deact_display = (
            self._to_display_pressure(port_id, deact, unit_label, pressure_ref)
            if deact is not None
            else None
        )

        test_mode = wo.get('test_mode', False)
        if test_mode:
            logger.info(
                '%s: Test mode - skipping DB write (act=%s, deact=%s %s, pass=%s)',
                port_id,
                self._format_optional_pressure(act_display),
                self._format_optional_pressure(deact_display),
                unit_label,
                force_pass,
            )
            self._set_database_activity_status('Test Mode', last_write='Skipped', queue='0')
            return 'skipped'

        shop_order = wo.get('shop_order', '')
        part_id = wo.get('part_id', '')
        sequence_id = wo.get('sequence_id', '')
        operator_id = wo.get('operator_id', '')
        test_params = self._config.get('test_parameters', {})
        equipment_id = test_params.get('equipment_id', 'STINGER_01')
        try:
            temperature_c = float(test_params.get('default_temperature_c', 25.0))
        except (TypeError, ValueError):
            temperature_c = 25.0
            logger.warning(
                '%s: Invalid test_parameters.default_temperature_c; using %.1f C',
                port_id,
                temperature_c,
            )

        serial = self._ui_bridge._port_serials.get(port_id, 1)
        activation_id = sm._attempt_count + 1

        units_str = setup.units_label if setup else 'PSI'

        increasing_activation, decreasing_deactivation = self._map_result_to_database_fields(
            act_display,
            deact_display,
        )
        if allow_null_measurements and (increasing_activation is None or decreasing_deactivation is None):
            # The live OrderCalibrationDetail table does not allow NULL in these legacy
            # measurement columns, so no-switch failures use 0.0 as the no-measurement sentinel.
            increasing_activation = 0.0 if increasing_activation is None else increasing_activation
            decreasing_deactivation = 0.0 if decreasing_deactivation is None else decreasing_deactivation
        direction = (getattr(setup, 'activation_direction', '') or '').strip() if setup else ''
        logger.info(
            '%s: Direction-based result mapping: increasing_direction_point=%s, '
            'decreasing_direction_point=%s %s (activation=%s, deactivation=%s, '
            'TargetActivationDirection=%s)',
            port_id,
            self._format_optional_pressure(increasing_activation),
            self._format_optional_pressure(decreasing_deactivation),
            units_str or 'PSI',
            self._format_optional_pressure(act_display),
            self._format_optional_pressure(deact_display),
            direction or 'unknown',
        )

        try:
            queue_before = get_local_queue_count()
            observed_max_pressure = self._max_test_pressure_display.get(port_id)
            if observed_max_pressure is None:
                measured_values = [
                    value
                    for value in (act_display, deact_display)
                    if value is not None and math.isfinite(value)
                ]
                observed_max_pressure = max(measured_values) if measured_values else None
            try:
                gage_reference_diff = self._get_barometric_pressure(port_id)
            except Exception:
                logger.debug('%s: Unable to read barometric pressure for DB result', port_id, exc_info=True)
                gage_reference_diff = None
            success = save_test_result(
                shop_order=shop_order,
                part_id=part_id,
                sequence_id=sequence_id,
                serial_number=serial,
                increasing_activation=increasing_activation,
                decreasing_deactivation=decreasing_deactivation,
                in_spec=force_pass,
                temperature_c=temperature_c,
                units_of_measure=units_str or 'PSI',
                operator_id=operator_id,
                equipment_id=equipment_id,
                activation_id=activation_id,
                max_pressure_achieved=observed_max_pressure,
                gage_reference_diff=gage_reference_diff,
                order_qty=int(wo.get('total', 1) or 1),
                activation_target=getattr(setup, 'activation_target', None) if setup else None,
            )
            queue_after = get_local_queue_count()
        except Exception:
            logger.exception('%s: Unexpected error while saving test result', port_id)
            success = False
            queue_after = get_local_queue_count()
            queue_before = queue_after
        if not success:
            logger.error('%s: Failed to save test result (non-modal)', port_id)
            self._set_database_activity_status('Write Failed', queue=str(max(queue_after, 1)))
            return 'failed'

        status = 'Queued Locally' if queue_after > queue_before else 'Saved'
        last_write = 'Queued' if status == 'Queued Locally' else self._timestamp_for_status()
        self._set_database_activity_status(
            status,
            last_write=last_write,
            queue=str(queue_after),
        )
        return 'saved'

    @staticmethod
    def _format_optional_pressure(value: Optional[float]) -> str:
        """Format a pressure value for logs, preserving explicit null failures."""
        return 'NULL' if value is None else f'{value:.4f}'

    def _map_result_to_database_fields(
        self,
        activation_value: Optional[float],
        deactivation_value: Optional[float],
    ) -> tuple[Optional[float], Optional[float]]:
        """Map semantic edges to the legacy direction-named DB columns."""
        setup = self._current_test_setup
        direction = (getattr(setup, 'activation_direction', '') or '').strip().lower()
        if direction.startswith('decreas'):
            return deactivation_value, activation_value
        return activation_value, deactivation_value

    def _advance_serial(self, port_id: str) -> None:
        """Advance to the next serial number after recording a result."""
        sm = self._state_machines.get(port_id)
        if sm:
            sm.reset_for_new_unit()

        wo = self._ui_bridge._current_work_order if self._ui_bridge else None
        if not wo:
            return

        test_mode = wo.get('test_mode', False)
        current = self._ui_bridge._port_serials.get(port_id, 1)
        self._ui_bridge.allocate_serial(
            port_id,
            self._next_available_serial_for_port(port_id, current + 1, 1),
        )

        if test_mode:
            return

        # Update progress
        progress = get_work_order_progress(
            wo.get('shop_order', ''),
            wo.get('part_id', ''),
            wo.get('sequence_id', ''),
        )
        completed, total = self._normalize_progress_counts(
            wo.get('total', 0),
            progress.get('completed', 0),
            context=f'Progress refresh for {wo.get("shop_order", "unknown work order")}',
        )
        wo['total'] = total
        wo['completed'] = completed
        self._ui_bridge.update_progress(
            completed,
            total,
            progress.get('passed', 0),
            progress.get('failed', 0),
        )

    def cleanup(self) -> None:
        """Clean up hardware resources."""
        self._reset_precision_coordination()
        # Cancel any running test executors
        for executor in self._test_executors.values():
            if executor.is_running:
                executor.request_cancel()
        if hasattr(self, '_readings_timer'):
            self._readings_timer.stop()
        if hasattr(self, '_status_timer'):
            self._status_timer.stop()
        if hasattr(self, '_db_status_timer'):
            self._db_status_timer.stop()
        if self._port_manager:
            self._port_manager.stop_polling()
            self._port_manager.disconnect_all()

    def _handle_debug_action(self, port_id: str, action: str, payload: Dict[str, Any]) -> None:
        """Handle debug actions from the debug panel."""
        try:
            self._debug_actions.handle(port_id, action, payload)
        except Exception as e:
            logger.error(f"Error handling debug action {action} for {port_id}: {e}", exc_info=True)
            self._ui_bridge.show_error_message("Debug action failed", str(e))

    def _handle_admin_action(self, action: str, payload: Dict[str, Any]) -> None:
        """Handle admin actions from the admin panel."""
        try:
            self._admin_actions.handle(action, payload)
        except Exception as exc:
            logger.error("Error handling admin action %s: %s", action, exc, exc_info=True)
            self._ui_bridge.show_error_message("Admin action failed", str(exc))

    def _get_ui_pressure_unit(self) -> str:
        return self._ui_bridge.get_pressure_unit() if self._ui_bridge else 'PSI'

    def _set_debug_alicat_mode(self, port_id: str, mode: str) -> None:
        self._debug_alicat_mode[port_id] = str(mode or 'pressurize').strip().lower()

    def _convert_debug_display_to_abs_psi(self, port_id: str, value_psi: float, units_label: str) -> float:
        display_reference = self._resolve_display_reference(units_label)
        return self._to_absolute_pressure(port_id, value_psi, display_reference)

    def _resolve_alicat_command_reference(self, port_id: str) -> tuple[float, str]:
        barometric_psi = self._get_barometric_pressure(port_id)
        alicat_reference = self._infer_alicat_setpoint_reference(port_id, barometric_psi)
        return barometric_psi, alicat_reference

    def _reconnect_hardware(self) -> None:
        self._reset_precision_coordination()
        self._port_manager.stop_polling()
        self._port_manager.disconnect_all()
        connected = self._port_manager.connect_all()
        self._port_manager.start_polling()
        self._port_manager.set_alicat_poll_profile(None)
        self._refresh_hardware_status()
        level = self._ui_bridge.show_info_message if connected else self._ui_bridge.show_error_message
        level('Admin', 'Hardware reconnect completed.' if connected else 'Hardware reconnect completed with failures.')

    def _reconnect_database(self) -> None:
        close_database()
        db_cfg = self._config.get('database', {})
        connected = initialize_database(db_cfg if isinstance(db_cfg, dict) else {})
        self._refresh_database_status()
        if connected:
            self._sync_local_cache()
        level = self._ui_bridge.show_info_message if connected else self._ui_bridge.show_error_message
        level('Admin', 'Database reconnect successful.' if connected else 'Database reconnect failed.')

    def _log_dir(self) -> Path:
        log_cfg = self._config.get('logging', {})
        log_dir = Path(log_cfg.get('log_dir', 'logs'))
        if not log_dir.is_absolute():
            project_root = Path(__file__).resolve().parents[2]
            log_dir = project_root / log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir

    def _open_logs(self) -> None:
        log_dir = self._log_dir()
        try:
            os.startfile(str(log_dir))  # type: ignore[attr-defined]
        except Exception:
            logger.info('Log directory: %s', log_dir)
        self._ui_bridge.show_info_message('Admin', f'Log directory: {log_dir}')

    def _export_logs(self) -> None:
        log_dir = self._log_dir()
        archive_base = log_dir / f'stinger_logs_{time.strftime("%Y%m%d_%H%M%S")}'
        archive_path = shutil.make_archive(str(archive_base), 'zip', root_dir=str(log_dir))
        self._ui_bridge.show_info_message('Admin', f'Logs exported to {archive_path}')

    def _export_history(self) -> None:
        history = {
            'timestamp': time.time(),
            'last_barometric_psi': self._last_barometric_psi,
            'cycle_estimates_abs_psi': self._cycle_estimates_abs_psi,
        }
        out_path = self._log_dir() / f'run_history_{time.strftime("%Y%m%d_%H%M%S")}.yaml'
        import yaml
        out_path.write_text(yaml.safe_dump(history, sort_keys=False), encoding='utf-8')
        self._ui_bridge.show_info_message('Admin', f'History exported to {out_path}')

    def _safety_override(self, payload: Dict[str, Any]) -> None:
        enabled = bool(payload.get('enabled', False))
        logger.warning('Safety override requested: enabled=%s payload=%s', enabled, payload)
        self._ui_bridge.show_info_message(
            'Admin',
            'Safety override is logged only; no runtime bypass has been enabled.',
        )

    def _set_main_measurement_source(self, payload: Dict[str, Any]) -> None:
        requested = str(payload.get("preferred_source", "transducer") or "transducer").strip().lower()
        if requested not in {"transducer", "alicat", "auto"}:
            raise ValueError(f"Unsupported measurement source '{requested}'")

        hardware_cfg = self._config.setdefault("hardware", {})
        if not isinstance(hardware_cfg, dict):
            raise ValueError('Config section "hardware" must be a mapping')
        measurement_cfg = hardware_cfg.setdefault("measurement", {})
        if not isinstance(measurement_cfg, dict):
            raise ValueError('Config section "hardware.measurement" must be a mapping')

        measurement_cfg["preferred_source"] = requested
        measurement_cfg["fallback_on_unavailable"] = bool(
            measurement_cfg.get("fallback_on_unavailable", True)
        )

        save_path = save_config(self._config)
        settings = get_measurement_settings(self._config)
        logger.info(
            "Main measurement source updated to %s (fallback=%s); saved to %s",
            settings.preferred_source,
            settings.fallback_on_unavailable,
            save_path,
        )
        self._ui_bridge.show_info_message(
            "Admin",
            f"Main measurement source set to {settings.preferred_source}.",
        )


def _get_default_test_ptp() -> Dict[str, str]:
    """Generate default PTP parameters for test mode."""
    return {
        "ActivationTarget": "25.0",
        "IncreasingLowerLimit": "24.0",
        "IncreasingUpperLimit": "26.0",
        "DecreasingLowerLimit": "23.0",
        "DecreasingUpperLimit": "25.0",
        "ResetBandLowerLimit": "22.0",
        "ResetBandUpperLimit": "27.0",
        "TargetActivationDirection": "Increasing",
        "UnitsOfMeasure": "1",  # PSI
        "PressureReference": "Absolute",
        "CommonTerminal": "1",
        "NormallyOpenTerminal": "2",
        "NormallyClosedTerminal": "3",
    }


def _workflow_for_sequence(sequence_id: str) -> str:
    try:
        return "QAL15" if int(sequence_id.strip()) == 300 else "QAL16"
    except (TypeError, ValueError):
        return "QAL16"
