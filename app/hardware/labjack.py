"""
LabJack T-series controller (T7-Pro).

Handles:
- Analog input (ratiometric transducer)
- Digital input (switch NO/NC states)
- Digital output (solenoid control)
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Dict, Optional

from app.services.pressure_calibration import apply_error_model, build_legacy_two_band_model

logger = logging.getLogger(__name__)


def _prime_labjack_dll_search_path() -> None:
    """Help Python locate LabJackM.dll before importing labjack.ljm."""
    candidate_dirs = [
        r"C:\Windows\System32",
        r"C:\Program Files\LabJack\Drivers",
        r"C:\Program Files (x86)\LabJack\Drivers",
    ]
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    updated = False
    for directory in candidate_dirs:
        if not os.path.isdir(directory):
            continue
        try:
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(directory)
        except Exception:
            pass
        if directory not in path_entries:
            path_entries.insert(0, directory)
            updated = True
    if updated:
        os.environ["PATH"] = os.pathsep.join(path_entries)


_prime_labjack_dll_search_path()

ljm: Any = None
LJM_IMPORT_ERROR: Optional[str] = None
LJM_NATIVE_LIB_LOADED = False
try:
    from labjack import ljm as _ljm
    import labjack.ljm.ljm as _ljm_native

    ljm = _ljm
    LJM_NATIVE_LIB_LOADED = getattr(_ljm_native, '_staticLib', None) is not None
    LJM_AVAILABLE = bool(LJM_NATIVE_LIB_LOADED)
    if not LJM_AVAILABLE:
        LJM_IMPORT_ERROR = (
            'labjack.ljm imported but the native LJM driver library did not load '
            '(LabJackM.dll unavailable).'
        )
        logger.warning(LJM_IMPORT_ERROR)
except ImportError as exc:
    LJM_IMPORT_ERROR = f'labjack.ljm not available: {exc}'
    logger.warning('labjack.ljm not available - LabJack hardware unavailable')
    LJM_AVAILABLE = False


@dataclass
class TransducerReading:
    """Result of a transducer reading."""

    voltage: float
    pressure: float
    pressure_raw: Optional[float]
    pressure_reference: str
    timestamp: float


@dataclass
class SwitchState:
    """State of the switch terminals."""

    no_active: bool  # Normally Open terminal is active (closed)
    nc_active: bool  # Normally Closed terminal is active (closed)
    timestamp: float

    @property
    def is_valid(self) -> bool:
        """Check if state is valid (not both active or both inactive for SPDT)."""
        return self.no_active != self.nc_active

    @property
    def switch_activated(self) -> bool:
        """True when tripped; uses NO/NC pair, including single-pole fallbacks."""
        from app.services.sweep_primitives import collapse_switch_activated

        return collapse_switch_activated(no_active=self.no_active, nc_active=self.nc_active)


def _solenoid_state_path() -> Path:
    """Persisted DIO states survive process restarts (vacuum leak-down)."""
    return Path(__file__).resolve().parents[2] / 'logs' / 'vacuum_solenoid_dio.json'


class LabJackController:
    """
    Controls a single LabJack device with per-port channel assignments.

    A single LabJack is shared across all ports; each controller instance
    references a shared LJM handle and uses its own channel mapping.
    """

    _handle_lock = threading.Lock()
    _io_lock = threading.RLock()
    _shared_handle: Optional[int] = None
    _handle_ref_count = 0
    _solenoid_dio_state: ClassVar[Dict[int, int]] = {}
    _solenoid_file_loaded: ClassVar[bool] = False

    @classmethod
    def _load_solenoid_state_file(cls) -> None:
        if cls._solenoid_file_loaded:
            return
        cls._solenoid_file_loaded = True
        path = _solenoid_state_path()
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
            cls._solenoid_dio_state.update({int(k): int(v) for k, v in raw.items()})
        except Exception as exc:
            logger.warning('Could not load solenoid state file %s: %s', path, exc)

    @classmethod
    def _persist_solenoid_state_file(cls) -> None:
        path = _solenoid_state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({str(k): v for k, v in cls._solenoid_dio_state.items()}, indent=2),
                encoding='utf-8',
            )
        except Exception as exc:
            logger.warning('Could not persist solenoid state file %s: %s', path, exc)

    def _solenoid_output(self, to_vacuum: bool) -> int:
        """Return the configured DIO output for the requested physical route."""
        return self.solenoid_vacuum_state if to_vacuum else self.solenoid_atmosphere_state

    def _initial_solenoid_output(self) -> int:
        """Always configure a newly opened solenoid DIO to its safe route."""
        return self._solenoid_output(to_vacuum=False)

    def _remember_solenoid_output(self, output: int) -> None:
        if self.solenoid_dio is None:
            return
        self._solenoid_dio_state[self.solenoid_dio] = 1 if output else 0
        self._persist_solenoid_state_file()

    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config

        self.device_type = config.get('device_type', 'T7')
        self.connection_type = config.get('connection_type', 'USB')
        self.identifier = config.get('identifier', 'ANY')

        self.transducer_ain = config.get('transducer_ain')
        self.transducer_ain_neg = config.get('transducer_ain_neg')  # Negative channel for differential
        self.voltage_min = config.get('transducer_voltage_min', 0.5)
        self.voltage_max = config.get('transducer_voltage_max', 4.5)
        self.pressure_min = config.get('transducer_pressure_min', 0.0)
        self.pressure_max = config.get('transducer_pressure_max', 115.0)
        self.pressure_reference = str(config.get('transducer_reference', 'absolute')).lower()
        self.pressure_offset = float(config.get('transducer_offset_psi', 0.0))
        # Optional two-band nonlinear correction model (fit on static calibration data).
        # The model describes sensor error as:
        #   error_psi = slope * pressure_psi + intercept
        # and we apply correction as:
        #   corrected_psi = measured_psi - error_psi
        nonlinear_cfg = config.get('transducer_nonlinear_correction', {}) or {}
        error_model_cfg = config.get('transducer_error_model', {}) or {}
        self._error_model: Optional[Dict[str, Any]] = None
        if isinstance(error_model_cfg, dict) and error_model_cfg.get('type'):
            self._error_model = error_model_cfg
        self._nonlinear_breakpoint_psi = float(nonlinear_cfg.get('breakpoint_psi', 5.0))
        self._nonlinear_low_slope = float(nonlinear_cfg.get('low_slope_error_per_psi', 0.0))
        self._nonlinear_low_intercept = float(nonlinear_cfg.get('low_intercept_error_psi', 0.0))
        self._nonlinear_high_slope = float(nonlinear_cfg.get('high_slope_error_per_psi', 0.0))
        self._nonlinear_high_intercept = float(nonlinear_cfg.get('high_intercept_error_psi', 0.0))
        if self._error_model is None and nonlinear_cfg:
            self._error_model = build_legacy_two_band_model(
                breakpoint_psi=self._nonlinear_breakpoint_psi,
                low_slope_error_per_psi=self._nonlinear_low_slope,
                low_intercept_error_psi=self._nonlinear_low_intercept,
                high_slope_error_per_psi=self._nonlinear_high_slope,
                high_intercept_error_psi=self._nonlinear_high_intercept,
            )

        self.switch_no_dio = config.get('switch_no_dio')
        self.switch_nc_dio = config.get('switch_nc_dio')
        self.switch_com_dio = config.get('switch_com_dio')
        self.switch_com_state = int(config.get('switch_com_state', 1))
        self.switch_active_low = bool(config.get('switch_active_low', False))
        self.switch_nc_derived_from_no = bool(config.get('switch_nc_derived_from_no', False))
        self.switch_no_derived_from_nc = bool(config.get('switch_no_derived_from_nc', False))
        self.solenoid_dio = config.get('solenoid_dio')
        self.solenoid_vacuum_state = int(config.get('solenoid_vacuum_state', 1))
        self.solenoid_atmosphere_state = int(config.get('solenoid_atmosphere_state', 0))
        if self.solenoid_vacuum_state not in {0, 1} or self.solenoid_atmosphere_state not in {0, 1}:
            raise ValueError('Solenoid route states must be 0 or 1')
        if self.solenoid_vacuum_state == self.solenoid_atmosphere_state:
            raise ValueError('Solenoid vacuum and atmosphere states must differ')

        self._lock = threading.RLock()
        self._is_configured = False
        self._last_status = 'Not Initialized'
        self._io_retries = max(0, int(config.get('io_retries', 2)))
        self._io_retry_delay_s = max(0.0, float(config.get('io_retry_delay_s', 0.02)))

        # Resolution index: 0=device default, 1-8=16-bit ADC, 9-12=24-bit ADC (T7-Pro)
        self._resolution_index = int(config.get('resolution_index', 0))

        # EMA pressure filter: alpha in (0, 1]. 0 disables filtering, 1 = no smoothing.
        self._filter_alpha = float(config.get('pressure_filter_alpha', 0.4))
        self._ema_pressure: Optional[float] = None

        self._sim_pressure = 14.7
        self._sim_switch_activated = False
        self._allow_simulated_hardware = bool(config.get('allow_simulated_hardware', False))

        logger.info(
            'LabJackController initialized for %s/%s',
            self.connection_type,
            self.identifier,
        )

    @classmethod
    def _open_handle(cls, device_type: str, connection_type: str, identifier: str) -> Optional[int]:
        if not LJM_AVAILABLE:
            return None

        with cls._handle_lock:
            if cls._shared_handle is None:
                cls._shared_handle = ljm.openS(device_type, connection_type, identifier)
                cls._handle_ref_count = 0
            cls._handle_ref_count += 1
            return cls._shared_handle

    @classmethod
    def _close_handle(cls) -> None:
        if not LJM_AVAILABLE:
            return

        with cls._handle_lock:
            if cls._handle_ref_count > 0:
                cls._handle_ref_count -= 1
            if cls._handle_ref_count == 0 and cls._shared_handle is not None:
                try:
                    ljm.close(cls._shared_handle)
                finally:
                    cls._shared_handle = None

    def configure_di_pins(
        self,
        no_pin: int,
        nc_pin: int,
        com_pin: Optional[int] = None,
        com_state: Optional[int] = None,
    ) -> None:
        """Configure digital input pins for NO/NC terminals."""
        self.switch_no_dio = no_pin
        self.switch_nc_dio = nc_pin
        if com_pin is not None:
            self.switch_com_dio = com_pin
        if com_state is not None:
            self.switch_com_state = 1 if int(com_state) else 0
        logger.info('LabJack: NO=DIO%s, NC=DIO%s', self.switch_no_dio, self.switch_nc_dio)
        self._apply_switch_directions()

    def set_dio_direction(self, dio: int, is_output: bool, output_state: Optional[int] = None) -> bool:
        """Configure a DIO line as input or output.

        On T-series, individual DIO read sets the line to input; write sets it to output
        and the state. There is no per-channel DIO{n}_DIRECTION register; direction is
        implied by read vs write.
        """
        if not LJM_AVAILABLE:
            return True

        handle = self._shared_handle
        if handle is None:
            return False

        try:
            if is_output:
                return self._write_name_with_retry(f'DIO{dio}', 1 if output_state else 0)
            else:
                return self._read_name_with_retry(f'DIO{dio}') is not None
        except Exception as exc:
            logger.error('LabJack DIO direction set failed: %s', exc)
            return False

    def read_dio_values(self, max_dio: int = 19) -> Optional[Dict[int, int]]:
        """Read all DIO values from 0..max_dio inclusive."""
        if not LJM_AVAILABLE:
            if not self._allow_simulated_hardware:
                return None
            return {dio: 0 for dio in range(max_dio + 1)}

        handle = self._shared_handle
        if handle is None:
            return None

        try:
            state_value = self._read_name_with_retry('DIO_STATE')
            if state_value is None:
                return None
            state_mask = int(state_value)
            return {dio: 1 if state_mask & (1 << dio) else 0 for dio in range(max_dio + 1)}
        except Exception as exc:
            logger.error('LabJack DIO read failed: %s', exc)
            return None

    def set_pressure_reference(self, reference: str) -> None:
        """Set pressure reference (absolute or gauge)."""
        self.pressure_reference = str(reference or 'absolute').lower()

    def configure(self) -> bool:
        """Open the LabJack connection and set to safe state."""
        if not LJM_AVAILABLE:
            if self._allow_simulated_hardware:
                self._is_configured = True
                self._last_status = 'Configured (simulated)'
                return True
            self._is_configured = False
            self._last_status = f'Config Error: {LJM_IMPORT_ERROR or "LJM unavailable"}'
            return False

        with self._lock:
            try:
                handle = self._open_handle(self.device_type, self.connection_type, self.identifier)
                if handle is None:
                    self._last_status = 'Config Error: LJM unavailable'
                    return False

                # Configure differential mode for transducer if negative channel is specified
                if self.transducer_ain is not None and self.transducer_ain_neg is not None:
                    # Set the negative channel for differential measurement
                    # AIN#_NEGATIVE_CH register: value = negative channel number, or 199 for single-ended (GND)
                    ljm.eWriteName(handle, f'AIN{self.transducer_ain}_NEGATIVE_CH', self.transducer_ain_neg)
                    logger.info(
                        'LabJack: Configured AIN%d as differential (negative=AIN%d)',
                        self.transducer_ain,
                        self.transducer_ain_neg,
                    )
                elif self.transducer_ain is not None:
                    # Single-ended mode (negative = GND)
                    ljm.eWriteName(handle, f'AIN{self.transducer_ain}_NEGATIVE_CH', 199)
                    logger.info('LabJack: Configured AIN%d as single-ended', self.transducer_ain)

                # Set AIN resolution index (0 = device default)
                if self._resolution_index > 0 and self.transducer_ain is not None:
                    ljm.eWriteName(
                        handle,
                        f'AIN{self.transducer_ain}_RESOLUTION_INDEX',
                        self._resolution_index,
                    )
                    logger.info(
                        'LabJack: Set AIN%d resolution index to %d',
                        self.transducer_ain,
                        self._resolution_index,
                    )

                if self.solenoid_dio is not None:
                    initial = self._initial_solenoid_output()
                    self.set_dio_direction(self.solenoid_dio, True, initial)
                    logger.info(
                        'LabJack: solenoid DIO%s initial output=%d (atmosphere)',
                        self.solenoid_dio,
                        initial,
                    )

                self._apply_switch_directions()

                self._is_configured = True
                self._last_status = 'Configured'
                return True
            except Exception as exc:
                logger.error('LabJack configuration failed: %s', exc)
                self._last_status = f'Config Error: {exc}'
                self.cleanup()
                return False

    @staticmethod
    def _is_transient_ljm_error(exc: Exception) -> bool:
        message = str(exc)
        return 'LJME_RECONNECT_FAILED' in message or '1239' in message

    def _recover_handle(self) -> bool:
        if not LJM_AVAILABLE:
            return False

        with self._lock:
            try:
                self._close_handle()
                handle = self._open_handle(self.device_type, self.connection_type, self.identifier)
                if handle is None:
                    return False
                if self.transducer_ain is not None and self.transducer_ain_neg is not None:
                    ljm.eWriteName(handle, f'AIN{self.transducer_ain}_NEGATIVE_CH', self.transducer_ain_neg)
                elif self.transducer_ain is not None:
                    ljm.eWriteName(handle, f'AIN{self.transducer_ain}_NEGATIVE_CH', 199)
                if self._resolution_index > 0 and self.transducer_ain is not None:
                    ljm.eWriteName(
                        handle,
                        f'AIN{self.transducer_ain}_RESOLUTION_INDEX',
                        self._resolution_index,
                    )
                self._apply_switch_directions()
                return True
            except Exception as recovery_exc:
                logger.error('LabJack recovery failed: %s', recovery_exc)
                return False

    def _read_name_locked(self, handle: int, name: str) -> Optional[float]:
        """Read one register; caller must hold ``_io_lock``."""
        for attempt in range(self._io_retries + 1):
            try:
                return float(ljm.eReadName(handle, name))
            except Exception as exc:
                if attempt < self._io_retries and self._is_transient_ljm_error(exc):
                    logger.warning('LabJack transient read error (%s), retrying %s', exc, name)
                    if self._recover_handle():
                        handle = self._shared_handle
                        if handle is None:
                            return None
                    time.sleep(self._io_retry_delay_s)
                    continue
                raise
        return None

    def _read_names_locked(self, handle: int, names: list[str]) -> Optional[list[float]]:
        """Read multiple registers; caller must hold ``_io_lock``."""
        for attempt in range(self._io_retries + 1):
            try:
                values = ljm.eReadNames(handle, len(names), names)
                return [float(v) for v in values]
            except Exception as exc:
                if attempt < self._io_retries and self._is_transient_ljm_error(exc):
                    logger.warning('LabJack transient read error (%s), retrying %s', exc, names)
                    if self._recover_handle():
                        handle = self._shared_handle
                        if handle is None:
                            return None
                    time.sleep(self._io_retry_delay_s)
                    continue
                raise
        return None

    def _write_name_locked(self, handle: int, name: str, value: float) -> bool:
        """Write one register; caller must hold ``_io_lock``."""
        for attempt in range(self._io_retries + 1):
            try:
                ljm.eWriteName(handle, name, value)
                return True
            except Exception as exc:
                if attempt < self._io_retries and self._is_transient_ljm_error(exc):
                    logger.warning('LabJack transient write error (%s), retrying %s', exc, name)
                    if self._recover_handle():
                        handle = self._shared_handle
                        if handle is None:
                            return False
                    time.sleep(self._io_retry_delay_s)
                    continue
                logger.error('LabJack write failed for %s: %s', name, exc)
                return False
        return False

    def _read_name_with_retry(self, name: str) -> Optional[float]:
        handle = self._shared_handle
        if handle is None:
            return None

        with self._io_lock:
            return self._read_name_locked(handle, name)

    def _read_names_with_retry(self, names: list[str]) -> Optional[list[float]]:
        handle = self._shared_handle
        if handle is None:
            return None

        with self._io_lock:
            return self._read_names_locked(handle, names)

    def _write_name_with_retry(self, name: str, value: float) -> bool:
        handle = self._shared_handle
        if handle is None:
            return False

        with self._io_lock:
            return self._write_name_locked(handle, name, value)

    def _read_transducer_voltage(self) -> Optional[float]:
        """Read transducer differential voltage with T7 mux settling.

        When multiple differential AIN pairs share one T7, an immediate read of
        AIN# after another pair was sampled can return stale mux values (~15%
        outliers).  Reading the negative line single-ended once resets the ADC
        path before the differential sample.
        """
        handle = self._shared_handle
        pos = self.transducer_ain
        if handle is None or pos is None:
            return None

        neg = self.transducer_ain_neg
        with self._io_lock:
            if neg is None:
                return self._read_name_locked(handle, f'AIN{pos}')

            # 199 = single-ended vs device GND (LabJack T-series convention).
            if not self._write_name_locked(handle, f'AIN{neg}_NEGATIVE_CH', 199):
                return None
            if self._read_name_locked(handle, f'AIN{neg}') is None:
                return None
            if not self._write_name_locked(handle, f'AIN{pos}_NEGATIVE_CH', neg):
                return None
            return self._read_name_locked(handle, f'AIN{pos}')

    def _apply_ema(self, pressure: float) -> float:
        """Apply exponential moving average filter to pressure.

        Returns the filtered value.  When the filter is disabled
        (alpha <= 0 or alpha >= 1) or on the very first sample, the raw
        value is returned unchanged.
        """
        alpha = self._filter_alpha
        if alpha <= 0.0 or alpha >= 1.0:
            # Filtering disabled — pass through raw value
            self._ema_pressure = pressure
            return pressure
        if self._ema_pressure is None:
            # First sample — seed the filter
            self._ema_pressure = pressure
            return pressure
        self._ema_pressure = alpha * pressure + (1.0 - alpha) * self._ema_pressure
        return self._ema_pressure

    def apply_error_model_config(self, model: Optional[Dict[str, Any]]) -> None:
        """Update the transducer error model at runtime (e.g. after quality calibration)."""
        if model and model.get('type'):
            self._error_model = model
        else:
            self._error_model = None

    def _apply_nonlinear_correction(self, pressure_psi: float) -> float:
        """Apply optional two-band correction to pressure."""
        return apply_error_model(pressure_psi, self._error_model)

    def read_transducer(self) -> Optional[TransducerReading]:
        """Read pressure from the ratiometric transducer.

        Returns a TransducerReading with EMA-filtered pressure in
        ``pressure`` and the unfiltered value in ``pressure_raw``.
        """
        timestamp = time.time()

        if not LJM_AVAILABLE:
            if not self._allow_simulated_hardware:
                return None
            voltage_range = self.voltage_max - self.voltage_min
            pressure_range = self.pressure_max - self.pressure_min
            voltage = self.voltage_min + (
                (self._sim_pressure - self.pressure_min) / pressure_range * voltage_range
                if pressure_range > 0
                else 0.0
            )
            pressure_linear = self._sim_pressure + self.pressure_offset
            pressure_raw = self._apply_nonlinear_correction(pressure_linear)
            pressure_filtered = self._apply_ema(pressure_raw)
            return TransducerReading(
                voltage=voltage,
                pressure=pressure_filtered,
                pressure_raw=pressure_raw,
                pressure_reference=self.pressure_reference,
                timestamp=timestamp,
            )

        if self.transducer_ain is None:
            return None

        handle = self._shared_handle
        if handle is None:
            return None

        try:
            voltage = self._read_transducer_voltage()
            if voltage is None:
                return None
            voltage_range = self.voltage_max - self.voltage_min
            pressure_range = self.pressure_max - self.pressure_min
            if voltage_range > 0:
                pressure = (voltage - self.voltage_min) / voltage_range * pressure_range + self.pressure_min
            else:
                pressure = self.pressure_min
            pressure_linear = pressure + self.pressure_offset
            pressure_raw = self._apply_nonlinear_correction(pressure_linear)
            pressure_filtered = self._apply_ema(pressure_raw)
            return TransducerReading(
                voltage=voltage,
                pressure=pressure_filtered,
                pressure_raw=pressure_raw,
                pressure_reference=self.pressure_reference,
                timestamp=timestamp,
            )
        except Exception as exc:
            logger.error('LabJack transducer read error: %s', exc)
            return None

    def read_switch_state(self) -> Optional[SwitchState]:
        """Read the current state of the switch terminals."""
        timestamp = time.time()

        if not LJM_AVAILABLE:
            if not self._allow_simulated_hardware:
                return None
            return SwitchState(
                no_active=self._sim_switch_activated,
                nc_active=not self._sim_switch_activated,
                timestamp=timestamp,
            )

        if self.switch_no_dio is None or self.switch_nc_dio is None:
            return None

        handle = self._shared_handle
        if handle is None:
            return None

        try:
            names = [f'DIO{self.switch_no_dio}', f'DIO{self.switch_nc_dio}']
            states = self._read_names_with_retry(names)
            if isinstance(states, list) and len(states) >= 2:
                no_raw = bool(states[0])
                nc_raw = bool(states[1])
                if self.switch_active_low:
                    no_active = not no_raw
                    nc_active = not nc_raw
                else:
                    no_active = no_raw
                    nc_active = nc_raw
                if self.switch_nc_derived_from_no:
                    # Single-pole wiring: only NO line toggles; NC is the complementary contact.
                    nc_active = not no_active
                elif self.switch_no_derived_from_nc:
                    # Single-pole wiring: only NC line toggles; NO is the complementary contact.
                    no_active = not nc_active
                return SwitchState(
                    no_active=no_active,
                    nc_active=nc_active,
                    timestamp=timestamp,
                )
            return None
        except Exception as exc:
            logger.error('LabJack switch read error: %s', exc)
            return None

    def set_solenoid(self, to_vacuum: bool) -> bool:
        """Set solenoid state."""
        if not LJM_AVAILABLE:
            if not self._allow_simulated_hardware:
                return False
            self._remember_solenoid_output(self._solenoid_output(to_vacuum))
            logger.debug('LabJack solenoid -> %s', 'Vacuum' if to_vacuum else 'Atmosphere')
            return True

        if self.solenoid_dio is None:
            return False

        handle = self._shared_handle
        if handle is None:
            return False

        try:
            output = self._solenoid_output(to_vacuum)
            if not self._write_name_with_retry(f'DIO{self.solenoid_dio}', output):
                return False
            self._remember_solenoid_output(output)
            logger.debug('LabJack solenoid -> %s', 'Vacuum' if to_vacuum else 'Atmosphere')
            return True
        except Exception as exc:
            logger.error('LabJack solenoid control error: %s', exc)
            return False

    def set_solenoid_safe(self) -> bool:
        """Set solenoid to safe state (atmosphere)."""
        return self.set_solenoid(to_vacuum=False)

    def _apply_switch_directions(self) -> None:
        if not LJM_AVAILABLE:
            return
        if self.switch_no_dio is not None:
            self.set_dio_direction(self.switch_no_dio, False)
        if self.switch_nc_dio is not None:
            self.set_dio_direction(self.switch_nc_dio, False)
        if self.switch_com_dio is not None:
            self.set_dio_direction(self.switch_com_dio, True, self.switch_com_state)

    def reset_filter(self) -> None:
        """Reset the EMA pressure filter state.

        Call after large pressure discontinuities (e.g. solenoid switch) so
        the filter re-seeds from the next raw sample instead of slowly
        converging from the old value.
        """
        self._ema_pressure = None

    def sim_set_pressure(self, pressure: float) -> None:
        """Set simulated pressure (for testing)."""
        self._sim_pressure = pressure

    def sim_set_switch(self, activated: bool) -> None:
        """Set simulated switch state (for testing)."""
        self._sim_switch_activated = activated

    def cleanup(self, *, preserve_solenoid_state: bool = False) -> None:
        """Release LabJack resources.

        When ``preserve_solenoid_state`` is True, do not force the solenoid DIO
        to atmosphere before closing (e.g. vacuum leak-down test with pump off).
        """
        with self._lock:
            if LJM_AVAILABLE:
                try:
                    if self.solenoid_dio is not None and self._shared_handle is not None:
                        if preserve_solenoid_state:
                            try:
                                val = int(
                                    ljm.eReadName(
                                        self._shared_handle,
                                        f'DIO{self.solenoid_dio}',
                                    )
                                )
                                self._remember_solenoid_output(val)
                            except Exception:
                                pass
                        else:
                            ljm.eWriteName(self._shared_handle, f'DIO{self.solenoid_dio}', 0)
                            self._remember_solenoid_output(0)
                except Exception:
                    pass
                self._close_handle()

            self._is_configured = False
            self._last_status = 'Closed'

        logger.info('LabJack resources cleaned up')

    def hardware_available(self) -> bool:
        """Return True if the LabJack library is available and hardware can be used."""
        return LJM_AVAILABLE

    def get_status(self) -> Dict[str, Any]:
        """Get current status of the LabJack controller."""
        return {
            'device_type': self.device_type,
            'connection_type': self.connection_type,
            'identifier': self.identifier,
            'configured': self._is_configured,
            'status': self._last_status,
            'driver_loaded': LJM_AVAILABLE,
            'driver_error': LJM_IMPORT_ERROR,
            'simulated': self._allow_simulated_hardware and not LJM_AVAILABLE,
        }
