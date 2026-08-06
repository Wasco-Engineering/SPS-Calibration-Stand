"""
Alicat Pressure Controller Interface.

Handles serial communication with Alicat pressure controllers for:
- Pressure setpoint control
- Ramp rate configuration
- Pressure reading (for control, not measurement authority)
"""

import logging
import threading
import time
from dataclasses import dataclass
from functools import wraps
from typing import Any, Dict, List, Optional

from app.services.pressure_calibration import apply_error_model

logger = logging.getLogger(__name__)

try:
    import serial
    import serial.tools.list_ports
    SERIAL_AVAILABLE = True
except ImportError:
    serial = None
    SERIAL_AVAILABLE = False
    logger.warning("pyserial not available - Alicat hardware unavailable")

# ``serial.tools.list_ports.comports()`` can stall for many seconds on Windows when USB
# devices are queried; cache briefly so repeated connects (e.g. diagnostics) stay responsive.
_list_ports_cache: Optional[tuple[float, frozenset[str]]] = None


def _serialized_operation(method: Any) -> Any:
    """Keep status parsing and multi-command configuration internally consistent."""
    @wraps(method)
    def wrapped(self: 'AlicatController', *args: Any, **kwargs: Any) -> Any:
        with self._operation_lock:
            return method(self, *args, **kwargs)

    return wrapped


def _cached_serial_device_names(ttl_s: float = 15.0) -> frozenset[str]:
    """Return COM device names, refreshing the list at most once per ``ttl_s``."""
    global _list_ports_cache
    if not SERIAL_AVAILABLE or serial is None:
        return frozenset()
    now = time.monotonic()
    if _list_ports_cache is not None and now - _list_ports_cache[0] < ttl_s:
        return _list_ports_cache[1]
    devices = frozenset(p.device for p in serial.tools.list_ports.comports())
    _list_ports_cache = (now, devices)
    return devices


@dataclass
class AlicatReading:
    """Result of an Alicat status query."""
    pressure: float
    setpoint: float
    timestamp: float
    gauge_pressure: Optional[float] = None
    barometric_pressure: Optional[float] = None
    pressure_raw: Optional[float] = None
    raw_response: Optional[str] = None
    raw_values: Optional[List[float]] = None


class AlicatController:
    """
    Controls a single Alicat pressure controller.
    
    Multiple Alicats can share the same COM port with different addresses.
    """

    _shared_serials: Dict[str, Any] = {}
    _shared_serial_lock = threading.RLock()
    _command_locks: Dict[str, threading.Lock] = {}
    _command_locks_lock = threading.Lock()
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize Alicat controller.

        Args:
            config: Alicat configuration including com_port, baudrate, address.
        """
        self.config = config

        self.com_port = config.get('com_port', 'COM3')
        self.baudrate = config.get('baudrate', 19200)
        self.timeout = config.get('timeout_s', 0.05)
        self.address = config.get('address', 'A').upper()
        self._pressure_index = self._coerce_optional_int(config.get('pressure_index'))
        self._setpoint_index = self._coerce_optional_int(config.get('setpoint_index'))
        self._gauge_index = self._coerce_optional_int(config.get('gauge_index'))
        self._barometric_index = self._coerce_optional_int(config.get('barometric_index'))
        self._auto_configure = bool(config.get('auto_configure', True))
        self._setpoint_source_mode = config.get('setpoint_source_mode', 'S')
        self._pressure_units_stat = self._coerce_optional_int(config.get('pressure_units_stat', 2))
        self._pressure_units_value = self._coerce_optional_int(config.get('pressure_units_value', 10))
        self._pressure_units_group = self._coerce_optional_int(config.get('pressure_units_group', 0)) or 0
        self._pressure_units_override = self._coerce_optional_int(config.get('pressure_units_override', 0)) or 0
        self._auto_tare_on_connect = bool(config.get('auto_tare_on_connect', False))
        self._auto_tare_max_delta = float(config.get('auto_tare_max_delta_psi', 0.5))
        self._auto_tare_delay_s = float(config.get('auto_tare_delay_s', 0.5))
        self._command_retries = max(0, int(config.get('command_retries', 2)))
        self._command_retry_delay_s = max(0.0, float(config.get('command_retry_delay_s', 0.03)))
        self._response_read_attempts = max(1, int(config.get('response_read_attempts', 3)))

        # Shared serial connection (may be set externally for multi-device COM port)
        self._serial = None
        self._owns_serial = False  # True if we created the connection
        self._lock = threading.Lock()
        self._operation_lock = threading.RLock()

        self._is_connected = False
        self._last_status = "Not Initialized"

        self._sim_pressure = 14.7
        self._sim_setpoint = 0.0
        self._logged_raw_status = False
        self._prefer_psi_commands = False

        error_model_cfg = config.get('alicat_error_model', {}) or {}
        self._error_model: Optional[Dict[str, Any]] = None
        if isinstance(error_model_cfg, dict) and error_model_cfg.get('type'):
            self._error_model = error_model_cfg

        # Track current display-unit label so readings can be normalised
        # to PSI and commands can be converted back to display units.
        self._display_units_label: str = 'PSI'
        self._update_display_units_label()

        logger.info(f"AlicatController initialized: {self.com_port} address={self.address}")
    
    # ------------------------------------------------------------------
    # Display-unit tracking & conversion
    # ------------------------------------------------------------------

    # Map from Alicat numeric unit code to the label used by
    # ptp_service.convert_pressure.  Codes not listed here fall back to 'PSI'.
    _UNIT_CODE_TO_LABEL: Dict[int, str] = {
        1: 'PSI', 10: 'PSI',    # PSI / PSIA
        12: 'mTorr', 13: 'Torr', 14: 'mmHg', 15: 'INHG',
        # 19/20/21 are water-column units on observed hardware.
        # They are intentionally not mapped to avoid incorrect conversion.
    }

    # Mapping from PTP UnitsOfMeasure codes to Alicat DCU unit codes.
    # PTP code 21 means Torr in this application, but Alicat DCU code 21
    # is inH2O@60F on observed hardware, so we translate to DCU code 13.
    _PTP_TO_ALICAT_UNIT_CODE: Dict[str, int] = {
        '1': 10,   # PSI/PSIA
        '12': 12,  # mTorr
        '13': 13,  # Torr
        '14': 14,  # mmHg
        '15': 15,  # inHg
        '19': 14,  # mmHg @ 0C -> mmHgA @ 0C
        '21': 13,  # Torr (PTP) -> torrA (Alicat)
    }

    def _update_display_units_label(self) -> None:
        """Refresh ``_display_units_label`` from ``_pressure_units_value``."""
        code = self._pressure_units_value or 10
        self._display_units_label = self._UNIT_CODE_TO_LABEL.get(code, 'PSI')

    def _display_to_psi(self, value: float) -> float:
        """Convert a value from the Alicat's current display units to PSI."""
        if self._display_units_label in ('PSI', 'PSIA'):
            return value
        from app.services.ptp_service import convert_pressure
        return convert_pressure(value, self._display_units_label, 'PSI')

    def _psi_to_display(self, value: float) -> float:
        """Convert a PSI value to the Alicat's current display units."""
        if self._display_units_label in ('PSI', 'PSIA'):
            return value
        from app.services.ptp_service import convert_pressure
        return convert_pressure(value, 'PSI', self._display_units_label)

    def _display_units_are_psi(self) -> bool:
        return self._display_units_label in ('PSI', 'PSIA')

    def set_shared_serial(self, serial_conn: Any) -> None:
        """
        Set a shared serial connection (for multiple Alicats on same COM port).

        Args:
            serial_conn: An existing serial.Serial connection.
        """
        self._serial = serial_conn
        self._owns_serial = False
        self._is_connected = True
        self._post_connect_config()

    
    def connect(self, max_retries: int = 3) -> bool:
        """
        Establish serial connection to the Alicat.
        
        Args:
            max_retries: Number of connection attempts.
            
        Returns:
            True if connected successfully.
        """
        if not SERIAL_AVAILABLE:
            self._is_connected = True
            self._last_status = "Connected (no hardware)"
            return True

        if self._serial and self._serial.is_open:
            self._is_connected = True
            return True

        if not self.com_port:
            self._last_status = "Connection Failed: Missing COM port"
            return False

        with self._shared_serial_lock:
            shared = self._shared_serials.get(self.com_port)
            if shared is not None and getattr(shared, "is_open", False):
                self._serial = shared
                self._owns_serial = False
                if self._verify_connection():
                    self._is_connected = True
                    self._last_status = "Connected (Shared)"
                    self._post_connect_config()
                    logger.info(f"Alicat {self.address}: Connected on {self.com_port} (shared)")
                    return True
                self._last_status = f"Address verify failed on {self.com_port}"
                return False

        for attempt in range(max_retries):
            try:
                if not SERIAL_AVAILABLE or serial is None:
                    raise RuntimeError('pyserial unavailable')
                
                # Check if port is available before attempting to open
                available_ports = _cached_serial_device_names()
                if self.com_port not in available_ports:
                    raise RuntimeError(f"Port {self.com_port} not found in available ports")
                
                self._serial = serial.Serial(
                    port=self.com_port,
                    baudrate=self.baudrate,
                    timeout=self.timeout,
                    write_timeout=self.timeout
                )
                self._owns_serial = True
                with self._shared_serial_lock:
                    self._shared_serials[self.com_port] = self._serial
                
                # Verify communication
                if self._verify_connection():
                    self._is_connected = True
                    self._last_status = "Connected"
                    self._post_connect_config()
                    logger.info(f"Alicat {self.address}: Connected on {self.com_port}")
                    return True

                if self._serial:
                    self._serial.close()
                self._last_status = f"Address verify failed on {self.com_port}"

            except PermissionError as e:
                error_msg = (
                    f"Permission denied accessing {self.com_port}. "
                    "Port may be in use by another application or require administrator privileges. "
                    f"Error: {e}"
                )
                logger.error(f"Alicat {self.address}: {error_msg}")
                self._last_status = f"Permission denied: {self.com_port}"
                if self._serial:
                    try:
                        self._serial.close()
                    except Exception:
                        pass
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"Alicat {self.address}: Connection attempt {attempt+1} failed: {e}")
                if self._serial:
                    try:
                        self._serial.close()
                    except Exception:
                        pass
                time.sleep(0.5)
        
        self._last_status = "Connection Failed"
        return False

    def _post_connect_config(self) -> None:
        """Apply post-connect configuration for setpoint and units."""
        if not SERIAL_AVAILABLE or not self._auto_configure:
            return
        self._configure_setpoint_source()
        if self._pressure_units_value is not None:
            self._ensure_pressure_units_value(int(self._pressure_units_value))
        self._maybe_auto_tare()

    def _maybe_auto_tare(self) -> None:
        if not self._auto_tare_on_connect:
            return
        if self._auto_tare_delay_s > 0:
            time.sleep(self._auto_tare_delay_s)
        status = self.read_status()
        if not status or status.barometric_pressure is None:
            logger.warning("Alicat %s: Auto-tare skipped (no barometric pressure)", self.address)
            return
        delta = abs(status.pressure - status.barometric_pressure)
        if delta > self._auto_tare_max_delta:
            logger.info(
                "Alicat %s: Auto-tare skipped (not at atmosphere: P=%.2f, baro=%.2f, delta=%.2f)",
                self.address,
                status.pressure,
                status.barometric_pressure,
                delta,
            )
            return
        # Do not call exhaust() here. On Stinger stands EXH vents through the
        # atmosphere solenoid route and pulls open DUT lines toward vacuum.
        if self.tare():
            logger.info("Alicat %s: Auto-tare complete (at atmosphere)", self.address)
        else:
            logger.warning("Alicat %s: Auto-tare failed", self.address)

    def _configure_setpoint_source(self) -> None:
        if not self._setpoint_source_mode:
            return
        mode = str(self._setpoint_source_mode).strip().upper()
        if not mode:
            return
        response = self._send_command("LSS")
        current = None
        if response:
            parts = response.split()
            if len(parts) >= 2:
                current = parts[-1].upper()
        if current == mode:
            return
        response = self._send_command(f"LSS {mode}")
        if response and response.startswith(self.address):
            logger.info("Alicat %s: Setpoint source set to %s", self.address, mode)
        else:
            logger.warning("Alicat %s: Failed to set setpoint source (%s)", self.address, mode)

    def _configure_pressure_units(self) -> bool:
        if self._pressure_units_stat is None or self._pressure_units_value is None:
            return False
        stat = self._pressure_units_stat
        unit_value = self._pressure_units_value
        group = self._pressure_units_group
        override = self._pressure_units_override

        # First check current units.
        response = self._send_command(f"DCU {stat}")
        if response:
            parts = response.split()
            if len(parts) >= 2 and parts[1].isdigit() and int(parts[1]) == unit_value:
                return True

        # Preferred modern form: DCU <stat> <unit>
        response = self._send_command(f'DCU {stat} {unit_value}')
        if response and response.startswith(self.address):
            logger.info(
                'Alicat %s: Units set command sent for stat=%s -> %s',
                self.address,
                stat,
                unit_value,
            )
            return True

        # Legacy fallback form.
        response = self._send_command(f'DCU {stat} {group} {unit_value} {override}')
        if response and response.startswith(self.address):
            logger.info(
                'Alicat %s: Units set command sent via legacy form stat=%s -> %s (group=%s override=%s)',
                self.address,
                stat,
                unit_value,
                group,
                override,
            )
            return True

        logger.warning(
            'Alicat %s: Failed to set units for stat=%s (unit=%s; legacy group=%s override=%s)',
            self.address,
            stat,
            unit_value,
            group,
            override,
        )
        return False

    def _read_pressure_units_code(self) -> Optional[int]:
        """Read currently active Alicat pressure unit code for configured stat."""
        if self._pressure_units_stat is None:
            return None
        response = self._send_command(f'DCU {self._pressure_units_stat}')
        if not response:
            return None
        for token in response.split():
            cleaned = token.strip()
            if not cleaned:
                continue
            if cleaned.upper() == self.address:
                continue
            if cleaned.lstrip('+-').isdigit():
                return int(cleaned)
        return None

    def _ensure_pressure_units_value(self, unit_value: int) -> bool:
        """Set and verify Alicat pressure units, updating label to actual units."""
        self._pressure_units_value = unit_value
        observed_code: Optional[int] = None

        for attempt in range(2):
            command_ok = self._configure_pressure_units()
            if not command_ok:
                continue
            for _ in range(3):
                code = self._read_pressure_units_code()
                if code is None:
                    time.sleep(0.03)
                    continue
                observed_code = code
                if code == unit_value:
                    self._pressure_units_value = unit_value
                    self._update_display_units_label()
                    if not self._display_units_are_psi():
                        self._prefer_psi_commands = False
                    logger.info(
                        'Alicat %s: Units verified stat=%s code=%s label=%s',
                        self.address,
                        self._pressure_units_stat,
                        code,
                        self._display_units_label,
                    )
                    return True
                break
            if attempt == 0:
                logger.warning(
                    'Alicat %s: Units verify mismatch (wanted=%s observed=%s), retrying',
                    self.address,
                    unit_value,
                    observed_code,
                )

        if observed_code is not None:
            self._pressure_units_value = observed_code
            self._update_display_units_label()
        logger.error(
            'Alicat %s: Units verify failed (wanted=%s observed=%s label=%s)',
            self.address,
            unit_value,
            observed_code,
            self._display_units_label,
        )
        return False

    @_serialized_operation
    def configure_units_from_ptp(self, units_code: str) -> bool:
        """Keep absolute controllers in TorrA; use mmHg for explicit mmHg PTPs."""
        normalized_code = str(units_code).strip()
        if normalized_code not in self._PTP_TO_ALICAT_UNIT_CODE:
            logger.warning("Alicat %s: Invalid PTP units code: %s", self.address, units_code)
            return False
        unit_value = 14 if normalized_code in {'14', '19'} else 13

        self._pressure_units_value = unit_value
        if not self._is_connected:
            # Alicat not yet connected — store the value so _post_connect_config
            # will apply it.  Update label now because no polling is happening
            # yet to be confused by the mismatch.
            self._update_display_units_label()
            if not self._display_units_are_psi():
                self._prefer_psi_commands = False
            logger.info("Alicat %s: Units set pending connect (%s)", self.address, unit_value)
            return True

        return self._ensure_pressure_units_value(unit_value)

    def configure_units_from_ptp_prefer_psi(self, units_code: str) -> bool:
        """Deprecated compatibility alias for the native-unit configuration."""
        return self.configure_units_from_ptp(units_code)
    
    def _verify_connection(self) -> bool:
        """Verify communication with the Alicat."""
        response = self._send_command("")
        return response is not None

    def _is_ack(self, response: Optional[str]) -> bool:
        """Return True if response looks like a valid ack/data line."""
        if not response:
            return False
        trimmed = response.strip()
        if not trimmed:
            return False
        if trimmed.startswith(self.address):
            return True
        first = trimmed[0]
        return first.isdigit() or first in "+-"
    
    def _send_command(self, command: str) -> Optional[str]:
        """
        Send a command to the Alicat and return the response.
        
        Args:
            command: Command string (without address prefix).
            
        Returns:
            Response string, or None on error.
        """
        if not SERIAL_AVAILABLE:
            return self._simulate_command(command)

        if not self._serial or not self._serial.is_open:
            return None

        command_lock = None
        with self._shared_serial_lock:
            shared = self._shared_serials.get(self.com_port)
            if shared is self._serial:
                with self._command_locks_lock:
                    command_lock = self._command_locks.setdefault(self.com_port, threading.Lock())

        lock_context = command_lock or self._lock
        with lock_context:
            full_command = f"{self.address}{command}\r"
            last_error: Optional[Exception] = None

            for attempt in range(self._command_retries + 1):
                try:
                    if self._serial.in_waiting > 0:
                        self._serial.reset_input_buffer()

                    self._serial.write(full_command.encode())

                    for _ in range(self._response_read_attempts):
                        raw_response = self._serial.read_until(b'\r')
                        if not raw_response:
                            continue
                        decoded = raw_response.decode(errors='ignore').strip()
                        if not decoded:
                            continue
                        response = ''.join(ch for ch in decoded if ch.isprintable())
                        if response:
                            return response

                except Exception as e:
                    last_error = e
                    is_write_timeout = False
                    if serial is not None:
                        timeout_exc = getattr(serial, 'SerialTimeoutException', None)
                        if timeout_exc is not None and isinstance(e, timeout_exc):
                            is_write_timeout = True
                    if attempt < self._command_retries:
                        retry_log = logger.debug if attempt == 0 else logger.warning
                        if is_write_timeout:
                            retry_log(
                                'Alicat %s: Write timeout for "%s", retry %d/%d',
                                self.address,
                                command,
                                attempt + 1,
                                self._command_retries,
                            )
                        else:
                            retry_log(
                                'Alicat %s: Command "%s" error (%s), retry %d/%d',
                                self.address,
                                command,
                                e,
                                attempt + 1,
                                self._command_retries,
                            )
                        time.sleep(self._command_retry_delay_s)
                        continue

                    if is_write_timeout:
                        self._last_status = 'Command error: Write timeout'
                    else:
                        self._last_status = f'Command error: {e}'
                    logger.error(f'Alicat {self.address}: {self._last_status}')
                    return None

                if attempt < self._command_retries:
                    retry_log = logger.debug if attempt == 0 else logger.warning
                    retry_log(
                        'Alicat %s: Empty/invalid response for "%s", retry %d/%d',
                        self.address,
                        command,
                        attempt + 1,
                        self._command_retries,
                    )
                    time.sleep(self._command_retry_delay_s)
                    continue

            if last_error is not None:
                self._last_status = f'Command error: {last_error}'
                logger.error(f'Alicat {self.address}: {self._last_status}')
            return None
    
    def _simulate_command(self, command: str) -> str:
        """Simulate Alicat command responses.

        Internal sim variables (``_sim_pressure``, ``_sim_setpoint``) are
        stored in PSI.  Responses are returned in the controller's current
        display units so that ``read_status`` can normalise them uniformly.
        """
        if command == "":
            # Status query — return values in display units
            p = self._psi_to_display(self._sim_pressure)
            s = self._psi_to_display(self._sim_setpoint)
            return f"{self.address} {p:.2f} {s:.2f}"
        elif command.startswith("S"):
            # Setpoint command (value is in display units from set_pressure)
            try:
                setpoint_native = float(command[1:])
                self._sim_setpoint = self._display_to_psi(setpoint_native)
                self._sim_pressure = self._sim_setpoint
            except ValueError:
                pass
            return f"{self.address}"
        else:
            return f"{self.address}"
    
    def apply_error_model_config(self, model: Optional[Dict[str, Any]]) -> None:
        """Update the Alicat error model at runtime (e.g. after quality calibration)."""
        if model and model.get('type'):
            self._error_model = model
        else:
            self._error_model = None

    @_serialized_operation
    def read_status(self) -> Optional[AlicatReading]:
        """
        Read current pressure and setpoint from the Alicat.
        
        Returns:
            AlicatReading with pressure and setpoint, or None on error.
        """
        timestamp = time.time()
        
        response = self._send_command("")
        if not response:
            return None

        parts = response.split()
        # Handle responses that omit a space after address (e.g., "A3.6 +0")
        if parts and parts[0].upper().startswith(self.address) and len(parts[0]) > 1:
            parts = [self.address, parts[0][1:]] + parts[1:]
        has_address = len(parts) > 0 and parts[0].upper() == self.address
        value_parts = parts[1:] if has_address else parts
        numeric_values = []
        for value in value_parts:
            try:
                numeric_values.append(float(value))
            except ValueError:
                continue

        if not self._logged_raw_status:
            logger.info(
                "Alicat %s raw status response: %s (values=%s)",
                self.address,
                response,
                numeric_values,
            )
            self._logged_raw_status = True

        def get_indexed_value(index: Optional[int]) -> Optional[float]:
            if index is None:
                return None
            if 0 <= index < len(numeric_values):
                return numeric_values[index]
            return None

        pressure = get_indexed_value(self._pressure_index)
        setpoint = get_indexed_value(self._setpoint_index)
        gauge_pressure = get_indexed_value(self._gauge_index)
        barometric_pressure = get_indexed_value(self._barometric_index)

        try:
            if pressure is None and len(numeric_values) >= 1:
                pressure = numeric_values[0]
            if setpoint is None and len(numeric_values) >= 2:
                setpoint = numeric_values[1]

            # Short status packets (e.g. "A +013.53 +014.70 EXH") only carry
            # pressure + setpoint. Do not treat the setpoint slot as gauge/barometric.
            if len(numeric_values) < 3:
                if self._gauge_index is not None and self._gauge_index >= len(numeric_values):
                    gauge_pressure = None
                if self._barometric_index is not None and self._barometric_index >= len(numeric_values):
                    barometric_pressure = None
            if (
                gauge_pressure is not None
                and setpoint is not None
                and abs(gauge_pressure - setpoint) < 0.05
            ):
                gauge_pressure = None

            if pressure is not None and setpoint is not None:
                # Normalise to PSI so the rest of the application has a
                # single consistent unit regardless of the Alicat's
                # display configuration.
                pressure_psi = self._display_to_psi(pressure)
                pressure_raw_psi = pressure_psi
                pressure_corrected = apply_error_model(pressure_psi, self._error_model)
                return AlicatReading(
                    pressure=pressure_corrected,
                    setpoint=self._display_to_psi(setpoint),
                    timestamp=timestamp,
                    gauge_pressure=(
                        self._display_to_psi(gauge_pressure)
                        if gauge_pressure is not None else None
                    ),
                    barometric_pressure=(
                        self._display_to_psi(barometric_pressure)
                        if barometric_pressure is not None else None
                    ),
                    pressure_raw=pressure_raw_psi,
                    raw_response=response,
                    raw_values=numeric_values,
                )
        except (ValueError, IndexError) as e:
            logger.error(f"Alicat {self.address}: Parse error: {e}, response: {response}")
        
        return None
    
    @_serialized_operation
    def set_pressure(self, setpoint_psi: float) -> bool:
        """
        Set the pressure setpoint.

        Args:
            setpoint_psi: Target pressure **in PSI**.  The value is
                automatically converted to the Alicat's current display
                units before sending.

        Returns:
            True if command was acknowledged.
        """
        setpoint_native = self._psi_to_display(setpoint_psi)
        use_psi_commands = self._prefer_psi_commands and self._display_units_are_psi()
        if use_psi_commands:
            commands = [
                (f'S {setpoint_psi:.2f}', 'psi-spaced'),
                (f'S{setpoint_psi:.2f}', 'psi-compact'),
                (f'S {setpoint_native:.2f}', 'native-spaced'),
                (f'S{setpoint_native:.2f}', 'native-compact'),
            ]
        else:
            commands = [
                (f'S {setpoint_native:.2f}', 'native-spaced'),
                (f'S{setpoint_native:.2f}', 'native-compact'),
                (f'S {setpoint_psi:.2f}', 'psi-spaced'),
                (f'S{setpoint_psi:.2f}', 'psi-compact'),
            ]

        response: Optional[str] = None
        success = False
        for index, (command, mode) in enumerate(commands):
            response = self._send_command(command)
            success = self._is_ack(response)
            if success:
                self._prefer_psi_commands = mode.startswith('psi-') and self._display_units_are_psi()
                if index > 0:
                    logger.warning(
                        'Alicat %s: Setpoint accepted via %s fallback',
                        self.address,
                        mode,
                    )
                break
            if index == 0:
                logger.warning(
                    'Alicat %s: Setpoint command format fallback (%r -> compact)',
                    self.address,
                    response,
                )
            elif index == 1:
                logger.warning(
                    'Alicat %s: Setpoint unit fallback to PSI command (%r)',
                    self.address,
                    response,
                )

        if success:
            logger.debug(
                "Alicat %s: Setpoint -> %.2f %s (%.4f PSI)",
                self.address, setpoint_native,
                self._display_units_label, setpoint_psi,
            )
        else:
            logger.error(
                "Alicat %s: Failed to set pressure to %.2f %s "
                "(%.4f PSI, response=%r)",
                self.address, setpoint_native,
                self._display_units_label, setpoint_psi, response,
            )

        return success

    @_serialized_operation
    def resume_and_set_pressure(self, setpoint_psi: float) -> bool:
        """Enter control mode and send a new setpoint as one serialized operation."""
        if not self.cancel_hold():
            return False
        return self.set_pressure(setpoint_psi)
    
    @_serialized_operation
    def set_ramp_rate(self, rate_psi: float, time_unit: str = 's') -> bool:
        """
        Set the pressure ramp rate.

        Args:
            rate_psi: Ramp rate in **PSI** per *time_unit*.  Converted to
                the Alicat's display units automatically.
            time_unit: Time unit ('s' for seconds, 'm' for minutes).

        Returns:
            True if command was acknowledged.
        """
        rate_native = self._psi_to_display(rate_psi)
        unit_map = {'ms': 3, 's': 4, 'm': 5, 'h': 6, 'd': 7}
        unit_val = unit_map.get(time_unit, 4)
        use_psi_commands = self._prefer_psi_commands and self._display_units_are_psi()
        if use_psi_commands:
            commands = [
                (f'SR {rate_psi:.4f} {unit_val}', 'psi-spaced'),
                (f'SR{rate_psi:.4f} {unit_val}', 'psi-compact'),
                (f'SR {rate_native:.4f} {unit_val}', 'native-spaced'),
                (f'SR{rate_native:.4f} {unit_val}', 'native-compact'),
            ]
        else:
            commands = [
                (f'SR {rate_native:.4f} {unit_val}', 'native-spaced'),
                (f'SR{rate_native:.4f} {unit_val}', 'native-compact'),
                (f'SR {rate_psi:.4f} {unit_val}', 'psi-spaced'),
                (f'SR{rate_psi:.4f} {unit_val}', 'psi-compact'),
            ]

        response: Optional[str] = None
        success = False
        for index, (command, mode) in enumerate(commands):
            response = self._send_command(command)
            success = self._is_ack(response)
            if success:
                self._prefer_psi_commands = mode.startswith('psi-') and self._display_units_are_psi()
                if index > 0:
                    logger.warning(
                        'Alicat %s: Ramp accepted via %s fallback',
                        self.address,
                        mode,
                    )
                logger.info(
                    'Alicat %s: Ramp rate -> %.4f PSI/s (%s)',
                    self.address,
                    rate_psi,
                    mode,
                )
                return True

        logger.error(
            'Alicat %s: Failed to set ramp rate %.4f PSI/s (native=%.4f %s/s, response=%r)',
            self.address,
            rate_psi,
            rate_native,
            self._display_units_label,
            response,
        )
        return False
    
    @_serialized_operation
    def cancel_hold(self) -> bool:
        """
        Cancel hold mode and resume closed-loop control.
        
        Returns:
            True if command was acknowledged.
        """
        response = self._send_command("C")
        return self._is_ack(response)
    
    @_serialized_operation
    def hold_valve(self, closed: bool = False) -> bool:
        """
        Engage valve hold mode.
        
        Args:
            closed: If True, hold valve closed; otherwise hold at current position.
            
        Returns:
            True if command was acknowledged.
        """
        command = "HC" if closed else "HP"
        response = self._send_command(command)
        return self._is_ack(response)
    
    @_serialized_operation
    def exhaust(self) -> bool:
        """
        Activate exhaust mode (vent to atmosphere).
        
        Returns:
            True if command was acknowledged.
        """
        response = self._send_command("E")
        return self._is_ack(response)
    
    @_serialized_operation
    def tare(self) -> bool:
        """
        Tare (zero) the pressure reading.
        
        Returns:
            True if command was acknowledged.
        """
        response = self._send_command("PC")
        return self._is_ack(response)
    
    def disconnect(self) -> None:
        """Close the serial connection."""
        if self._serial and self._owns_serial:
            try:
                self._serial.close()
            except Exception:
                pass
            with self._shared_serial_lock:
                current = self._shared_serials.get(self.com_port)
                if current is self._serial:
                    self._shared_serials.pop(self.com_port, None)
        self._serial = None
        self._is_connected = False
        self._last_status = "Disconnected"
        logger.info(f"Alicat {self.address}: Disconnected")
    
    def hardware_available(self) -> bool:
        """Return True if pyserial is available and hardware can be used."""
        return SERIAL_AVAILABLE

    def get_status(self) -> Dict[str, Any]:
        """Get current status of the Alicat controller."""
        return {
            "address": self.address,
            "port": self.com_port,
            "connected": self._is_connected,
            "status": self._last_status,
        }

    def sim_set_pressure(self, pressure: float) -> None:
        """Set simulated pressure (for testing)."""
        self._sim_pressure = pressure

    @staticmethod
    def _coerce_optional_int(value: Any) -> Optional[int]:
        """Convert optional config value to int, if possible."""
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    
    @staticmethod
    def list_available_ports() -> List[Dict[str, str]]:
        """List available serial ports."""
        if not SERIAL_AVAILABLE:
            return []
        
        if serial is None:
            return []

        list_ports_module = getattr(serial, 'tools', None)
        if list_ports_module is None:
            return []

        list_ports = getattr(list_ports_module, 'list_ports', None)
        if list_ports is None:
            return []

        try:
            ports = []
            for port in list_ports.comports():
                ports.append({
                    'device': port.device,
                    'description': port.description,
                })
            return ports
        except Exception as e:
            logger.error(f"Error listing serial ports: {e}")
            return []
