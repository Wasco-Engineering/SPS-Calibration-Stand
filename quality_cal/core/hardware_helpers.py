"""Shared hardware helpers for quality calibration runners."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from app.hardware.port import Port, PortReading

logger = logging.getLogger(__name__)

# Alicat S0 / exhaust leaves EXH mode; use a small positive setpoint for vacuum pulls.
# Transducer below this absolute pressure means the vacuum line is active (not atmosphere).
VACUUM_LINE_MAX_PSIA = 5.0
MIN_VACUUM_SETPOINT_PSIA = 0.05
VACUUM_PRIME_SETPOINT_PSIA = 0.2
VACUUM_STALL_TIMEOUT_S = 45.0
PRESSURE_STALL_TIMEOUT_S = 60.0
HIGH_PRESSURE_MIN_TARGET_PSIA = 30.0
# Last-resort sea-level default only. Prefer Port._configured_barometric_psia()
# (quality_cal / stinger local_barometric_psi, e.g. 13.49 in Idaho).
NOMINAL_BAROMETRIC_PSIA = 14.7


@dataclass(slots=True)
class StabilizedReading:
    elapsed_s: float
    alicat_psia: Optional[float]
    transducer_psia: Optional[float]
    barometric_psia: float


def port_configured_barometric_psia(port: Optional[Port] = None) -> float:
    """Site baro from port/labjack config; never invent sea-level when configured."""
    if port is not None:
        getter = getattr(port, '_configured_barometric_psia', None)
        if callable(getter):
            try:
                return float(getter())
            except (TypeError, ValueError):
                pass
        labjack_cfg = getattr(port, '_labjack_config', None) or {}
        raw = labjack_cfg.get('local_barometric_psi')
        if raw is not None:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                value = float('nan')
            if value == value and value > 0.0:
                return value
    return NOMINAL_BAROMETRIC_PSIA


def infer_barometric_psia(
    reading: Optional[PortReading],
    fallback_barometric_psia: float = NOMINAL_BAROMETRIC_PSIA,
) -> float:
    if reading is None or reading.alicat is None:
        return float(fallback_barometric_psia)
    if reading.alicat.barometric_pressure is not None:
        return float(reading.alicat.barometric_pressure)
    if reading.alicat.pressure is not None and reading.alicat.gauge_pressure is not None:
        inferred = float(reading.alicat.pressure - reading.alicat.gauge_pressure)
        # gauge_pressure is often the setpoint index, not true gauge — ignore nonsense baro.
        if 12.5 <= inferred <= 16.5:
            return inferred
    return float(fallback_barometric_psia)


def alicat_abs_psia(
    reading: Optional[PortReading],
    fallback_barometric_psia: float = NOMINAL_BAROMETRIC_PSIA,
) -> Optional[float]:
    if reading is None or reading.alicat is None:
        return None
    if reading.alicat.pressure is not None:
        return float(reading.alicat.pressure)
    if reading.alicat.gauge_pressure is not None:
        return float(reading.alicat.gauge_pressure + fallback_barometric_psia)
    return None


def transducer_abs_psia(
    reading: Optional[PortReading],
    fallback_barometric_psia: float = NOMINAL_BAROMETRIC_PSIA,
    *,
    use_raw: bool = False,
) -> Optional[float]:
    if reading is None or reading.transducer is None:
        return None
    if use_raw and reading.transducer.pressure_raw is not None:
        value = float(reading.transducer.pressure_raw)
    else:
        value = float(reading.transducer.pressure)
    reference = str(reading.transducer.pressure_reference or "absolute").strip().lower()
    if reference == "gauge":
        return value + fallback_barometric_psia
    return value


def transducer_range_max_psia(port: Optional[Port]) -> float:
    if port is not None:
        return float(getattr(port.daq, "pressure_max", 30.0))
    return 30.0


def transducer_saturated(
    reading: Optional[PortReading],
    barometric_psia: float,
    *,
    port: Optional[Port] = None,
) -> bool:
    """True when the 30 PSIA (etc.) transducer is pegged at the top of its range — expected above ~30 psia."""
    transducer = transducer_abs_psia(reading, barometric_psia, use_raw=True)
    if transducer is None:
        return False
    return transducer >= (transducer_range_max_psia(port) - 0.25)


def feedback_pressure_psia(
    reading: Optional[PortReading],
    *,
    target_psia: float,
    barometric_psia: float,
    route: str,
    use_raw_transducer: bool = False,
    port: Optional[Port] = None,
) -> Optional[float]:
    """Pressure reading used for closed-loop settle.

    Above the transducer span (~30 psia) or on the pressure route, always use Alicat —
    the transducer is expected to saturate and must not drive settle.
    """
    alicat = alicat_abs_psia(reading, barometric_psia)
    if port is not None and not bool(
        getattr(port, '_labjack_config', {}).get('transducer_installed', True)
    ):
        return alicat
    transducer = transducer_abs_psia(
        reading,
        barometric_psia,
        use_raw=use_raw_transducer,
    )
    transducer_max = transducer_range_max_psia(port)
    if route == "pressure" or transducer is None:
        return alicat
    if target_psia > (transducer_max - 0.5) or transducer_saturated(
        reading,
        barometric_psia,
        port=port,
    ):
        return alicat
    # Deep vacuum only: Alicat line reads atmospheric bleed; transducer tracks the DUT.
    if target_psia <= VACUUM_LINE_MAX_PSIA:
        return transducer
    if alicat is None:
        return transducer
    if abs(alicat - target_psia) > abs(transducer - target_psia):
        return transducer
    return alicat


def port_on_vacuum(port: Port, barometric_psia: float) -> bool:
    """True when the transducer shows real vacuum (vacuum line active)."""
    return port_on_vacuum_from_reading(port.read_all(), barometric_psia)


def needs_atmosphere_vent(reading: Optional[PortReading], barometric_psia: float) -> bool:
    """Vent only when the transducer confirms high pressure (ignore Alicat bleed on vacuum)."""
    if port_on_vacuum_from_reading(reading, barometric_psia):
        return False
    if not transducer_shows_high_pressure(reading, barometric_psia):
        return False
    transducer = transducer_abs_psia(reading, barometric_psia, use_raw=True)
    return transducer is not None and transducer > (barometric_psia + 1.0)


def transducer_shows_high_pressure(
    reading: Optional[PortReading],
    barometric_psia: float,
    *,
    margin_psia: float = 2.0,
) -> bool:
    """True when the transducer indicates the DUT is near atmosphere (not on vacuum)."""
    transducer = transducer_abs_psia(reading, barometric_psia, use_raw=True)
    return transducer is not None and transducer > (barometric_psia - margin_psia)


def transducer_trusted_for_safety(
    reading: Optional[PortReading],
    barometric_psia: float,
    *,
    port: Port,
) -> bool:
    """False when the transducer is pegged (expected >30 psia) or disagrees strongly with Alicat."""
    if transducer_saturated(reading, barometric_psia, port=port):
        return False
    transducer = transducer_abs_psia(reading, barometric_psia, use_raw=True)
    if transducer is None:
        return False
    alicat = alicat_abs_psia(reading, barometric_psia)
    if alicat is not None and transducer > (alicat + 5.0):
        return False
    return True


def set_vacuum_solenoid(port: Port, barometric_psia: float) -> bool:
    """Engage vacuum solenoid using transducer for pump protection.

    On the vacuum line the Alicat often reads atmospheric bleed (~14 psia) while the
    transducer tracks the DUT — ``Port.set_solenoid(True)`` would refuse and force a
    pressure-route prime that vents to atmosphere.

    When the transducer is pegged at its range ceiling (common on 30 PSIA sensors left
  at atmosphere) fall back to Alicat for the safety check.
    """
    solenoid_cfg = getattr(port, "_solenoid_config", {}) or {}
    threshold_psi = float(solenoid_cfg.get("safe_vacuum_switch_threshold_psi", 2.0))
    reading = port.read_all()
    transducer = transducer_abs_psia(reading, barometric_psia, use_raw=True)
    if transducer is not None and transducer_trusted_for_safety(
        reading,
        barometric_psia,
        port=port,
    ):
        reference_baro = max(barometric_psia, NOMINAL_BAROMETRIC_PSIA - 1.0)
        safe_limit = reference_baro + threshold_psi
        safe = transducer <= safe_limit
        if not safe:
            logger.warning(
                "%s: Refusing vacuum — transducer %.2f psia above safe %.2f psia",
                port.port_id.value,
                transducer,
                safe_limit,
            )
            return False
        result = port.daq.set_solenoid(True)
        if result:
            port.daq.reset_filter()
        return result
    if transducer is not None:
        alicat = alicat_abs_psia(reading, barometric_psia)
        logger.info(
            "%s: Transducer %.2f psia not trusted for vacuum safety (Alicat %.2f) — using Alicat",
            port.port_id.value,
            transducer,
            alicat if alicat is not None else float("nan"),
        )
    return port.set_solenoid(to_vacuum=True)


def port_on_vacuum_from_reading(reading: Optional[PortReading], barometric_psia: float) -> bool:
    """True when the transducer shows real vacuum (not bleed-through near atmosphere)."""
    transducer = transducer_abs_psia(reading, barometric_psia, use_raw=True)
    threshold = min(VACUUM_LINE_MAX_PSIA, barometric_psia - 5.0)
    return transducer is not None and transducer < threshold


def alicat_in_exhaust_mode(port: Port) -> bool:
    reading = port.alicat.read_status()
    if reading is None or not reading.raw_response:
        return False
    return "EXH" in reading.raw_response.upper()


def effective_alicat_setpoint_psia(target_psia: float, barometric_psia: float) -> float:
    if target_psia < (barometric_psia - 0.5):
        return max(target_psia, MIN_VACUUM_SETPOINT_PSIA)
    return target_psia


def leave_alicat_exhaust(port: Port) -> None:
    """Exit Alicat EXH so closed-loop setpoints take effect on the vacuum line."""
    for attempt in range(3):
        port.alicat.cancel_hold()
        time.sleep(0.15)
        port.alicat.set_pressure(VACUUM_PRIME_SETPOINT_PSIA)
        time.sleep(0.4)
        if not alicat_in_exhaust_mode(port):
            port.alicat.cancel_hold()
            return
        logger.warning(
            "%s: Alicat still EXH after exit attempt %s/3",
            port.port_id.value,
            attempt + 1,
        )
    logger.error(
        "%s: Alicat remains in EXH — vacuum setpoints may not move the line",
        port.port_id.value,
    )


def ensure_port_at_atmosphere(
    port: Port,
    *,
    timeout_s: float = 120.0,
    cancel_event: Optional[threading.Event] = None,
) -> bool:
    """Vent and wait until the Alicat shows near-atmosphere pressure."""
    if cancel_event is not None and cancel_event.is_set():
        return False
    try:
        return port.vent_to_atmosphere(bleed_installed_dut=True, timeout_s=timeout_s)
    except Exception as exc:
        logger.warning('Failed to vent %s to atmosphere: %s', port.port_id.value, exc)
        return False


def prime_vacuum_route(
    port: Port,
    *,
    barometric_psia: float,
    cancel_event: threading.Event,
) -> bool:
    """Match scripts/vacuum_pull_test: atmosphere + safe SP, then vacuum solenoid (no long waits)."""
    if cancel_event.is_set():
        return False

    latest = port.read_all()
    if not port_on_vacuum_from_reading(latest, barometric_psia):
        alicat = alicat_abs_psia(latest, barometric_psia)
        if alicat is not None and alicat > (barometric_psia + 2.5):
            if not ensure_port_at_atmosphere(port, cancel_event=cancel_event):
                return False

    latest = port.read_all()
    if port_on_vacuum_from_reading(latest, barometric_psia):
        if alicat_in_exhaust_mode(port):
            logger.info("%s: Leaving Alicat EXH (already on vacuum)", port.port_id.value)
            leave_alicat_exhaust(port)
        ok = set_vacuum_solenoid(port, barometric_psia)
        if ok:
            time.sleep(0.5)
        return ok

    if alicat_in_exhaust_mode(port):
        logger.info("%s: Leaving Alicat EXH before vacuum route", port.port_id.value)
        leave_alicat_exhaust(port)

    logger.info("%s: Priming vacuum route (atmosphere, SP=%.2f psia)", port.port_id.value, VACUUM_PRIME_SETPOINT_PSIA)
    port.set_solenoid(to_vacuum=False)
    port.alicat.cancel_hold()
    port.alicat.set_pressure(VACUUM_PRIME_SETPOINT_PSIA)
    time.sleep(2.0)

    ok = set_vacuum_solenoid(port, barometric_psia)
    if not ok:
        logger.warning(
            "%s: Vacuum solenoid blocked — brief atmosphere vent then retry",
            port.port_id.value,
        )
        port.vent_to_atmosphere()
        time.sleep(3.0)
        port.set_solenoid(to_vacuum=False)
        port.alicat.cancel_hold()
        port.alicat.set_pressure(VACUUM_PRIME_SETPOINT_PSIA)
        time.sleep(2.0)
        ok = set_vacuum_solenoid(port, barometric_psia)

    if ok:
        time.sleep(1.0)
        logger.info("%s: Vacuum route primed", port.port_id.value)
    else:
        logger.error("%s: Failed to engage vacuum solenoid after prime", port.port_id.value)
    return ok


def prepare_port_for_target(
    port: Port,
    target_psia: float,
    fallback_barometric_psia: float,
    cancel_event: threading.Event,
    *,
    previous_route: Optional[str] = None,
) -> tuple[bool, str, float]:
    """Select the route safely before commanding the next target."""
    if cancel_event.is_set():
        return False, "cancelled", fallback_barometric_psia

    site_baro = port_configured_barometric_psia(port)
    if abs(float(fallback_barometric_psia) - NOMINAL_BAROMETRIC_PSIA) < 1e-6:
        # Caller still on sea-level default — prefer stand local baro.
        fallback_barometric_psia = site_baro
    else:
        fallback_barometric_psia = float(fallback_barometric_psia)

    latest = port.read_all()
    barometric_psia = infer_barometric_psia(latest, fallback_barometric_psia)
    use_vacuum = target_psia < (barometric_psia - 0.3)
    if not use_vacuum:
        if alicat_in_exhaust_mode(port):
            logger.info(
                "%s: Leaving Alicat EXH before pressure route (target %.3f psia)",
                port.port_id.value,
                target_psia,
            )
            leave_alicat_exhaust(port)
        ok = port.set_solenoid(to_vacuum=False)
        return ok, "pressure", barometric_psia

    if previous_route == "vacuum" or port_on_vacuum_from_reading(latest, barometric_psia):
        if alicat_in_exhaust_mode(port):
            leave_alicat_exhaust(port)
        ok = set_vacuum_solenoid(port, barometric_psia)
        if ok:
            time.sleep(0.2)
        return ok, "vacuum", barometric_psia

    ok = prime_vacuum_route(
        port,
        barometric_psia=barometric_psia,
        cancel_event=cancel_event,
    )
    return ok, "vacuum", barometric_psia


def settle_tolerance_for_target(target_psia: float, base_tolerance_psia: float) -> float:
    """Settle band for routing only — point pass/fail still uses profile pressure_tolerance."""
    if target_psia <= 1.0:
        return max(base_tolerance_psia, 0.25)
    if target_psia <= 5.0:
        return max(base_tolerance_psia, 0.18, target_psia * 0.06)
    if target_psia > HIGH_PRESSURE_MIN_TARGET_PSIA:
        return max(base_tolerance_psia, 0.25)
    return max(base_tolerance_psia, 0.15)


def settle_timeout_for_target(target_psia: float, base_timeout_s: float) -> float:
    """Allow extra time for Alicat to reach high absolute pressures (transducer not used)."""
    if target_psia > HIGH_PRESSURE_MIN_TARGET_PSIA:
        return max(base_timeout_s, 180.0)
    return base_timeout_s


def command_target_pressure(
    port: Port,
    target_psia: float,
    ramp_rate_psi_per_s: float,
    *,
    configure_units: bool = True,
) -> None:
    barometric_psia = infer_barometric_psia(port.read_all())
    commanded = effective_alicat_setpoint_psia(target_psia, barometric_psia)
    if configure_units:
        port.alicat.configure_units_from_ptp("1")
    if ramp_rate_psi_per_s > 0:
        port.alicat.set_ramp_rate(ramp_rate_psi_per_s)
    if alicat_in_exhaust_mode(port):
        logger.info(
            "%s: Alicat in EXH before commanding %.3f psia — exiting exhaust",
            port.port_id.value,
            commanded,
        )
        leave_alicat_exhaust(port)
    port.alicat.cancel_hold()
    if not port.set_pressure(commanded):
        raise RuntimeError(f"Failed to command target pressure {commanded:.3f} psia")
    if commanded != target_psia:
        logger.info(
            "%s: Commanded %.3f psia (profile target %.3f) — Alicat minimum vacuum setpoint",
            port.port_id.value,
            commanded,
            target_psia,
        )


def wait_until_near_target(
    *,
    port: Port,
    target_psia: float,
    tolerance_psia: float,
    hold_s: float,
    timeout_s: float,
    sample_hz: float,
    cancel_event: threading.Event,
    progress_callback: Optional[Callable[[str, Optional[float], Optional[float]], None]],
    route: str = "pressure",
) -> StabilizedReading:
    start = time.perf_counter()
    near_since: Optional[float] = None
    vacuum_stall_since: Optional[float] = None
    pressure_stall_since: Optional[float] = None
    pressure_stall_psia: Optional[float] = None
    last_status_log = start
    last_progress_emit = start
    sample_period_s = max(0.05, 1.0 / max(sample_hz, 0.1))
    progress_emit_period_s = 2.0
    last_alicat: Optional[float] = None
    last_transducer: Optional[float] = None
    last_feedback: Optional[float] = None
    barometric_psia = port_configured_barometric_psia(port)

    while time.perf_counter() - start <= timeout_s:
        if cancel_event.is_set():
            raise RuntimeError("Cancelled")

        reading = port.read_all()
        barometric_psia = infer_barometric_psia(reading, barometric_psia)
        last_alicat = alicat_abs_psia(reading, barometric_psia)
        last_transducer = transducer_abs_psia(reading, barometric_psia)
        feedback = feedback_pressure_psia(
            reading,
            target_psia=target_psia,
            barometric_psia=barometric_psia,
            route=route,
            use_raw_transducer=True,
            port=port,
        )

        if feedback is not None:
            last_feedback = feedback
            error = abs(feedback - target_psia)
            now = time.perf_counter()
            if progress_callback is not None and (
                now - last_progress_emit >= progress_emit_period_s
            ):
                progress_callback(
                    f"Settling at {target_psia:.1f} psia",
                    last_alicat,
                    last_transducer,
                )
                last_progress_emit = now
            elif time.perf_counter() - last_status_log >= 10.0:
                logger.info(
                    "%s: settling target=%.2f psia feedback=%.3f err=%.3f tol=%.3f "
                    "alicat=%s transducer=%s route=%s (%.0fs)",
                    port.port_id.value,
                    target_psia,
                    feedback,
                    abs(feedback - target_psia),
                    tolerance_psia,
                    f"{last_alicat:.3f}" if last_alicat is not None else "n/a",
                    f"{last_transducer:.3f}" if last_transducer is not None else "n/a",
                    route,
                    time.perf_counter() - start,
                )
                last_status_log = time.perf_counter()
            if error <= tolerance_psia:
                if near_since is None:
                    near_since = now
                elif now - near_since >= hold_s:
                    return StabilizedReading(
                        elapsed_s=now - start,
                        alicat_psia=last_alicat,
                        transducer_psia=last_transducer,
                        barometric_psia=barometric_psia,
                    )
            else:
                near_since = None

            if (
                route == "vacuum"
                and target_psia < (barometric_psia - 1.0)
                and feedback > (barometric_psia - 2.0)
            ):
                if vacuum_stall_since is None:
                    vacuum_stall_since = time.perf_counter()
                elif time.perf_counter() - vacuum_stall_since >= VACUUM_STALL_TIMEOUT_S:
                    raise RuntimeError(
                        f"Vacuum not reaching {target_psia:.3f} psia — port still near atmosphere "
                        f"({feedback:.2f} psia on transducer). Check vacuum pump, solenoid routing "
                        f"(DIO), and that the Alicat left EXH mode."
                    )
            else:
                vacuum_stall_since = None

            if (
                route == "pressure"
                and target_psia > HIGH_PRESSURE_MIN_TARGET_PSIA
                and last_alicat is not None
                and abs(last_alicat - target_psia) > 5.0
            ):
                if (
                    pressure_stall_since is None
                    or pressure_stall_psia is None
                    or abs(last_alicat - pressure_stall_psia) > 1.0
                ):
                    pressure_stall_since = now
                    pressure_stall_psia = last_alicat
                elif now - pressure_stall_since >= PRESSURE_STALL_TIMEOUT_S:
                    raise RuntimeError(
                        f"Alicat not reaching {target_psia:.1f} psia — stuck near {last_alicat:.1f} psia. "
                        f"The port transducer saturates above ~{transducer_range_max_psia(port):.0f} psia "
                        f"(expected). Check gas supply, regulator capacity, and that Alicat is not in EXH."
                    )
            else:
                pressure_stall_since = None
                pressure_stall_psia = None

        time.sleep(sample_period_s)

    if target_psia > HIGH_PRESSURE_MIN_TARGET_PSIA:
        raise TimeoutError(
            f"Timed out waiting for Alicat to reach {target_psia:.3f} psia "
            f"(Alicat={last_alicat}, transducer={last_transducer} — transducer pegged above "
            f"~{transducer_range_max_psia(port):.0f} psia is expected). Check supply and EXH mode."
        )
    raise TimeoutError(
        f"Timed out waiting for {target_psia:.3f} psia "
        f"(feedback={last_feedback}, Alicat={last_alicat}, transducer={last_transducer}, route={route})"
    )


def safe_shutdown_port(port: Port) -> None:
    try:
        port.vent_to_atmosphere()
    except Exception as exc:
        logger.warning("Failed to vent port safely: %s", exc)
