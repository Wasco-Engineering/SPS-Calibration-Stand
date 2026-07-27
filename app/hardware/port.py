"""
Port abstraction - combines LabJack + Alicat for a single test port.

Each port (A/B, Left/Right) is an independent test station with its own
hardware and state machine.
"""

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Callable

from app.core.config import is_port_installed
from app.services.ptp_switch_resolver import PtpSwitchResolution, resolve_ptp_switch_config

from .labjack import LabJackController, TransducerReading, SwitchState
from .alicat import AlicatController, AlicatReading

logger = logging.getLogger(__name__)


class PortId(Enum):
    """Identifier for test ports."""
    PORT_A = "port_a"  # Left
    PORT_B = "port_b"  # Right


@dataclass
class PortReading:
    """Combined reading from all port hardware."""
    transducer: Optional[TransducerReading] = None
    switch: Optional[SwitchState] = None
    alicat: Optional[AlicatReading] = None
    dio: Optional[Dict[int, int]] = None
    timestamp: float = 0.0


@dataclass
class EdgeEvent:
    """Record of a switch edge detection."""
    pressure: float
    timestamp: float
    direction: str  # 'increasing' or 'decreasing'
    activated: bool  # True if switch became activated
    

# Nominal atmosphere for absolute-pressure safety check (PSI)
_ATMOSPHERE_PSI = 14.7
_IDLE_ATMOSPHERE_TOLERANCE_PSIA = 1.0
_IDLE_BLEED_TIMEOUT_S = 90.0


class Port:
    """Single test port with LabJack + Alicat hardware."""
    
    def __init__(
        self,
        port_id: PortId,
        labjack_config: Dict[str, Any],
        alicat_config: Dict[str, Any],
        solenoid_config: Optional[Dict[str, Any]] = None,
    ):
        """Initialize a test port."""
        self.port_id = port_id
        self._solenoid_config = solenoid_config or {}
        self._labjack_config = dict(labjack_config)
        self._transducer_installed = bool(labjack_config.get('transducer_installed', True))

        # Initialize hardware controllers
        self.daq = LabJackController(labjack_config)
        self.alicat = AlicatController(alicat_config)
        
        # Edge detection state
        self._last_switch_state: Optional[SwitchState] = None
        self._edge_history: List[EdgeEvent] = []
        self._edge_callbacks: List[Callable[[EdgeEvent], None]] = []

        # Cached Alicat reading for fast polling (updated every Nth cycle)
        self._cached_alicat: Optional[AlicatReading] = None
        self._cached_alicat_lock = threading.Lock()
        
        # Current test context
        self._no_pin: Optional[int] = None
        self._nc_pin: Optional[int] = None
        self._last_switch_resolution: Optional[PtpSwitchResolution] = None
        
        logger.info(f"Port {port_id.value} initialized")

    def _open_fitting_line(self) -> bool:
        """True when the DUT line is open to room air (no sealed transducer path)."""
        if 'open_fitting' in self._labjack_config:
            return bool(self._labjack_config.get('open_fitting'))
        return not self._transducer_installed

    def _configured_barometric_psia(self) -> float:
        """Site-local barometric default (e.g. ~13.5 PSIA in Idaho), else sea level."""
        raw = self._labjack_config.get('local_barometric_psi')
        if raw is not None:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                value = float('nan')
            if value == value and value > 0.0:  # finite
                return value
        return _ATMOSPHERE_PSI
    
    def configure_from_ptp(self, ptp_params: Dict[str, str]) -> bool:
        """Configure port hardware from PTP parameters."""
        try:
            resolution = resolve_ptp_switch_config(
                ptp_params=ptp_params,
                port_id=self.port_id.value,
                port_config=self._labjack_config,
            )
            self._last_switch_resolution = resolution
            for warning in resolution.warnings:
                logger.warning('Port %s: %s', self.port_id.value, warning)
            if not resolution.is_valid:
                logger.error(
                    'Port %s: PTP switch configuration invalid: %s',
                    self.port_id.value,
                    '; '.join(resolution.errors) or 'unknown error',
                )
                return False

            logger.info(
                'Port %s: Using PTP switch terminals %s',
                self.port_id.value,
                resolution.summary,
            )
            self._no_pin = resolution.no_dio
            self._nc_pin = resolution.nc_dio
            self.daq.switch_nc_derived_from_no = resolution.derive_nc_from_no
            self.daq.switch_no_derived_from_nc = resolution.derive_no_from_nc
            self.daq.configure_di_pins(
                resolution.no_dio,
                resolution.nc_dio,
                resolution.drive_dio,
                com_state=self.daq.switch_com_state,
            )
            
            logger.info(f"Port {self.port_id.value}: Configured from PTP")
            return True
            
        except Exception as e:
            logger.error(f"Port {self.port_id.value}: PTP configuration error: {e}")
            return False

    @property
    def last_switch_resolution(self) -> Optional[PtpSwitchResolution]:
        """Most recent PTP switch resolution for diagnostics/tests."""
        return self._last_switch_resolution
    
    def connect(self) -> bool:
        """
        Connect to all hardware for this port.
        
        Returns:
            True if all connections successful.
        """
        success = True
        
        # Configure LabJack
        if not self.daq.configure():
            logger.error(f"Port {self.port_id.value}: LabJack configuration failed")
            success = False
        
        # Connect to Alicat
        if not self.alicat.connect():
            logger.error(f"Port {self.port_id.value}: Alicat connection failed")
            success = False
        
        if success:
            logger.info(f"Port {self.port_id.value}: All hardware connected")
        
        return success
    
    def read_all(self) -> PortReading:
        """Read all sensors for this port."""
        return self._read(use_cached_alicat=False)
    
    def refresh_alicat(self) -> bool:
        """Update the cached Alicat reading (slow serial I/O)."""
        reading = self.alicat.read_status()
        if reading is None:
            return False
        with self._cached_alicat_lock:
            self._cached_alicat = reading
        return True

    def get_cached_alicat(self) -> Optional[AlicatReading]:
        """Return the most recent complete Alicat status reading."""
        with self._cached_alicat_lock:
            return self._cached_alicat

    def read_fast(self) -> PortReading:
        """Read LabJack-only sensors (fast path) using cached Alicat.

        Reads transducer, switch state, and DIO from the LabJack but uses
        the most recently cached Alicat reading instead of blocking on serial.
        """
        return self._read(use_cached_alicat=True, include_dio=True)

    def read_precision_fast(self) -> PortReading:
        """Minimal LabJack read for precision sweep (transducer + switch only).

        Skips DIO_STATE to reduce shared T7 bus time while Alicat runs at the
        precision poll divisor on the same loop.
        """
        return self._read(use_cached_alicat=True, include_dio=False)

    def _read(self, use_cached_alicat: bool, include_dio: bool = True) -> PortReading:
        """Shared read path used by full, fast, and precision reads."""
        import time

        timestamp = time.time()
        alicat_reading = self.get_cached_alicat() if use_cached_alicat else self.alicat.read_status()

        reading = PortReading(
            transducer=self.daq.read_transducer() if self._transducer_installed else None,
            switch=self.daq.read_switch_state(),
            alicat=alicat_reading,
            dio=self.daq.read_dio_values(max_dio=22) if include_dio else None,
            timestamp=timestamp,
        )
        self._normalize_transducer_reference(reading)
        self._check_for_edge(reading)
        return reading

    def _normalize_transducer_reference(self, reading: PortReading) -> None:
        """Convert transducer absolute to gauge when LabJack is gauge-referenced."""
        # Convert transducer absolute -> gauge if configured
        if reading.transducer and reading.alicat:
            if getattr(self.daq, 'pressure_reference', 'absolute') == 'gauge':
                baro = reading.alicat.barometric_pressure
                if baro is not None:
                    reading.transducer.pressure = reading.transducer.pressure - baro
                    reading.transducer.pressure_reference = 'gauge'

    def _check_for_edge(self, reading: PortReading) -> None:
        """Check if a switch edge occurred and record it."""
        if reading.switch is None:
            return
        
        current = reading.switch
        previous = self._last_switch_state
        
        if previous is not None and current.switch_activated != previous.switch_activated:
            # Edge detected!
            pressure = self._physical_abs_pressure_psi(reading)
            if pressure is None:
                pressure = 0.0
            
            # Determine direction based on pressure change
            # (Would need to track pressure history for accurate direction)
            direction = "unknown"  # Will be set by state machine based on control direction
            
            edge = EdgeEvent(
                pressure=pressure,
                timestamp=current.timestamp,
                direction=direction,
                activated=current.switch_activated
            )
            
            self._edge_history.append(edge)
            logger.info(f"Port {self.port_id.value}: Edge detected at {pressure:.2f} PSI, "
                       f"activated={current.switch_activated}")
            
            # Notify callbacks
            for callback in self._edge_callbacks:
                try:
                    callback(edge)
                except Exception as e:
                    logger.error(f"Edge callback error: {e}")
        
        self._last_switch_state = current

    def _physical_abs_pressure_psi_for_solenoid_guard(self) -> tuple[Optional[float], float]:
        """Best-effort absolute pressure and barometric basis for vacuum-route safety."""
        if self._transducer_installed:
            # Differential AIN reads can occasionally contain a single mux-settling
            # outlier after switching between the two transducer pairs.  A lone high
            # sample must not strand a safely vented port, while a genuinely
            # pressurized line will remain high across this short sample group.
            samples: list[float] = []
            for _ in range(5):
                transducer = self.daq.read_transducer()
                reading = PortReading(transducer=transducer, alicat=self.get_cached_alicat())
                self._normalize_transducer_reference(reading)
                pressure = self._physical_abs_pressure_psi(reading)
                if pressure is not None:
                    samples.append(float(pressure))
            if samples:
                samples.sort()
                pressure = samples[len(samples) // 2]
                barometric = _ATMOSPHERE_PSI
                if reading.alicat and reading.alicat.barometric_pressure is not None:
                    barometric = float(reading.alicat.barometric_pressure)
                return pressure, barometric

        alicat_reading = self.alicat.read_status() or self.get_cached_alicat()
        barometric = _ATMOSPHERE_PSI
        if alicat_reading and alicat_reading.barometric_pressure is not None:
            barometric = float(alicat_reading.barometric_pressure)
        return self._alicat_abs_pressure_psi(alicat_reading), barometric

    @staticmethod
    def _alicat_abs_pressure_psi(reading: Optional[AlicatReading]) -> Optional[float]:
        if reading is None:
            return None
        if reading.pressure is not None:
            return float(reading.pressure)
        if reading.gauge_pressure is None:
            return None
        barometric = reading.barometric_pressure
        if barometric is None:
            barometric = _ATMOSPHERE_PSI
        return float(reading.gauge_pressure + barometric)

    def _physical_abs_pressure_psi(self, reading: PortReading) -> Optional[float]:
        """Best-effort physical line pressure for edge history/logging."""
        if self._transducer_installed and reading.transducer is not None:
            from app.services.measurement_source import _transducer_pressure_abs_psi

            barometric = (
                reading.alicat.barometric_pressure
                if reading.alicat and reading.alicat.barometric_pressure is not None
                else _ATMOSPHERE_PSI
            )
            pressure = _transducer_pressure_abs_psi(reading, float(barometric))
            if pressure is not None:
                return pressure
        return self._alicat_abs_pressure_psi(reading.alicat)
    
    def register_edge_callback(self, callback: Callable[[EdgeEvent], None]) -> None:
        """Register a callback to be called when an edge is detected."""
        self._edge_callbacks.append(callback)
    
    def clear_edge_history(self) -> None:
        """Clear the edge detection history."""
        self._edge_history.clear()
        self._last_switch_state = None
    
    def get_edge_history(self) -> List[EdgeEvent]:
        """Get the list of detected edges."""
        return self._edge_history.copy()
    
    def set_pressure(self, setpoint: float) -> bool:
        """Exit EXH/hold and set the Alicat pressure setpoint."""
        combined = getattr(self.alicat, 'resume_and_set_pressure', None)
        if callable(combined):
            return bool(combined(setpoint))
        if not self.alicat.cancel_hold():
            logger.warning('%s: Failed to enter Alicat control mode before setpoint', self.port_id.value)
            return False
        return self.alicat.set_pressure(setpoint)
    
    def set_ramp_rate(self, rate: float) -> bool:
        """Set the Alicat ramp rate."""
        return self.alicat.set_ramp_rate(rate)
    
    def set_solenoid(self, to_vacuum: bool) -> bool:
        """Set the solenoid state.

        Pump protection: do not switch to vacuum unless port pressure is at or
        below the safe threshold (~atmosphere). Switching with high positive
        pressure can damage the pump.
        """
        if to_vacuum:
            # Only open to vacuum when pressure is close to atm (pump blowout protection).
            threshold_psi = self._solenoid_config.get(
                "safe_vacuum_switch_threshold_psi", 1.0
            )
            if threshold_psi is not None:
                pressure_psi, barometric = self._physical_abs_pressure_psi_for_solenoid_guard()
                safe_limit = barometric + float(threshold_psi)

                if pressure_psi is None or pressure_psi > safe_limit:
                    logger.warning(
                        "%s: Refusing vacuum - port pressure %.2f exceeds safe limit %.2f psi "
                        "(pump protection)",
                        self.port_id.value,
                        pressure_psi if pressure_psi is not None else -1.0,
                        safe_limit,
                    )
                    return False
        result = self.daq.set_solenoid(to_vacuum)
        if result:
            # Reset EMA filter so it re-seeds from the next sample after the
            # pressure discontinuity caused by the solenoid switch.
            self.daq.reset_filter()
        return result

    def connect_test_route(self) -> bool:
        """Connect the DUT to the Alicat-controlled test line.

        The energized solenoid state is named ``vacuum`` historically, but on
        this stand it is the active Alicat/test route for both positive-pressure
        and vacuum moves.  Callers should still use ``vent_to_atmosphere`` for
        the safe/idle state.
        """
        result = self.daq.set_solenoid(to_vacuum=True)
        if result:
            self.daq.reset_filter()
        return result

    def _sample_barometric_via_exhaust(self, timeout_s: float = 2.0) -> Optional[float]:
        """Vent the Alicat to EXH on the atmosphere route and read local baro."""
        from app.services.pressure_domain import is_plausible_barometric_psi

        self.daq.set_solenoid_safe()
        self.daq.reset_filter()
        if not self.alicat.exhaust():
            return None
        deadline = time.perf_counter() + max(0.5, timeout_s)
        last_plausible: Optional[float] = None
        while time.perf_counter() < deadline:
            time.sleep(0.15)
            reading = self.read_all()
            pressure = self._alicat_abs_psia(reading, 0.0)
            if pressure is not None and is_plausible_barometric_psi(pressure):
                last_plausible = float(pressure)
                if self._alicat_in_exhaust_mode():
                    break
        if last_plausible is not None:
            self.alicat.hold_valve(closed=True)
        return last_plausible

    def _infer_barometric_psia(self, reading: Optional[PortReading] = None) -> float:
        local_default = self._configured_barometric_psia()
        if reading is None:
            try:
                reading = self.read_all()
            except Exception:
                return local_default
        if reading.alicat is None:
            return local_default
        if reading.alicat.barometric_pressure is not None:
            return float(reading.alicat.barometric_pressure)
        from app.services.pressure_domain import (
            infer_barometric_pressure_from_alicat,
            is_plausible_barometric_psi,
        )
        inferred = infer_barometric_pressure_from_alicat(reading.alicat)
        if inferred is not None:
            return inferred
        # Short Alicat status packets omit baro; on an open line at hold the
        # absolute reading is local barometric pressure (e.g. ~13.5 PSIA in Idaho)
        # unless the controller is parked on a sea-level setpoint.
        if (
            self._alicat_in_hold_mode()
            and not self._alicat_in_exhaust_mode()
            and reading.alicat.pressure is not None
            and is_plausible_barometric_psi(float(reading.alicat.pressure))
        ):
            pressure = float(reading.alicat.pressure)
            setpoint = reading.alicat.setpoint
            if setpoint is None or abs(pressure - float(setpoint)) > 0.75:
                return pressure
        return local_default

    def _alicat_abs_psia(self, reading: Optional[PortReading], barometric_psia: float) -> Optional[float]:
        if reading is None or reading.alicat is None:
            return None
        if reading.alicat.pressure is not None:
            return float(reading.alicat.pressure)
        return None

    def _alicat_in_exhaust_mode(self) -> bool:
        reading = self.alicat.read_status()
        if reading is None or not reading.raw_response:
            return False
        return 'EXH' in reading.raw_response.upper()

    def _alicat_in_hold_mode(self) -> bool:
        reading = self.alicat.read_status()
        if reading is None or not reading.raw_response:
            return False
        return 'HLD' in reading.raw_response.upper()

    def is_at_atmospheric_idle(self, barometric_psia: Optional[float] = None) -> bool:
        """True when the DUT line is already at safe atmospheric idle.

        Used on connect/disconnect to avoid disturbing ports that are already
        sitting near barometric pressure with no exhaust bleed active.
        """
        if self._alicat_in_exhaust_mode():
            return False
        if barometric_psia is None:
            barometric_psia = self._infer_barometric_psia()
        reading = self.read_all()
        current = self._alicat_abs_psia(reading, barometric_psia)
        if current is None:
            return False
        # Stale sea-level setpoints (14.7) with HLD trap the line off true site
        # baro on open fittings — treat as not idle so vent re-locks to local baro.
        setpoint = reading.alicat.setpoint if reading.alicat is not None else None
        if setpoint is not None and abs(float(setpoint) - float(barometric_psia)) > 0.25:
            return False
        # Held closed near local baro (common on open fittings at altitude).
        if self._alicat_in_hold_mode():
            from app.services.pressure_domain import is_plausible_barometric_psi

            if is_plausible_barometric_psi(current):
                hold_tolerance = (
                    _IDLE_ATMOSPHERE_TOLERANCE_PSIA
                    if self._open_fitting_line()
                    else max(_IDLE_ATMOSPHERE_TOLERANCE_PSIA * 2.0, 1.5)
                )
                if abs(current - barometric_psia) <= hold_tolerance:
                    return True
            return False
        low = barometric_psia - _IDLE_ATMOSPHERE_TOLERANCE_PSIA
        high = (
            barometric_psia + _IDLE_ATMOSPHERE_TOLERANCE_PSIA
            if self._open_fitting_line()
            else barometric_psia + 2.0
        )
        if current < low or current > high:
            return False
        return True

    def _exit_alicat_exhaust(self, target_psia: float = _ATMOSPHERE_PSI) -> None:
        """Leave Alicat EXH so closed-loop setpoints can move the DUT line."""
        for attempt in range(3):
            self.set_pressure(target_psia)
            time.sleep(0.4)
            if not self._alicat_in_exhaust_mode():
                return
            logger.warning(
                '%s: Alicat still EXH after exit attempt %s/3',
                self.port_id.value,
                attempt + 1,
            )

    def _bleed_line_to_atmosphere(
        self,
        *,
        barometric_psia: float,
        timeout_s: float = _IDLE_BLEED_TIMEOUT_S,
    ) -> bool:
        """Bleed a vacuum-held line up through the Alicat atmosphere route."""
        target_psia = barometric_psia
        low_threshold = barometric_psia - _IDLE_ATMOSPHERE_TOLERANCE_PSIA
        reading = self.read_all()
        current = self._alicat_abs_psia(reading, barometric_psia)
        if current is not None and current >= low_threshold:
            return True

        logger.info(
            '%s: Bleeding DUT line from %.2f psia toward atmosphere (target %.2f psia)',
            self.port_id.value,
            current if current is not None else float('nan'),
            target_psia,
        )
        # Recover from vacuum on the atmosphere solenoid route. The historical
        # test-route bleed could not raise an installed DUT line from ~1.5 psia.
        if not self.daq.set_solenoid_safe():
            logger.warning('%s: Failed to engage atmosphere route for bleed', self.port_id.value)
            return False
        self.daq.reset_filter()
        self._exit_alicat_exhaust(target_psia)
        self.alicat.set_ramp_rate(8.0)
        if not self.set_pressure(target_psia):
            logger.warning('%s: Failed to command atmosphere bleed setpoint', self.port_id.value)
            return False

        start = time.perf_counter()
        while time.perf_counter() - start <= timeout_s:
            reading = self.read_all()
            current = self._alicat_abs_psia(reading, barometric_psia)
            if current is not None and current >= low_threshold:
                logger.info(
                    '%s: DUT line reached %.2f psia after bleed',
                    self.port_id.value,
                    current,
                )
                return True
            time.sleep(0.5)

        reading = self.read_all()
        current = self._alicat_abs_psia(reading, barometric_psia)
        logger.warning(
            '%s: Timed out bleeding to atmosphere (still %.2f psia)',
            self.port_id.value,
            current if current is not None else float('nan'),
        )
        return current is not None and current >= low_threshold

    def _lock_idle_at_atmosphere(
        self,
        barometric_psia: float,
        *,
        command_pressure: bool = True,
    ) -> bool:
        """Leave the DUT line at atmosphere without pulling vacuum.

        Uses the atmosphere solenoid route and closed-loop pressure toward
        local barometric PSI. Do **not** use Alicat EXH here — on this stand EXH
        pulls the line toward vacuum even on the atmosphere route.
        """
        self.daq.set_solenoid_safe()
        self.daq.reset_filter()

        reading = self.read_all()
        current = self._alicat_abs_psia(reading, barometric_psia)
        low_threshold = barometric_psia - _IDLE_ATMOSPHERE_TOLERANCE_PSIA
        high_threshold = (
            barometric_psia + _IDLE_ATMOSPHERE_TOLERANCE_PSIA
            if self._open_fitting_line()
            else barometric_psia + 2.0
        )
        in_band = (
            current is not None
            and low_threshold <= current <= high_threshold
        )
        in_exh = self._alicat_in_exhaust_mode()
        if in_exh:
            self._exit_alicat_exhaust(barometric_psia)

        setpoint = reading.alicat.setpoint if reading.alicat is not None else None
        setpoint_mismatch = (
            setpoint is None
            or abs(float(setpoint) - float(barometric_psia)) > 0.25
        )
        # Always re-command site baro when SP is stale (e.g. leftover 14.7), even
        # if measured pressure already sits near local atmosphere.
        need_pressure_command = command_pressure and (
            in_exh or not in_band or setpoint_mismatch
        )
        if need_pressure_command:
            self.alicat.cancel_hold()
            self.alicat.set_ramp_rate(8.0)
            if not self.set_pressure(barometric_psia):
                logger.warning(
                    '%s: Failed to command atmosphere idle setpoint',
                    self.port_id.value,
                )
                return False

            start = time.perf_counter()
            while time.perf_counter() - start <= 15.0:
                reading = self.read_all()
                current = self._alicat_abs_psia(reading, barometric_psia)
                if current is not None and current >= low_threshold:
                    break
                time.sleep(0.5)

        if self._alicat_in_hold_mode():
            ok = True
        else:
            ok = self.alicat.hold_valve(closed=True)
        try:
            self.refresh_alicat()
        except Exception:
            pass
        reading = self.read_all()
        current = self._alicat_abs_psia(reading, barometric_psia)
        logger.info(
            '%s: Idle atmosphere (DIO=atmosphere, hold closed) P=%.2f psia',
            self.port_id.value,
            current if current is not None else float('nan'),
        )
        return ok

    def _vent_to_atmosphere_via_setpoint(
        self,
        *,
        bleed_installed_dut: bool = True,
        timeout_s: float = _IDLE_BLEED_TIMEOUT_S,
    ) -> bool:
        """Vent the port to atmosphere (safe idle state).

        With a DUT installed the line can remain at vacuum while the exhaust
        solenoid is already on atmosphere. Bleed through the Alicat test route
        when needed, then lock the line near barometric pressure on the
        atmosphere solenoid route. Do **not** use Alicat EXH here — EXH pulls
        installed DUT lines back to vacuum on this stand.

        When the line is already near barometric pressure (typical after a
        clean shutdown), this is a no-op so operators do not see ports
        pressurize or pull vacuum on application startup.
        """
        # Always target configured site baro (e.g. 13.49 Idaho). Inferring from
        # Alicat P−gauge / short frames recreates sea-level ~14.7 and parks the
        # HLD line above true atmosphere on open-fitting stands.
        barometric_psia = self._configured_barometric_psia()

        if self.is_at_atmospheric_idle(barometric_psia):
            reading = self.read_all()
            current = self._alicat_abs_psia(reading, barometric_psia)
            logger.info(
                '%s: Already at atmospheric idle (%.2f psia) — no vent action',
                self.port_id.value,
                current if current is not None else float('nan'),
            )
            return True

        reading = self.read_all()
        current = self._alicat_abs_psia(reading, barometric_psia)
        low_threshold = barometric_psia - _IDLE_ATMOSPHERE_TOLERANCE_PSIA
        high_threshold = (
            barometric_psia + _IDLE_ATMOSPHERE_TOLERANCE_PSIA
            if self._open_fitting_line()
            else barometric_psia + 2.0
        )
        if current is not None and low_threshold <= current <= high_threshold:
            logger.info(
                '%s: Near atmosphere (%.2f psia) — idle lock to site baro %.2f',
                self.port_id.value,
                current,
                barometric_psia,
            )
            # command_pressure=True so a stale 14.7 setpoint is rewritten to
            # local_barometric_psi even when P already looks near atm.
            return self._lock_idle_at_atmosphere(
                barometric_psia,
                command_pressure=True,
            )

        if bleed_installed_dut:
            try:
                self._bleed_line_to_atmosphere(
                    barometric_psia=barometric_psia,
                    timeout_s=timeout_s,
                )
            except Exception as exc:
                logger.warning(
                    '%s: Atmosphere bleed failed (continuing to idle lock): %s',
                    self.port_id.value,
                    exc,
                )

        return self._lock_idle_at_atmosphere(barometric_psia)

    def vent_to_atmosphere(
        self,
        *,
        bleed_installed_dut: bool = True,
        timeout_s: float = _IDLE_BLEED_TIMEOUT_S,
    ) -> bool:
        """Vent the port to atmosphere (safe idle).

        Prefer closed-loop setpoint recovery. On this Idaho stand Alicat EXH is
        plumbed to vacuum and never equalizes to baro — using EXH as the primary
        vent path hung QualityCal/connect for ~90s and left the controller at
        ~0 PSIA. Setpoint bleed/lock recovers in a couple of seconds.
        """
        return self._vent_to_atmosphere_via_setpoint(
            bleed_installed_dut=bleed_installed_dut,
            timeout_s=timeout_s,
        )

    def prepare_vacuum_route_for_test(self, barometric_psi: float = _ATMOSPHERE_PSI) -> bool:
        """Vent on atmosphere, then route to vacuum for test cycling (transducer-guarded)."""
        self.vent_to_atmosphere()
        if self._transducer_installed:
            transducer = self.daq.read_transducer()
            if transducer is not None and transducer.pressure is not None:
                from app.services.measurement_source import _transducer_pressure_abs_psi

                tr_reading = PortReading(transducer=transducer, alicat=self.get_cached_alicat())
                self._normalize_transducer_reference(tr_reading)
                transducer_psi = _transducer_pressure_abs_psi(tr_reading, barometric_psi)
                if transducer_psi is not None and transducer_psi > barometric_psi + 3.0:
                    logger.warning(
                        "%s: Vacuum prep with elevated line pressure %.2f psia (proceeding)",
                        self.port_id.value,
                        transducer_psi,
                    )
        if not self.daq.set_solenoid(to_vacuum=True):
            return False
        self.daq.reset_filter()
        return True
    
    def disconnect(self, *, restore_safe_state: bool = True) -> None:
        """Disconnect all hardware.

        When ``restore_safe_state`` is True (default), vent to atmosphere and
        lock the Alicat valve before releasing the serial port.

        When ``restore_safe_state`` is False, leave solenoid routing unchanged so
        the physical line stays on vacuum after the app exits (leak-down tests).
        """
        if restore_safe_state:
            try:
                self.vent_to_atmosphere()
            except Exception as exc:
                logger.warning(
                    '%s: Safe vent on disconnect failed: %s',
                    self.port_id.value,
                    exc,
                )
                try:
                    self.daq.set_solenoid_safe()
                    self.alicat.hold_valve(closed=True)
                except Exception:
                    pass

        self.daq.cleanup(preserve_solenoid_state=not restore_safe_state)
        self.alicat.disconnect()
        
        logger.info(f"Port {self.port_id.value}: Disconnected")
    
    def get_status(self) -> Dict[str, Any]:
        """Get combined status of all hardware."""
        return {
            "port_id": self.port_id.value,
            "daq": self.daq.get_status(),
            "alicat": self.alicat.get_status(),
        }


class PortManager:
    """Manages test ports (A and B)."""
    
    def __init__(self, config: Dict[str, Any]):
        """Initialize port manager."""
        self.config = config
        self.ports: Dict[PortId, Port] = {}
        self._polling = False
        self._poll_thread: Optional[threading.Thread] = None
        timing_cfg = config.get('timing', {})
        self._poll_interval_ms = timing_cfg.get('hardware_poll_interval_ms', 10)
        legacy_divisor = max(1, int(timing_cfg.get('alicat_poll_divisor', 10)))
        self._alicat_poll_divisor_normal = max(
            1, int(timing_cfg.get('alicat_poll_divisor_normal', legacy_divisor))
        )
        self._alicat_poll_divisor_precision = max(
            1, int(timing_cfg.get('alicat_poll_divisor_precision', self._alicat_poll_divisor_normal))
        )
        self._labjack_poll_divisor_sibling = max(
            1, int(timing_cfg.get('labjack_poll_divisor_sibling', self._alicat_poll_divisor_normal))
        )
        self._poll_interval_ms_precision = int(timing_cfg.get('hardware_poll_interval_ms_precision', 0))
        self._alicat_background_enabled = bool(
            timing_cfg.get('alicat_background_polling_enabled', False)
        )
        self._alicat_background_hz = max(
            1.0, float(timing_cfg.get('alicat_background_poll_hz', 120.0))
        )
        self._alicat_background_thread: Optional[threading.Thread] = None
        self._alicat_background_stop = threading.Event()
        self._alicat_background_metrics_lock = threading.Lock()
        self._alicat_background_started_s = 0.0
        self._alicat_background_cycles = 0
        self._alicat_background_successes: Dict[PortId, int] = {}
        self._alicat_background_failures: Dict[PortId, int] = {}
        self._poll_callback: Optional[Callable[[Dict[PortId, PortReading]], None]] = None
        self._poll_policy_lock = threading.Lock()
        self._alicat_poll_divisors: Dict[PortId, int] = {}
        self._alicat_refresh_countdown: Dict[PortId, int] = {}
        self._precision_owner: Optional[PortId] = None
        self._labjack_sibling_countdown: Dict[PortId, int] = {}
        self._serial_busy_ports: set[PortId] = set()
        self._last_poll_readings: Dict[PortId, PortReading] = {}
        self._hardware_ready = False

        logger.info("PortManager initialized")

    @property
    def is_hardware_ready(self) -> bool:
        """True after start_polling(); live reads use poll_once() on the GUI thread."""
        return self._hardware_ready
    
    def initialize_ports(self) -> bool:
        """Initialize all configured ports."""
        labjack_config = self.config.get('hardware', {}).get('labjack', {})
        alicat_config = self.config.get('hardware', {}).get('alicat', {})

        success = True

        def build_labjack_config(port_key: str) -> Dict[str, Any]:
            # Start with all top-level (non-port) keys from hardware.labjack
            base = {
                key: value
                for key, value in labjack_config.items()
                if key not in {'port_a', 'port_b'}
            }
            # Overlay port-specific keys
            return {**base, **labjack_config.get(port_key, {})}

        def build_alicat_config(port_key: str) -> Dict[str, Any]:
            port_config = alicat_config.get(port_key, {})
            base_config = {
                key: value
                for key, value in alicat_config.items()
                if key not in {'port_a', 'port_b'}
            }
            return {**base_config, **port_config}

        solenoid_config = self.config.get("hardware", {}).get("solenoid", {})

        # Initialize Port A
        if 'port_a' in labjack_config and is_port_installed(self.config, 'port_a'):
            port_a = Port(
                port_id=PortId.PORT_A,
                labjack_config=build_labjack_config('port_a'),
                alicat_config=build_alicat_config('port_a'),
                solenoid_config=solenoid_config,
            )
            self.ports[PortId.PORT_A] = port_a

        # Initialize Port B
        if 'port_b' in labjack_config and is_port_installed(self.config, 'port_b'):
            port_b = Port(
                port_id=PortId.PORT_B,
                labjack_config=build_labjack_config('port_b'),
                alicat_config=build_alicat_config('port_b'),
                solenoid_config=solenoid_config,
            )
            self.ports[PortId.PORT_B] = port_b
        
        logger.info(f"PortManager: {len(self.ports)} ports initialized")
        with self._poll_policy_lock:
            for port_id in self.ports.keys():
                self._alicat_poll_divisors[port_id] = self._alicat_poll_divisor_normal
                self._alicat_refresh_countdown[port_id] = 0
        return success
    
    def connect_all(self, *, safe_idle_on_connect: bool = True) -> bool:
        """Connect to hardware for all ports.

        When an Alicat connection fails on its configured COM port, auto-
        discovery is attempted: available serial ports are probed for a
        responding Alicat at the expected address.  If found, the port's
        Alicat controller is updated and the connection retried.

        When ``safe_idle_on_connect`` is True (default), each connected port
        is checked for atmospheric idle. Ports already near barometric
        pressure are left alone; only vacuum or mis-routed lines are recovered.
        """
        import time

        success = True
        overall_start = time.perf_counter()
        for port_id, port in self.ports.items():
            port_start = time.perf_counter()
            if not port.connect():
                alicat = getattr(port, 'alicat', None)
                if alicat is None or not alicat.hardware_available() or alicat._is_connected:
                    logger.error(f"PortManager: Failed to connect {port_id.value}")
                    success = False
                else:
                    discovered = self._discover_alicat_port(port)
                    if discovered and discovered != alicat.com_port:
                        logger.info(
                            'PortManager: Auto-discovered %s Alicat on %s (was %s)',
                            port_id.value,
                            discovered,
                            alicat.com_port,
                        )
                        alicat.com_port = discovered
                        if not alicat.connect():
                            logger.error(f"PortManager: Failed to connect {port_id.value}")
                            success = False
                    else:
                        logger.error(f"PortManager: Failed to connect {port_id.value}")
                        success = False
            logger.info(
                "PortManager: %s connect completed in %.3fs",
                port_id.value,
                time.perf_counter() - port_start,
            )

        logger.info(
            'PortManager: connect_all finished in %.3fs (success=%s)',
            time.perf_counter() - overall_start,
            success,
        )

        if safe_idle_on_connect:
            for port_id, port in self.ports.items():
                try:
                    port.refresh_alicat()
                    if port.is_at_atmospheric_idle():
                        logger.info(
                            'PortManager: %s already at atmospheric idle on connect',
                            port_id.value,
                        )
                        continue
                    port.vent_to_atmosphere()
                    logger.info('PortManager: %s safe idle (atmosphere hold)', port_id.value)
                except Exception as exc:
                    logger.warning(
                        'PortManager: %s safe idle vent on connect failed: %s',
                        port_id.value,
                        exc,
                    )

        return success

    def _discover_alicat_port(self, port: Port) -> Optional[str]:
        """Scan available serial ports for a responding Alicat at the expected address."""
        available = AlicatController.list_available_ports()
        available_ports = [p['device'] for p in available]
        if not available_ports:
            return None

        alicat = port.alicat
        for candidate in available_ports:
            if candidate == alicat.com_port:
                continue
            probe_cfg = {
                **alicat.config,
                'com_port': candidate,
                'auto_configure': False,
                'auto_tare_on_connect': False,
                'command_retries': 0,
                'response_read_attempts': 2,
            }
            probe = AlicatController(probe_cfg)
            try:
                if not probe.connect(max_retries=1):
                    continue
                reading = probe.read_status()
                if reading is not None:
                    return candidate
            except Exception as exc:
                logger.debug(
                    'Alicat discovery probe failed on %s address=%s: %s',
                    candidate,
                    alicat.address,
                    exc,
                )
            finally:
                try:
                    probe.disconnect()
                except Exception:
                    pass
        return None
    
    def get_port(self, port_id: PortId | str) -> Optional[Port]:
        """Get a specific port by ID."""
        if isinstance(port_id, str):
            try:
                port_id = PortId(port_id)
            except ValueError:
                return None
        return self.ports.get(port_id)
    
    def read_all_ports(self) -> Dict[PortId, PortReading]:
        """Read all sensors from all ports."""
        readings = {}
        for port_id, port in self.ports.items():
            readings[port_id] = port.read_all()
        return readings
    
    def disconnect_all(self, *, restore_safe_state: bool = True) -> None:
        """Disconnect all ports."""
        self._stop_alicat_background_polling()
        ports = list(self.ports.items())
        if restore_safe_state:
            for port_id, port in ports:
                try:
                    port.vent_to_atmosphere()
                    logger.info('PortManager: %s safe idle before disconnect', port_id.value)
                except Exception as exc:
                    logger.warning(
                        'PortManager: %s safe vent before disconnect failed: %s',
                        port_id.value,
                        exc,
                    )
        for port_id, port in ports:
            port.disconnect(restore_safe_state=False)
        self.ports.clear()
        logger.info("PortManager: All ports disconnected")
    
    def get_all_status(self) -> Dict[str, Any]:
        """Get status of all ports."""
        return {
            port_id.value: port.get_status()
            for port_id, port in self.ports.items()
        }
    
    def set_poll_callback(self, callback: Callable[[Dict[PortId, PortReading]], None]) -> None:
        """Set callback function to be called with readings on each poll."""
        self._poll_callback = callback

    def set_alicat_poll_divisor(self, port_id: PortId | str, divisor: int) -> bool:
        """Set Alicat poll divisor for a single port at runtime."""
        normalized = self._normalize_port_id(port_id)
        if normalized is None:
            return False
        divisor_val = max(1, int(divisor))
        with self._poll_policy_lock:
            self._alicat_poll_divisors[normalized] = divisor_val
            # Apply speed increases immediately.
            self._alicat_refresh_countdown[normalized] = 0
        logger.info(
            'PortManager: %s Alicat poll divisor set to %d',
            normalized.value,
            divisor_val,
        )
        return True

    def set_alicat_poll_profile(self, precision_port: Optional[PortId | str]) -> None:
        """
        Apply precision polling profile for Alicat serial and LabJack transducer.

        When a precision port is active:
        - precision owner: Alicat divisor=precision, LabJack every cycle (no DIO)
        - sibling port(s): Alicat divisor=normal, LabJack every sibling divisor cycles
        """
        precision_id = self._normalize_port_id(precision_port) if precision_port is not None else None
        with self._poll_policy_lock:
            self._precision_owner = precision_id
            for port_id in self.ports.keys():
                if precision_id is not None and port_id == precision_id:
                    self._alicat_poll_divisors[port_id] = self._alicat_poll_divisor_precision
                else:
                    self._alicat_poll_divisors[port_id] = self._alicat_poll_divisor_normal
                # Force immediate refresh after profile change.
                self._alicat_refresh_countdown[port_id] = 0
                self._labjack_sibling_countdown[port_id] = 0
        if precision_id is None:
            logger.info(
                'PortManager: Poll profile normal (alicat_div=%d, labjack every cycle)',
                self._alicat_poll_divisor_normal,
            )
        else:
            logger.info(
                'PortManager: Precision poll owner=%s (alicat precision=%d normal=%d, '
                'labjack sibling_div=%d, interval_ms=%d)',
                precision_id.value,
                self._alicat_poll_divisor_precision,
                self._alicat_poll_divisor_normal,
                self._labjack_poll_divisor_sibling,
                self._poll_interval_ms_precision,
            )

    def get_precision_poll_status(self) -> Dict[str, Any]:
        """Return precision polling profile state for UI/diagnostics."""
        with self._poll_policy_lock:
            owner = self._precision_owner.value if self._precision_owner is not None else None
            return {
                'precision_owner': owner,
                'labjack_poll_divisor_sibling': self._labjack_poll_divisor_sibling,
                'hardware_poll_interval_ms_precision': self._poll_interval_ms_precision,
            }

    def get_alicat_poll_divisors(self) -> Dict[str, int]:
        """Return current per-port Alicat poll divisors."""
        with self._poll_policy_lock:
            return {
                port_id.value: int(self._alicat_poll_divisors.get(port_id, self._alicat_poll_divisor_normal))
                for port_id in self.ports.keys()
            }

    def set_serial_busy(self, port_id: PortId | str, busy: bool) -> None:
        """Mark a port's Alicat serial as owned by a worker (vent/pressurize/test).

        Background Alicat refresh skips busy ports so the GUI never contends for
        the COM lock and stays responsive.
        """
        normalized = self._normalize_port_id(port_id)
        if normalized is None:
            return
        with self._poll_policy_lock:
            if busy:
                self._serial_busy_ports.add(normalized)
            else:
                self._serial_busy_ports.discard(normalized)
    
    def start_polling(self) -> bool:
        """Enable hardware reads (polled on the Qt GUI thread via poll_once)."""
        if not self.ports:
            logger.error("PortManager: No ports initialized, cannot start polling")
            return False

        self._seed_alicat_cache()
        self._start_alicat_background_polling()
        self._hardware_ready = True
        logger.info(
            "PortManager: Live hardware polling enabled (GUI LabJack thread, interval target=%sms)",
            self._poll_interval_ms,
        )
        return True

    def stop_polling(self) -> None:
        """Disable hardware reads."""
        self._hardware_ready = False
        self._stop_alicat_background_polling()
        if self._polling:
            self._polling = False
            if self._poll_thread:
                self._poll_thread.join(timeout=1.0)
                self._poll_thread = None
        with self._poll_policy_lock:
            self._serial_busy_ports.clear()
        logger.info("PortManager: Stopped polling")

    def _start_alicat_background_polling(self) -> bool:
        """Start the independent shared-COM Alicat cache poller when configured."""
        if not self._alicat_background_enabled or not self.ports:
            return False
        thread = self._alicat_background_thread
        if thread is not None and thread.is_alive():
            return True

        self._alicat_background_stop.clear()
        with self._alicat_background_metrics_lock:
            self._alicat_background_started_s = time.perf_counter()
            self._alicat_background_cycles = 0
            self._alicat_background_successes = {port_id: 0 for port_id in self.ports}
            self._alicat_background_failures = {port_id: 0 for port_id in self.ports}
        self._alicat_background_thread = threading.Thread(
            target=self._alicat_background_poll_loop,
            name='alicat-cache-poller',
            daemon=True,
        )
        self._alicat_background_thread.start()
        logger.info(
            'PortManager: Alicat background polling started at %.1f Hz per controller',
            self._alicat_background_hz,
        )
        return True

    def _stop_alicat_background_polling(self) -> None:
        """Stop the Alicat cache poller before disconnecting serial hardware."""
        thread = self._alicat_background_thread
        if thread is None:
            return
        self._alicat_background_stop.set()
        thread.join(timeout=2.0)
        if thread.is_alive():
            logger.warning('PortManager: Alicat background polling did not stop within 2 seconds')
            return
        self._alicat_background_thread = None

    def _alicat_background_poll_loop(self) -> None:
        """Poll every Alicat once per target period and atomically refresh each cache."""
        period_s = 1.0 / self._alicat_background_hz
        next_cycle_s = time.perf_counter()
        last_warning_s = 0.0
        while not self._alicat_background_stop.is_set():
            for port_id, port in list(self.ports.items()):
                if self._alicat_background_stop.is_set():
                    break
                with self._poll_policy_lock:
                    if port_id in self._serial_busy_ports:
                        continue
                ok = False
                try:
                    ok = bool(port.refresh_alicat())
                except Exception as exc:
                    now = time.perf_counter()
                    if now - last_warning_s >= 5.0:
                        logger.warning(
                            'PortManager: Background Alicat refresh failed for %s: %s',
                            port_id.value,
                            exc,
                        )
                        last_warning_s = now
                with self._alicat_background_metrics_lock:
                    metric = (
                        self._alicat_background_successes
                        if ok
                        else self._alicat_background_failures
                    )
                    metric[port_id] = metric.get(port_id, 0) + 1

            with self._alicat_background_metrics_lock:
                self._alicat_background_cycles += 1

            next_cycle_s += period_s
            now = time.perf_counter()
            if next_cycle_s < now - period_s:
                next_cycle_s = now
            self._alicat_background_stop.wait(max(0.0, next_cycle_s - now))

    def is_alicat_background_polling(self) -> bool:
        """True while the independent Alicat cache thread is alive."""
        thread = self._alicat_background_thread
        return bool(thread is not None and thread.is_alive())

    def get_alicat_background_poll_status(self) -> Dict[str, Any]:
        """Return target and achieved per-controller background polling rates."""
        now = time.perf_counter()
        with self._alicat_background_metrics_lock:
            elapsed_s = max(0.0, now - self._alicat_background_started_s)
            successes = dict(self._alicat_background_successes)
            failures = dict(self._alicat_background_failures)
            cycles = int(self._alicat_background_cycles)
        return {
            'enabled': self._alicat_background_enabled,
            'running': self.is_alicat_background_polling(),
            'target_hz_per_controller': self._alicat_background_hz,
            'elapsed_s': elapsed_s,
            'cycles': cycles,
            'ports': {
                port_id.value: {
                    'successes': int(successes.get(port_id, 0)),
                    'failures': int(failures.get(port_id, 0)),
                    'achieved_hz': (
                        float(successes.get(port_id, 0)) / elapsed_s if elapsed_s > 0 else 0.0
                    ),
                }
                for port_id in self.ports
            },
        }

    def _seed_alicat_cache(self) -> None:
        """Prime Alicat caches so the first GUI poll has serial data."""
        for port_id, port in self.ports.items():
            try:
                port.refresh_alicat()
            except Exception:
                pass
            with self._poll_policy_lock:
                divisor = int(self._alicat_poll_divisors.get(port_id, self._alicat_poll_divisor_normal))
                self._alicat_refresh_countdown[port_id] = max(0, divisor - 1)

    def poll_once(self, *, labjack_only: bool = False) -> Dict[PortId, PortReading]:
        """Read all ports once. Must run on the Qt main thread for reliable UI updates.

        When ``labjack_only`` is True, skip Alicat serial I/O (transducer + switch only).
        Use this while a background test thread owns the Alicat lock so the UI
        timer is not blocked for hundreds of milliseconds.
        """
        if not self._hardware_ready or not self.ports:
            return {}
        try:
            return self._collect_poll_readings(labjack_only=labjack_only)
        except Exception as exc:
            logger.error("PortManager: poll_once failed: %s", exc, exc_info=True)
            return {}

    def _collect_poll_readings(self, *, labjack_only: bool = False) -> Dict[PortId, PortReading]:
        """Single poll cycle: refresh Alicat when due, then read LabJack (+ cached Alicat)."""
        if not labjack_only and not self.is_alicat_background_polling():
            for port_id, port in self.ports.items():
                should_refresh = False
                with self._poll_policy_lock:
                    if port_id in self._serial_busy_ports:
                        continue
                    remaining = int(self._alicat_refresh_countdown.get(port_id, 0))
                    if remaining <= 0:
                        should_refresh = True
                        divisor = int(
                            self._alicat_poll_divisors.get(port_id, self._alicat_poll_divisor_normal)
                        )
                        self._alicat_refresh_countdown[port_id] = max(0, divisor - 1)
                    else:
                        self._alicat_refresh_countdown[port_id] = remaining - 1
                if should_refresh:
                    try:
                        port.refresh_alicat()
                    except Exception as exc:
                        logger.warning(
                            "PortManager: Alicat refresh failed for %s: %s",
                            port_id.value,
                            exc,
                        )

        readings: Dict[PortId, PortReading] = {}
        for port_id, port in self.ports.items():
            readings[port_id] = self._poll_reading(port_id, port)
        return readings
    
    def _poll_reading(self, port_id: PortId, port: Port) -> PortReading:
        """Read one port according to the active precision poll profile."""
        with self._poll_policy_lock:
            owner = self._precision_owner

        if owner is None:
            reading = port.read_fast()
        elif port_id == owner:
            reading = port.read_precision_fast()
        else:
            # Always read LabJack (transducer + switch); Alicat stays on the
            # per-port refresh divisor in _poll_loop. Reusing a cached PortReading
            # here froze the main UI pressure display on the non-precision port.
            reading = port.read_fast()

        self._last_poll_readings[port_id] = reading
        return reading

    def start_background_polling(self) -> bool:
        """Optional legacy background poll thread (not used for live UI)."""
        if self._polling:
            return False
        if not self.ports:
            return False
        self._polling = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        return True

    def _poll_loop(self) -> None:
        """Legacy background poll loop; live UI uses poll_once() on the GUI thread."""
        import time

        self._seed_alicat_cache()
        while self._polling:
            start_time = time.perf_counter()
            with self._poll_policy_lock:
                precision_active = self._precision_owner is not None
            interval_ms = (
                self._poll_interval_ms_precision
                if precision_active
                else self._poll_interval_ms
            )
            if precision_active and interval_ms <= 0:
                interval_ms = self._poll_interval_ms
            interval_s = max(0.001, interval_ms / 1000.0)

            try:
                readings = self._collect_poll_readings()
                if self._poll_callback and readings:
                    self._poll_callback(readings)
            except Exception as e:
                logger.error("PortManager: Polling error: %s", e)

            elapsed = time.perf_counter() - start_time
            sleep_time = max(0.0, interval_s - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)

    @staticmethod
    def _normalize_port_id(port_id: PortId | str | None) -> Optional[PortId]:
        if port_id is None:
            return None
        if isinstance(port_id, PortId):
            return port_id
        try:
            return PortId(str(port_id))
        except ValueError:
            return None
