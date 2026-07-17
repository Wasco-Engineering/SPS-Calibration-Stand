"""
UI Bridge - Mediator between core logic and PyQt UI.

Follows the pattern from Functional Stand to decouple business logic
from UI implementation details.
"""

import logging
import threading
import time
from typing import Any, Dict, Optional, Set

from PyQt6.QtCore import QObject, pyqtSignal

from ..hardware.port import PortReading
from .measurement_source import (
    get_measurement_settings,
    select_ui_pressure_abs_psi,
    _transducer_pressure_abs_psi,
)
from .pressure_domain import (
    infer_barometric_pressure,
    infer_setpoint_abs_psi,
    is_gauge_unit_label,
    is_plausible_barometric_psi,
    resolve_barometric_psi,
    to_absolute_pressure,
    to_display_pressure,
)
from .ptp_service import convert_pressure

logger = logging.getLogger(__name__)


class UIBridge(QObject):
    """
    Mediator between core application logic and the UI.
    
    All UI updates go through signals from this class.
    All user actions are received as signals to this class.
    """
    
    # -------------------------------------------------------------------------
    # Signals TO the UI
    # -------------------------------------------------------------------------
    
    # Work order / session
    work_order_changed = pyqtSignal(dict)
    work_order_progress_updated = pyqtSignal(dict)
    database_status_updated = pyqtSignal(dict)
    ptp_updated = pyqtSignal(dict)
    
    # Hardware status
    hardware_status_updated = pyqtSignal(dict)
    pressure_updated = pyqtSignal(str, float, str)  # (port_id, pressure, unit)
    switch_state_updated = pyqtSignal(str, bool, bool)  # (port_id, no_active, nc_active)
    pressure_viz_updated = pyqtSignal(str, dict)  # (port_id, viz_data)
    barometric_pressure_updated = pyqtSignal(str, float)  # (port_id, barometric_pressure)
    debug_chart_updated = pyqtSignal(str, float, object, object, object)  # (port_id, timestamp, transducer, setpoint, alicat)
    debug_dio_updated = pyqtSignal(str, dict)  # (port_id, dio_values)
    
    # State machine
    state_changed = pyqtSignal(str, str, dict)  # (port_id, state, data)
    substate_changed = pyqtSignal(str, str, dict)  # (port_id, substate, data)
    button_state_changed = pyqtSignal(str, dict)  # (port_id, {primary, cancel})
    
    # Test results
    test_result_ready = pyqtSignal(str, dict)  # (port_id, result_data)
    
    # Serial numbers
    serial_updated = pyqtSignal(str, int)  # (port_id, serial)
    
    # Messages / dialogs
    show_error = pyqtSignal(str, str)  # (title, message)
    show_info = pyqtSignal(str, str)  # (title, message)
    request_operator_prompt = pyqtSignal(str, str, list)  # (title, message, buttons)
    
    # -------------------------------------------------------------------------
    # Signals FROM the UI (user actions)
    # -------------------------------------------------------------------------
    
    # Work order
    login_requested = pyqtSignal(dict)  # payload from login dialog
    logout_requested = pyqtSignal()
    
    # Test control
    start_pressurize_requested = pyqtSignal(str)  # (port_id)
    start_test_requested = pyqtSignal(str)  # (port_id)
    cancel_requested = pyqtSignal(str)  # (port_id)
    vent_requested = pyqtSignal(str)  # (port_id)
    
    # Results
    record_success_requested = pyqtSignal(str)  # (port_id)
    record_failure_requested = pyqtSignal(str)  # (port_id)
    retest_requested = pyqtSignal(str)  # (port_id)
    
    # Serial number
    serial_increment_requested = pyqtSignal(str)  # (port_id)
    serial_decrement_requested = pyqtSignal(str)  # (port_id)
    serial_manual_entry_requested = pyqtSignal(str, int)  # (port_id, serial)
    
    # Program control
    close_program_requested = pyqtSignal()

    # Debug/Admin actions
    debug_action_requested = pyqtSignal(str, str, dict)  # (port_id, action, payload)
    admin_action_requested = pyqtSignal(str, dict)  # (action, payload)

    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize UI bridge.
        
        Args:
            config: Application configuration.
        """
        super().__init__()
        self.config = config
        
        # State tracking
        self._current_work_order: Optional[Dict] = None
        self._port_serials: Dict[str, int] = {}
        self._in_progress_serials: Set[int] = set()
        self._serial_lock = threading.Lock()
        self._pressure_unit = "PSIA"
        self._display_reference: Optional[str] = None
        self._last_pressure_abs_psi: Dict[str, float] = {
            "port_a": 0.0,
            "port_b": 0.0,
        }
        self._last_barometric_psi: Dict[str, float] = {
            "port_a": 14.7,
            "port_b": 14.7,
        }
        self._last_debug_readings: Dict[str, Dict[str, Optional[float]]] = {
            "port_a": {
                "timestamp": None,
                "pressure_abs_psi": None,
                "setpoint_abs_psi": None,
                "alicat_abs_psi": None,
                "barometric_psi": 14.7,
            },
            "port_b": {
                "timestamp": None,
                "pressure_abs_psi": None,
                "setpoint_abs_psi": None,
                "alicat_abs_psi": None,
                "barometric_psi": 14.7,
            },
        }
        self._last_debug_chart_emit_s: Dict[str, float] = {
            "port_a": 0.0,
            "port_b": 0.0,
        }
        self._debug_chart_interval_s = 0.10
        
        logger.info("UIBridge initialized")

    @staticmethod
    def _is_gauge_unit(unit_label: Optional[str]) -> bool:
        return is_gauge_unit_label(unit_label)

    def _to_display_pressure(
        self,
        value_abs_psi: Optional[float],
        unit_label: str,
        barometric_psi: float,
    ) -> Optional[float]:
        return to_display_pressure(
            value_abs_psi, unit_label, barometric_psi,
            pressure_reference=self._display_reference,
        )

    def _to_absolute_pressure(self, value_psi: float, source_reference: Optional[str], barometric_psi: float) -> float:
        return to_absolute_pressure(value_psi, source_reference, barometric_psi)

    def _infer_setpoint_abs_psi(
        self,
        setpoint: Optional[float],
        absolute_alicat: Optional[float],
        gauge_pressure: Optional[float],
        barometric_psi: float,
    ) -> Optional[float]:
        return infer_setpoint_abs_psi(
            setpoint=setpoint,
            absolute_alicat=absolute_alicat,
            gauge_pressure=gauge_pressure,
            barometric_psi=barometric_psi,
        )

    def _infer_barometric_pressure(self, reading: Optional[PortReading]) -> Optional[float]:
        return infer_barometric_pressure(reading)
    
    # -------------------------------------------------------------------------
    # Work order management
    # -------------------------------------------------------------------------
    
    def set_work_order(self, wo_data: Dict[str, Any]) -> None:
        """Update the current work order context."""
        self._current_work_order = wo_data
        self.work_order_changed.emit(wo_data)
    
    def update_progress(self, completed: int, total: int, passed: int, failed: int) -> None:
        """Update work order progress display."""
        self.work_order_progress_updated.emit({
            'completed': completed,
            'total': total,
            'passed': passed,
            'failed': failed,
        })
    
    # -------------------------------------------------------------------------
    # Hardware updates
    # -------------------------------------------------------------------------
    
    def update_pressure(self, port_id: str, reading: PortReading) -> None:
        """Update pressure display for a port."""
        baro_value = resolve_barometric_psi(
            reading,
            last_value=self._last_barometric_psi.get(port_id),
        )
        self._last_barometric_psi[port_id] = baro_value

        measurement_settings = get_measurement_settings(self.config)
        ui_pressure_abs_psi, _ui_source = select_ui_pressure_abs_psi(
            reading=reading,
            settings=measurement_settings,
            barometric_psi=baro_value,
        )
        if ui_pressure_abs_psi is not None:
            self._last_pressure_abs_psi[port_id] = ui_pressure_abs_psi

        display_pressure = self._to_display_pressure(
            ui_pressure_abs_psi,
            self._pressure_unit,
            baro_value,
        )
        if display_pressure is not None:
            self.pressure_updated.emit(
                port_id,
                display_pressure,
                self._pressure_unit,
            )

        # Emit chart data (for debug panel)
        setpoint = reading.alicat.setpoint if reading.alicat else None
        absolute_alicat = reading.alicat.pressure if reading.alicat else None
        gauge_pressure = reading.alicat.gauge_pressure if reading.alicat else None
        setpoint_abs_psi = self._infer_setpoint_abs_psi(
            setpoint,
            absolute_alicat,
            gauge_pressure,
            baro_value,
        )
        alicat_abs_psi = absolute_alicat
        transducer_abs_psi = _transducer_pressure_abs_psi(reading, baro_value)

        self._last_debug_readings[port_id] = {
            "timestamp": reading.timestamp,
            "pressure_abs_psi": ui_pressure_abs_psi,
            "setpoint_abs_psi": setpoint_abs_psi,
            "alicat_abs_psi": alicat_abs_psi,
            "transducer_abs_psi": transducer_abs_psi,
            "barometric_psi": baro_value,
        }

        now_s = time.monotonic()
        last_emit_s = self._last_debug_chart_emit_s.get(port_id, 0.0)
        if now_s - last_emit_s >= self._debug_chart_interval_s:
            self._last_debug_chart_emit_s[port_id] = now_s
            display_transducer = self._to_display_pressure(
                transducer_abs_psi,
                self._pressure_unit,
                baro_value,
            )
            display_setpoint = self._to_display_pressure(setpoint_abs_psi, self._pressure_unit, baro_value)
            display_alicat = self._to_display_pressure(alicat_abs_psi, self._pressure_unit, baro_value)
            self.debug_chart_updated.emit(
                port_id,
                reading.timestamp,
                display_transducer,
                display_setpoint,
                display_alicat,
            )
        
        if reading.switch:
            self.switch_state_updated.emit(
                port_id,
                reading.switch.no_active,
                reading.switch.nc_active
            )
        
        barometric = self._infer_barometric_pressure(reading)
        if barometric is not None and is_plausible_barometric_psi(barometric):
            self._last_barometric_psi[port_id] = float(barometric)
            self.barometric_pressure_updated.emit(port_id, float(barometric))

    def update_debug_dio(self, port_id: str, dio_values: Dict[int, int]) -> None:
        """Update debug DIO readouts for a port."""
        self.debug_dio_updated.emit(port_id, dio_values)

    def set_pressure(self, port_id: str, pressure: float, unit: str = "PSI") -> None:
        """Directly set pressure display for a port."""
        unit_label = unit or "PSI"
        pressure_psi = convert_pressure(pressure, unit_label, "PSI")
        baro_value = self._last_barometric_psi.get(port_id, 14.7)
        pressure_abs_psi = pressure_psi + baro_value if self._is_gauge_unit(unit_label) else pressure_psi

        self._last_pressure_abs_psi[port_id] = pressure_abs_psi
        display_pressure = self._to_display_pressure(pressure_abs_psi, self._pressure_unit, baro_value)
        self.pressure_updated.emit(port_id, display_pressure if display_pressure is not None else 0.0, self._pressure_unit)

    def set_pressure_unit(self, unit: str) -> None:
        """Set the display units for pressure readouts."""
        old_unit = self._pressure_unit
        self._pressure_unit = unit or "PSI"
        
        if old_unit == self._pressure_unit:
            return  # No change needed
        
        for port_id, pressure_abs_psi in self._last_pressure_abs_psi.items():
            baro_value = self._last_barometric_psi.get(port_id, 14.7)
            display_pressure = self._to_display_pressure(pressure_abs_psi, self._pressure_unit, baro_value)
            self.pressure_updated.emit(port_id, display_pressure if display_pressure is not None else 0.0, self._pressure_unit)
        for port_id, data in self._last_debug_readings.items():
            timestamp = data.get("timestamp")
            if timestamp is None:
                continue
            pressure_abs_psi = data.get("pressure_abs_psi")
            setpoint_abs_psi = data.get("setpoint_abs_psi")
            alicat_abs_psi = data.get("alicat_abs_psi")
            baro_value = data.get("barometric_psi") or self._last_barometric_psi.get(port_id, 14.7)
            display_pressure = self._to_display_pressure(pressure_abs_psi, self._pressure_unit, baro_value)
            display_setpoint = self._to_display_pressure(setpoint_abs_psi, self._pressure_unit, baro_value)
            display_alicat = self._to_display_pressure(alicat_abs_psi, self._pressure_unit, baro_value)
            self.debug_chart_updated.emit(
                port_id,
                timestamp,
                display_pressure,
                display_setpoint,
                display_alicat,
            )

    def get_pressure_unit(self) -> str:
        """Get the current display units for pressure readouts."""
        return self._pressure_unit

    def set_display_reference(self, reference: Optional[str]) -> None:
        """Set the pressure reference frame for display conversions."""
        self._display_reference = reference

    def get_display_reference(self) -> Optional[str]:
        """Get the current pressure reference frame for display conversions."""
        return self._display_reference

    def set_switch_state(self, port_id: str, no_active: bool, nc_active: bool) -> None:
        """Directly set switch state for a port."""
        self.switch_state_updated.emit(port_id, no_active, nc_active)

    def update_pressure_viz(self, port_id: str, viz_data: Dict[str, Any]) -> None:
        """Update pressure visualization settings for a port."""
        self.pressure_viz_updated.emit(port_id, viz_data)
    
    def update_hardware_status(self, status: Dict[str, Any]) -> None:
        """Update hardware status display."""
        self.hardware_status_updated.emit(status)

    def update_database_status(self, status: str, last_write: str = "--", queue: str = "0") -> None:
        """Update database status display."""
        self.database_status_updated.emit({
            "status": status,
            "last_write": last_write,
            "queue": queue,
        })

    def update_ptp_details(self, details: Dict[str, Any]) -> None:
        """Update current PTP details display."""
        self.ptp_updated.emit(details)
    
    # -------------------------------------------------------------------------
    # Serial number management (thread-safe)
    # -------------------------------------------------------------------------
    
    def allocate_serial(self, port_id: str, serial: int) -> None:
        """Allocate a serial number to a port."""
        with self._serial_lock:
            # Clear any previous serial for this port
            old_serial = self._port_serials.get(port_id)
            if old_serial is not None:
                self._in_progress_serials.discard(old_serial)
            
            # Set new serial
            self._port_serials[port_id] = serial
            self._in_progress_serials.add(serial)
        
        self.serial_updated.emit(port_id, serial)
    
    def release_serial(self, port_id: str) -> None:
        """Release the serial number for a port."""
        with self._serial_lock:
            serial = self._port_serials.pop(port_id, None)
            if serial is not None:
                self._in_progress_serials.discard(serial)
    
    def get_in_progress_serials(self) -> Set[int]:
        """Get the set of serials currently being tested."""
        with self._serial_lock:
            return self._in_progress_serials.copy()
    
    # -------------------------------------------------------------------------
    # State machine updates
    # -------------------------------------------------------------------------
    
    def update_state(self, port_id: str, state: str, data: Dict) -> None:
        """Forward state change to UI."""
        self.state_changed.emit(port_id, state, data)
    
    def update_substate(self, port_id: str, substate: str, data: Dict) -> None:
        """Forward substate change to UI."""
        self.substate_changed.emit(port_id, substate, data)
    
    def update_buttons(self, port_id: str, buttons: Dict) -> None:
        """Update button states for a port."""
        self.button_state_changed.emit(port_id, buttons)
    
    # -------------------------------------------------------------------------
    # Messages
    # -------------------------------------------------------------------------
    
    def show_error_message(self, title: str, message: str) -> None:
        """Show an error dialog."""
        self.show_error.emit(title, message)
    
    def show_info_message(self, title: str, message: str) -> None:
        """Show an info dialog."""
        self.show_info.emit(title, message)

    # -------------------------------------------------------------------------
    # Debug/Admin actions
    # -------------------------------------------------------------------------

    def request_debug_action(self, port_id: str, action: str, payload: Dict[str, Any]) -> None:
        """Forward debug actions from the UI."""
        self.debug_action_requested.emit(port_id, action, payload)

    def request_admin_action(self, action: str, payload: Dict[str, Any]) -> None:
        """Forward admin actions from the UI."""
        self.admin_action_requested.emit(action, payload)
