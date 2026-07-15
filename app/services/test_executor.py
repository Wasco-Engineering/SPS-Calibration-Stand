"""
Test execution engine for cycling and precision sweep.

Runs in a background thread, driven by the state machine. Reports results
back to the controller via callbacks.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Callable, Dict, Optional

from app.hardware.port import Port, PortReading
from app.services.control_config import parse_control_config
from app.services.measurement_source import (
    get_measurement_settings,
    select_main_pressure_abs_psi,
    select_ui_pressure_abs_psi,
)
from app.services.pressure_domain import (
    resolve_alicat_setpoint_reference_for_test,
    to_absolute_pressure,
    to_alicat_setpoint_psi,
)
from app.services.ptp_service import TestSetup, convert_pressure
from app.services.sweep_primitives import (
    EdgeDetection,
    SpdtDebounceState,
    SweepPassOutcome,
    SweepResult,
    collapse_switch_activated,
    observe_spdt_transition,
)
from app.services.sweep_utils import (
    resolve_cycle_ramp_targets,
    resolve_sweep_bounds,
    resolve_sweep_mode,
)
from app.services.test_protocol import TestEvent, TestFailure, TestFailureCode

logger = logging.getLogger(__name__)


def _config_bool(value: Any, default: bool = False) -> bool:
    """Coerce common YAML/env-style boolean values without making 'false' truthy."""
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


class CyclePhaseRunner:
    """Execute the cycling phase using executor context methods."""

    def __init__(self, ctx: 'TestExecutor') -> None:
        self._ctx = ctx

    def run_pre_approach(self, sweep_mode: str, bounds: tuple[float, float]) -> None:
        """Ramp quickly to just outside the test range before the first cycle.

        This eliminates the slow first-cycle problem where the system had
        to traverse all the way from atmosphere to the test range during the
        first cycling ramp.
        """
        if self._ctx._cancel_event.is_set():
            return
        min_psi, max_psi = bounds
        activation_direction = self._ctx._resolve_activation_sweep_direction()
        baro_psi = self._ctx._get_barometric_psi(self._ctx._port_id)

        if sweep_mode == 'vacuum':
            if activation_direction > 0:
                # Increasing absolute-vacuum switches must start on the deep
                # vacuum/reset side, then sweep upward toward activation.
                pre_approach_target = max(
                    min_psi - self._ctx._precision_prepass_nudge_psi,
                    self._ctx._resolve_hardware_limits_test_reference()[0],
                )
                pre_approach_abs = self._ctx._to_absolute(pre_approach_target)
            elif self._ctx._ptp_limits_use_psia_scale():
                # Decreasing vacuum switches are exercised by pulling down
                # from atmosphere.
                pre_approach_target = baro_psi
                pre_approach_abs = baro_psi
            else:
                pre_approach_target = self._ctx._absolute_to_test_reference(baro_psi)
                pre_approach_abs = baro_psi
        else:
            hw_min_psi, hw_max_psi = self._ctx._resolve_hardware_limits_test_reference()
            overshoot = max((max_psi - min_psi) * (self._ctx._overshoot_pct / 100.0), 0.5)
            _activation_target, pre_approach_target = self._ctx._resolve_cycle_targets(
                sweep_mode,
                min_psi,
                max_psi,
                overshoot,
                hw_min_psi,
                hw_max_psi,
            )
            pre_approach_abs = self._ctx._to_absolute(pre_approach_target)
        pre_approach_rate = self._ctx._fast_rate_psi * self._ctx._pre_approach_rate_multiplier

        logger.info(
            '%s: Pre-approach ramping to %.4f PSI (abs=%.4f) at rate %.4f PSI/s',
            self._ctx._port_id,
            pre_approach_target,
            pre_approach_abs,
            pre_approach_rate,
        )

        if sweep_mode == 'vacuum':
            if not self._ctx._port.vent_to_atmosphere():
                self._ctx._fail(
                    TestFailureCode.ROUTE_FAILURE,
                    f'Failed to vent for pre-approach on {self._ctx._port_id}',
                )

        self._ctx._stage_atmosphere_setpoint_before_route()
        self._ctx._set_active_test_route(sweep_mode, 'pre-approach')

        if not self._ctx._port.alicat.set_ramp_rate(pre_approach_rate):
            self._ctx._fail(
                TestFailureCode.RAMP_RATE_FAILURE,
                f'Failed to set pre-approach ramp rate for {self._ctx._port_id}',
            )
        self._ctx._set_pressure_or_raise(pre_approach_abs)
        self._ctx._port.alicat.cancel_hold()

        # Wait until close to the pre-approach target (generous tolerance)
        if sweep_mode == 'vacuum':
            tolerance = max(1.5, (max_psi - min_psi) * 0.15)
            timeout_cap_s = 3.0
        else:
            tolerance = max(0.15, min(0.35, (max_psi - min_psi) * 0.08))
            timeout_cap_s = self._ctx._edge_timeout_s
        current_pressure, _switch_state = self._ctx._read_pressure_and_switch_state()
        if current_pressure is not None and math.isfinite(current_pressure):
            travel_psi = abs(current_pressure - pre_approach_target)
        else:
            travel_psi = abs(self._ctx._absolute_to_test_reference(baro_psi) - pre_approach_target)
        pre_approach_timeout_s = min(
            self._ctx._edge_timeout_s,
            max(0.5, min(timeout_cap_s, travel_psi / max(pre_approach_rate, 0.1) + 0.5)),
        )

        if not self._ctx._wait_until_near_target(
            target_psi=pre_approach_target,
            timeout_s=pre_approach_timeout_s,
            tolerance_psi=tolerance,
            settle_s=0.1,
        ):
            logger.warning(
                '%s: Pre-approach did not reach target %.4f PSI within %.1fs; proceeding with cycling',
                self._ctx._port_id,
                pre_approach_target,
                pre_approach_timeout_s,
            )

    def run_single_cycle(self, sweep_mode: str, bounds: tuple[float, float]) -> None:
        """Run a single cycle: ramp fast until activation detected, then reverse until deactivation detected."""
        min_psi, max_psi = bounds
        direction = self._ctx._resolve_activation_sweep_direction()
        hw_min_psi, hw_max_psi = self._ctx._resolve_hardware_limits_test_reference()

        overshoot = max((max_psi - min_psi) * (self._ctx._overshoot_pct / 100.0), 0.5)
        full_activation, full_deactivation = self._ctx._resolve_cycle_targets(
            sweep_mode,
            min_psi,
            max_psi,
            overshoot,
            hw_min_psi,
            hw_max_psi,
        )
        target_activation = self._ctx._adaptive_cycle_target(
            'activation',
            full_activation,
            direction,
            bounds,
            (hw_min_psi, hw_max_psi),
        )
        if sweep_mode == 'vacuum':
            target_deactivation = full_deactivation
        else:
            target_deactivation = self._ctx._adaptive_cycle_target(
                'deactivation',
                full_deactivation,
                direction,
                bounds,
                (hw_min_psi, hw_max_psi),
            )
        activation_fallback = full_activation if not math.isclose(target_activation, full_activation, abs_tol=0.05) else None
        deactivation_fallback = full_deactivation if not math.isclose(target_deactivation, full_deactivation, abs_tol=0.05) else None
        deactivation_direction = 1 if target_deactivation > target_activation else -1
        logger.info(
            '%s: Cycle traverse limits activation=%.4f%s deactivation=%.4f%s PSI '
            '(mode=%s, edges detected while ramping)',
            self._ctx._port_id,
            target_activation,
            f' fallback={full_activation:.4f}' if activation_fallback is not None else '',
            target_deactivation,
            f' fallback={full_deactivation:.4f}' if deactivation_fallback is not None else '',
            sweep_mode,
        )

        self._ctx._cycle_debounce_state = SpdtDebounceState()
        if hasattr(self._ctx._port, 'clear_edge_history'):
            self._ctx._port.clear_edge_history()

        self._ctx._set_active_test_route(sweep_mode, 'cycle start')

        # Set fast ramp rate
        if not self._ctx._port.alicat.set_ramp_rate(self._ctx._fast_rate_psi):
            self._ctx._fail(
                TestFailureCode.RAMP_RATE_FAILURE,
                f'Failed to set fast ramp rate for {self._ctx._port_id}',
            )

        if self._ctx._uses_nc_derived_vacuum_window():
            self._run_nc_derived_vacuum_cycle(
                min_psi=min_psi,
                max_psi=max_psi,
                overshoot=overshoot,
                hw_min_psi=hw_min_psi,
                hw_max_psi=hw_max_psi,
            )
            return

        # ---- Phase 1: Ramp toward activation until activation edge detected ----
        self._ctx._prepare_switch_for_cycle_edge(
            sweep_mode=sweep_mode,
            min_psi=min_psi,
            max_psi=max_psi,
            direction=direction,
            edge_type='activation',
            overshoot=overshoot,
            hw_min_psi=hw_min_psi,
            hw_max_psi=hw_max_psi,
        )
        self._ctx._emit_substate('cycling.wait_activation')

        target_activation_abs = self._ctx._to_absolute(target_activation)
        self._ctx._set_pressure_or_raise(target_activation_abs)
        self._ctx._port.alicat.cancel_hold()

        logger.info(
            '%s: Cycle ramp toward activation (target=%.4f PSI)',
            self._ctx._port_id,
            target_activation,
        )

        # Wait for activation edge - check if debounce system commits an activation
        activation_samples_before = len(self._ctx._cycle_activation_samples)
        activation_detected, activation_diagnostic = self._ctx._wait_for_cycle_edge(
            target_psi=target_activation,
            direction=direction,
            edge_type='activation',
            samples_before=activation_samples_before,
            timeout_s=self._ctx._edge_timeout_s,
            fallback_target_psi=activation_fallback,
        )

        if self._ctx._cancel_event.is_set():
            return

        if not activation_detected:
            if activation_diagnostic == 'NO_SWITCH_DETECTED':
                logger.warning(
                    '%s: No switch detected - venting to atmosphere immediately',
                    self._ctx._port_id,
                )
                self._ctx._safe_vent()
                self._ctx._fail(
                    TestFailureCode.NO_SWITCH_DETECTED,
                    f'No switch detected on {self._ctx._port_id} - switch state did not change during pressure ramp',
                )
            else:
                error_msg = f'Activation edge not detected during cycle ramp on {self._ctx._port_id}'
                if activation_diagnostic:
                    error_msg += f': {activation_diagnostic}'
                self._ctx._fail(
                    TestFailureCode.EDGE_NOT_FOUND,
                    error_msg,
                )

        logger.info(
            '%s: Activation edge recorded — reversing toward deactivation target %.4f PSI',
            self._ctx._port_id,
            target_deactivation,
        )

        # ---- Phase 2: Immediately reverse direction and ramp toward deactivation until deactivation edge detected ----
        self._ctx._prepare_switch_for_cycle_edge(
            sweep_mode=sweep_mode,
            min_psi=min_psi,
            max_psi=max_psi,
            direction=direction,
            edge_type='deactivation',
            overshoot=overshoot,
            hw_min_psi=hw_min_psi,
            hw_max_psi=hw_max_psi,
        )
        get_history = getattr(self._ctx._port, 'get_edge_history', None)
        deactivation_port_edges_before = len(get_history()) if callable(get_history) else None
        self._ctx._emit_substate('cycling.wait_deactivation')

        target_deactivation_abs = self._ctx._to_absolute(target_deactivation)
        if not self._ctx._port.alicat.set_ramp_rate(self._ctx._fast_rate_psi):
            self._ctx._fail(
                TestFailureCode.RAMP_RATE_FAILURE,
                f'Failed to set ramp rate for deactivation leg on {self._ctx._port_id}',
            )
        self._ctx._set_pressure_or_raise(target_deactivation_abs)
        self._ctx._port.alicat.cancel_hold()

        logger.info(
            '%s: Cycle fast ramp to deactivation target %.4f PSI, waiting for reset edge',
            self._ctx._port_id,
            target_deactivation,
        )

        # Wait for deactivation edge - check if debounce system commits a deactivation
        deactivation_samples_before = len(self._ctx._cycle_deactivation_samples)
        deactivation_detected, deactivation_diagnostic = self._ctx._wait_for_cycle_edge(
            target_psi=target_deactivation,
            direction=deactivation_direction,
            edge_type='deactivation',
            samples_before=deactivation_samples_before,
            timeout_s=self._ctx._edge_timeout_s,
            fallback_target_psi=deactivation_fallback,
            port_edges_before=deactivation_port_edges_before,
        )

        if self._ctx._cancel_event.is_set():
            return

        if not deactivation_detected:
            if deactivation_diagnostic == 'NO_SWITCH_DETECTED':
                logger.warning(
                    '%s: No switch detected - venting to atmosphere immediately',
                    self._ctx._port_id,
                )
                self._ctx._safe_vent()
                self._ctx._fail(
                    TestFailureCode.NO_SWITCH_DETECTED,
                    f'No switch detected on {self._ctx._port_id} - switch state did not change during pressure ramp',
                )
            else:
                error_msg = f'Deactivation edge not detected during cycle ramp on {self._ctx._port_id}'
                if deactivation_diagnostic:
                    error_msg += f': {deactivation_diagnostic}'
                self._ctx._fail(
                    TestFailureCode.EDGE_NOT_FOUND,
                    error_msg,
                )

    def _run_nc_derived_vacuum_cycle(
        self,
        *,
        min_psi: float,
        max_psi: float,
        overshoot: float,
        hw_min_psi: float,
        hw_max_psi: float,
    ) -> None:
        """Cycle a one-input vacuum switch by traversing upward through both edges.

        These DB9/NC-derived inputs do not map neatly to the generic
        activation-then-reverse traversal.  Reposition below the band before
        each cycle, then sweep upward through both pressure edges.  The stored
        edge names are normalized later by PTP activation direction.
        """
        low_target = max(hw_min_psi, min_psi - overshoot)
        high_target = min(hw_max_psi, max_psi + overshoot)
        logger.info(
            '%s: NC-derived vacuum cycle traverse low=%.4f high=%.4f PSI',
            self._ctx._port_id,
            low_target,
            high_target,
        )

        get_history = getattr(self._ctx._port, 'get_edge_history', None)
        low_edges_before = len(get_history()) if callable(get_history) else None

        self._ctx._emit_substate('cycling.wait_activation')
        self._ctx._set_pressure_or_raise(self._ctx._to_absolute(low_target))
        self._ctx._port.alicat.cancel_hold()
        logger.info(
            '%s: NC-derived vacuum cycle sweep low to %.4f PSI',
            self._ctx._port_id,
            low_target,
        )

        activation_samples_before = len(self._ctx._cycle_activation_samples)
        activation_detected, activation_diagnostic = self._ctx._wait_for_cycle_edge(
            target_psi=low_target,
            direction=-1,
            edge_type='activation',
            samples_before=activation_samples_before,
            timeout_s=self._ctx._edge_timeout_s,
            port_edges_before=low_edges_before,
        )
        if self._ctx._cancel_event.is_set():
            return
        if not activation_detected:
            self._fail_missing_cycle_edge('activation', activation_diagnostic)

        high_edges_before = len(get_history()) if callable(get_history) else None
        self._ctx._emit_substate('cycling.wait_deactivation')
        self._ctx._set_pressure_or_raise(self._ctx._to_absolute(high_target))
        self._ctx._port.alicat.cancel_hold()
        logger.info(
            '%s: NC-derived vacuum cycle sweep high to %.4f PSI',
            self._ctx._port_id,
            high_target,
        )

        deactivation_samples_before = len(self._ctx._cycle_deactivation_samples)
        deactivation_detected, deactivation_diagnostic = self._ctx._wait_for_cycle_edge(
            target_psi=high_target,
            direction=1,
            edge_type='deactivation',
            samples_before=deactivation_samples_before,
            timeout_s=self._ctx._edge_timeout_s,
            port_edges_before=high_edges_before,
        )
        if self._ctx._cancel_event.is_set():
            return
        if not deactivation_detected:
            self._fail_missing_cycle_edge('deactivation', deactivation_diagnostic)

    def _fail_missing_cycle_edge(self, edge_type: str, diagnostic: Optional[str]) -> None:
        if diagnostic == 'NO_SWITCH_DETECTED':
            logger.warning(
                '%s: No switch detected - venting to atmosphere immediately',
                self._ctx._port_id,
            )
            self._ctx._safe_vent()
            self._ctx._fail(
                TestFailureCode.NO_SWITCH_DETECTED,
                f'No switch detected on {self._ctx._port_id} - switch state did not change during pressure ramp',
            )
        error_msg = f'{edge_type.capitalize()} edge not detected during cycle ramp on {self._ctx._port_id}'
        if diagnostic:
            error_msg += f': {diagnostic}'
        self._ctx._fail(TestFailureCode.EDGE_NOT_FOUND, error_msg)


class PrecisionPhaseRunner:
    """Execute the precision phase using executor context methods."""

    def __init__(self, ctx: 'TestExecutor') -> None:
        self._ctx = ctx

    def run_precision_sweep(
        self,
        sweep_mode: str,
        bounds: tuple[float, float],
        skip_atmosphere_gate: bool = False,
    ) -> Optional[SweepResult]:
        min_psi, max_psi = bounds
        atmosphere_psi = self._ctx._determine_atmosphere_psi()

        if min_psi >= max_psi:
            logger.error('%s: Invalid sweep range %.3f to %.3f', self._ctx._port_id, min_psi, max_psi)
            return None

        activation_direction = self._ctx._resolve_activation_sweep_direction()
        if not skip_atmosphere_gate:
            self._run_precision_atmosphere_gate(atmosphere_psi)
        else:
            logger.info(
                '%s: Skipping precision atmosphere gate - transitioning directly from cycling',
                self._ctx._port_id,
            )

        approach_target, target_out, target_back, target_source = self._ctx._resolve_precision_targets(
            min_psi,
            max_psi,
            activation_direction,
        )

        if self._ctx._uses_nc_derived_vacuum_window():
            logger.info(
                '%s: Precision NC-derived window sweep low=%.4f high=%.4f rate=%.4f psi/s',
                self._ctx._port_id,
                target_back,
                target_out,
                self._ctx._slow_edge_rate_psi,
            )
            outcome = self._ctx._run_window_precision_pass(
                low_target=target_back,
                high_target=target_out,
                rate_psi_per_sec=self._ctx._slow_edge_rate_psi,
            )
            self._ctx._last_precision_missing_edge = outcome.missing_edge
            if outcome.result:
                self._ctx._emit_substate('precision.exhaust')
                return outcome.result
            return None

        if self._can_start_precision_from_current_reset_side(
            skip_atmosphere_gate,
            activation_direction,
            approach_target,
        ):
            self._ctx._emit_substate('precision.fast_approach')
            logger.info(
                '%s: Precision direct handoff - already reset; starting slow sweep from current pressure',
                self._ctx._port_id,
            )
        else:
            self._run_precision_fast_approach(sweep_mode, approach_target)

        armed_from_reset_side = self._ensure_precision_starts_from_reset_side(
            sweep_mode,
            activation_direction,
            approach_target,
            target_back,
            min_psi,
            max_psi,
        )
        if armed_from_reset_side and not self._ctx._cancel_event.is_set():
            logger.info(
                '%s: Precision reset-side arming complete; fast-positioning back to %.4f PSI',
                self._ctx._port_id,
                approach_target,
            )
            self._run_precision_fast_approach(sweep_mode, approach_target)

        logger.info('%s: Precision final return target=%.4f PSI', self._ctx._port_id, target_back)
        if sweep_mode == 'vacuum' and not self._ctx._port.set_solenoid(to_vacuum=True):
            self._ctx._fail(
                TestFailureCode.ROUTE_FAILURE,
                f'Failed to restore vacuum route before precision sweep on {self._ctx._port_id}',
            )
        self._log_precision_start_snapshot(target_source)

        logger.info(
            '%s: Precision sweep direction=%s source=%s approach=%.4f out=%.4f back=%.4f rate=%.4f psi/s',
            self._ctx._port_id,
            'increasing' if activation_direction > 0 else 'decreasing',
            target_source,
            approach_target,
            target_out,
            target_back,
            self._ctx._slow_edge_rate_psi,
        )
        outcome = self._ctx._run_sweep_pass(
            target_out,
            target_back,
            activation_direction,
            self._ctx._slow_edge_rate_psi,
        )
        result = outcome.result
        self._ctx._last_precision_missing_edge = outcome.missing_edge

        if self._ctx._cancel_event.is_set():
            return None

        if result:
            self._ctx._emit_substate('precision.exhaust')
            return result

        return None

    def _can_start_precision_from_current_reset_side(
        self,
        skip_atmosphere_gate: bool,
        activation_direction: int,
        approach_target: float,
    ) -> bool:
        if not skip_atmosphere_gate:
            return False
        if self._ctx._uses_nc_derived_vacuum_window():
            return False
        pressure, switch_state = self._ctx._read_pressure_and_switch_state()
        if pressure is None or switch_state is None:
            return False
        if switch_state == self._ctx._target_switch_state_for_edge('activation'):
            return False
        tolerance = max(0.02, self._ctx._precision_approach_tolerance_psi)
        if activation_direction < 0:
            ready = pressure <= approach_target + tolerance
        else:
            ready = pressure >= approach_target - tolerance
        if ready:
            logger.info(
                '%s: Precision handoff snapshot pressure=%.4f PSI switch_activated=%s',
                self._ctx._port_id,
                pressure,
                switch_state,
            )
        return ready

    def _run_precision_atmosphere_gate(self, atmosphere_psi: float) -> None:
        self._ctx._emit_substate('precision.prep_atmosphere')
        stable_atmosphere_psi = atmosphere_psi
        stable_baro_psi = self._ctx._get_barometric_psi(self._ctx._port_id)
        logger.info(
            '%s: Precision atmosphere gate start target=%.4f hold=%.2fs',
            self._ctx._port_id,
            stable_atmosphere_psi,
            self._ctx._precision_atmosphere_hold_s,
        )
        self._ctx._safe_vent()
        pressure_ref = (
            self._ctx._test_setup.pressure_reference
            if self._ctx._test_setup
            else None
        )
        uses_absolute_atmosphere = (
            self._ctx._ptp_limits_use_psia_scale()
            or str(pressure_ref or '').strip().lower() == 'absolute'
        )
        target_abs = stable_atmosphere_psi if uses_absolute_atmosphere else stable_baro_psi
        self._ctx._set_pressure_or_raise(target_abs)
        self._ctx._port.alicat.cancel_hold()
        self._ctx._emit_substate('precision.hold_atmosphere')
        atmosphere_tolerance = (
            max(self._ctx._atmosphere_tolerance_psi, 1.25)
            if uses_absolute_atmosphere
            else self._ctx._atmosphere_tolerance_psi
        )
        if self._ctx._wait_for_atmosphere(
            stable_atmosphere_psi,
            timeout_s=self._ctx._edge_timeout_s,
            hold_s=self._ctx._precision_atmosphere_hold_s,
            tolerance_psi=atmosphere_tolerance,
        ):
            return
        if self._ctx._cancel_event.is_set():
            return
        self._ctx._fail(
            TestFailureCode.ATMOSPHERE_TIMEOUT,
            f'Timeout waiting for precision atmosphere gate on {self._ctx._port_id}',
        )

    def _run_precision_fast_approach(
        self,
        sweep_mode: str,
        approach_target: float,
    ) -> None:
        self._ctx._emit_substate('precision.fast_approach')
        approach_target_abs = self._ctx._to_absolute(approach_target)
        # Set pressure target and rate BEFORE canceling hold to prevent
        # the Alicat from resuming toward the old (stale) cycling setpoint.
        self._ctx._set_pressure_or_raise(approach_target_abs)
        if not self._ctx._port.alicat.set_ramp_rate(self._ctx._fast_rate_psi):
            self._ctx._fail(
                TestFailureCode.RAMP_RATE_FAILURE,
                f'Failed to set fast approach ramp rate for {self._ctx._port_id}',
            )
        self._ctx._port.alicat.cancel_hold()
        logger.info(
            '%s: Precision handoff commanding approach target=%.4f PSI before solenoid switch',
            self._ctx._port_id,
            approach_target,
        )
        self._ctx._set_active_test_route(sweep_mode, 'precision fast approach')
        if self._ctx._wait_until_near_target(
            target_psi=approach_target,
            timeout_s=min(self._ctx._edge_timeout_s, 30.0),
            tolerance_psi=self._ctx._precision_approach_tolerance_psi,
            settle_s=self._ctx._precision_approach_settle_s,
        ):
            return
        pressure, switch_state = self._ctx._read_pressure_and_switch_state()
        if (
            sweep_mode == 'vacuum'
            and switch_state is not None
            and switch_state != self._ctx._target_switch_state_for_edge('activation')
            and pressure is not None
        ):
            logger.warning(
                '%s: Precision approach target %.4f PSI was not reached exactly '
                '(pressure=%.4f PSI), but switch is reset; continuing slow sweep',
                self._ctx._port_id,
                approach_target,
                pressure,
            )
            return
        self._ctx._fail(
            TestFailureCode.TARGET_TIMEOUT,
            f'Timeout waiting for precision approach target {approach_target:.3f} PSI on {self._ctx._port_id}',
        )

    def _log_precision_start_snapshot(self, target_source: str) -> None:
        start_pressure, start_switch = self._ctx._read_pressure_and_switch_state()
        logger.info(
            '%s: Precision start snapshot pressure=%s switch_activated=%s source=%s',
            self._ctx._port_id,
            f'{start_pressure:.4f} PSI' if start_pressure is not None else '--',
            start_switch,
            target_source,
        )

    def _ensure_precision_starts_from_reset_side(
        self,
        sweep_mode: str,
        activation_direction: int,
        approach_target: float,
        reset_target: float,
        min_psi: float,
        max_psi: float,
    ) -> bool:
        pressure, switch_state = self._ctx._read_pressure_and_switch_state()
        activation_state = self._ctx._target_switch_state_for_edge('activation')
        if switch_state != activation_state:
            return False

        hw_min_psi, hw_max_psi = self._ctx._resolve_hardware_limits_test_reference()
        arm_target = min(hw_max_psi, max(hw_min_psi, reset_target))
        if abs(arm_target - approach_target) < 0.02:
            logger.warning(
                '%s: Pre-pass switch already activated at pressure=%s; no room to arm from reset side',
                self._ctx._port_id,
                f'{pressure:.4f} PSI' if pressure is not None else '--',
            )
            return False

        logger.warning(
            '%s: Pre-pass switch already activated at pressure=%s; arming from reset side at %.4f PSI',
            self._ctx._port_id,
            f'{pressure:.4f} PSI' if pressure is not None else '--',
            arm_target,
        )
        self._ctx._set_active_test_route(sweep_mode, 'precision arming')
        if not self._ctx._port.alicat.set_ramp_rate(self._ctx._fast_rate_psi):
            self._ctx._fail(
                TestFailureCode.RAMP_RATE_FAILURE,
                f'Failed to set precision arming ramp rate for {self._ctx._port_id}',
            )
        self._ctx._set_pressure_or_raise(self._ctx._to_absolute(arm_target))
        self._ctx._port.alicat.cancel_hold()

        start = time.perf_counter()
        near_target_since: Optional[float] = None
        timeout_s = min(self._ctx._edge_timeout_s, 20.0)
        while time.perf_counter() - start < timeout_s:
            if self._ctx._cancel_event.is_set():
                return True
            pressure_now, state_now = self._ctx._read_pressure_and_switch_state()
            if state_now is not None and state_now != activation_state:
                logger.info(
                    '%s: Precision armed from reset side at pressure=%s switch_activated=%s',
                    self._ctx._port_id,
                    f'{pressure_now:.4f} PSI' if pressure_now is not None else '--',
                    state_now,
                )
                return True
            if (
                pressure_now is not None
                and abs(pressure_now - arm_target) <= self._ctx._precision_approach_tolerance_psi
            ):
                now = time.perf_counter()
                if near_target_since is None:
                    near_target_since = now
                elif now - near_target_since >= self._ctx._precision_approach_settle_s:
                    logger.warning(
                        '%s: Precision arming reached %.4f PSI but switch still reads activated; '
                        'continuing so slow sweep can verify the edge',
                        self._ctx._port_id,
                        arm_target,
                    )
                    return True
            else:
                near_target_since = None
            time.sleep(0.02)

        self._ctx._fail(
            TestFailureCode.EDGE_NOT_FOUND,
            (
                f'Precision could not reset switch before activation sweep on {self._ctx._port_id} '
                f'(target {arm_target:.3f} PSI)'
            ),
        )

class TestExecutor:
    """
    Executes the cycling + precision test sequence in a background thread.

    Usage:
        executor = TestExecutor(...)
        executor.start()       # launches background thread
        executor.request_cancel()  # to abort

    Callbacks are invoked from the background thread; the controller must
    marshal them to the main thread (e.g. via QTimer.singleShot).
    """

    def __init__(
        self,
        port_id: str,
        port: Port,
        test_setup: TestSetup,
        config: Dict[str, Any],
        get_latest_reading: Callable[[str], Optional[PortReading]],
        get_barometric_psi: Callable[[str], float],
        # Callbacks
        on_cycling_complete: Optional[Callable[[], None]] = None,
        on_substate_update: Optional[Callable[[str], None]] = None,
        on_edges_captured: Optional[Callable[[float, float], None]] = None,
        on_edge_detected: Optional[Callable[[str, float], None]] = None,  # edge_type, pressure_psi
        on_cycle_estimate: Optional[Callable[[Optional[float], Optional[float], int], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_cancelled: Optional[Callable[[], None]] = None,
        on_event: Optional[Callable[[TestEvent], None]] = None,
        wait_for_precision_slot: Optional[Callable[[], bool]] = None,
    ):
        self._port_id = port_id
        self._port = port
        self._test_setup = test_setup
        self._config = config
        self._get_latest_reading = get_latest_reading
        self._get_barometric_psi = get_barometric_psi

        # Callbacks
        self._on_cycling_complete = on_cycling_complete
        self._on_substate_update = on_substate_update
        self._on_edges_captured = on_edges_captured
        self._on_edge_detected = on_edge_detected
        self._on_cycle_estimate = on_cycle_estimate
        self._on_error = on_error
        self._on_cancelled = on_cancelled
        self._on_event = on_event
        self._wait_for_precision_slot = wait_for_precision_slot

        # Control
        self._cancel_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Config values
        control_cfg = parse_control_config(config)

        self._num_cycles = control_cfg.cycling.num_cycles
        slow_torr_per_sec = control_cfg.ramps.precision_sweep_rate_torr_per_sec
        precision_edge_torr_per_sec = control_cfg.ramps.precision_edge_rate_torr_per_sec
        near_atmosphere_threshold_psi = control_cfg.ramps.low_pressure_precision_threshold_psi
        near_atmosphere_delta_psi = self._near_atmosphere_precision_delta_psi()
        if (
            near_atmosphere_delta_psi is not None
            and near_atmosphere_threshold_psi > 0.0
            and near_atmosphere_delta_psi <= near_atmosphere_threshold_psi
        ):
            slow_torr_per_sec = control_cfg.ramps.low_pressure_precision_sweep_rate_torr_per_sec
            precision_edge_torr_per_sec = slow_torr_per_sec
            logger.info(
                '%s: Near-atmosphere precision rate applied: %.3f Torr/s '
                '(nearest_delta=%.4f PSI, threshold=%.4f PSI)',
                port_id,
                slow_torr_per_sec,
                near_atmosphere_delta_psi,
                near_atmosphere_threshold_psi,
            )
        self._slow_rate_psi = convert_pressure(slow_torr_per_sec, 'Torr', 'PSI')
        self._slow_edge_rate_psi = convert_pressure(precision_edge_torr_per_sec, 'Torr', 'PSI')
        self._medium_rate_psi = self._slow_rate_psi * 3.0
        # Cycling can be deliberately slowed for diagnostics without changing
        # the precision sweep rate that determines measured setpoints.
        self._fast_rate_psi = control_cfg.ramps.fast_cycle_rate_psi_per_sec
        self._pre_approach_rate_multiplier = control_cfg.ramps.pre_approach_rate_multiplier
        self._overshoot_pct = control_cfg.edge_detection.overshoot_beyond_limit_percent
        self._edge_timeout_s = control_cfg.edge_detection.timeout_sec
        self._atmosphere_tolerance_psi = control_cfg.edge_detection.atmosphere_tolerance_psi
        approach_tolerance_torr = control_cfg.edge_detection.precision_approach_tolerance_torr
        self._precision_approach_tolerance_psi = max(
            0.02,
            convert_pressure(approach_tolerance_torr, 'Torr', 'PSI'),
        )
        self._precision_approach_settle_s = control_cfg.edge_detection.precision_approach_settle_sec
        self._precision_atmosphere_hold_s = control_cfg.edge_detection.precision_start_atmosphere_hold_sec
        close_limit_offset_torr = control_cfg.edge_detection.precision_close_limit_offset_torr
        self._precision_close_limit_offset_psi = max(
            0.05,
            convert_pressure(close_limit_offset_torr, 'Torr', 'PSI'),
        )
        prepass_nudge_torr = control_cfg.edge_detection.precision_prepass_nudge_torr
        self._precision_prepass_nudge_psi = max(
            0.02,
            convert_pressure(prepass_nudge_torr, 'Torr', 'PSI'),
        )
        deactivation_margin_torr = control_cfg.edge_detection.precision_deactivation_margin_torr
        self._precision_deactivation_margin_psi = max(
            0.02,
            convert_pressure(deactivation_margin_torr, 'Torr', 'PSI'),
        )
        return_overshoot_torr = control_cfg.edge_detection.precision_return_overshoot_torr
        self._precision_return_overshoot_psi = max(
            0.0,
            convert_pressure(return_overshoot_torr, 'Torr', 'PSI'),
        )
        self._precision_post_target_grace_s = max(
            0.0,
            control_cfg.edge_detection.precision_post_target_grace_sec,
        )
        self._cycle_no_switch_grace_s = max(2.5, self._precision_post_target_grace_s)
        self._stable_count = control_cfg.debounce.stable_sample_count
        self._cycle_stable_count = max(1, min(2, self._stable_count))
        self._min_edge_interval_s = control_cfg.debounce.min_edge_interval_ms / 1000.0
        self._cycle_activation_samples: list[float] = []
        self._cycle_deactivation_samples: list[float] = []
        self._cycle_observed_edge_states: dict[str, bool] = {}
        self._cycle_debounce_state = SpdtDebounceState()
        self._cycle_waiting_edge: Optional[str] = None
        self._run_atmosphere_psi: Optional[float] = None
        self._alicat_setpoint_ref: Optional[str] = None
        self._last_precision_missing_edge: Optional[str] = None
        self._cycle_phase_runner = CyclePhaseRunner(self)
        self._precision_phase_runner = PrecisionPhaseRunner(self)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _test_target_psi(self) -> Optional[float]:
        target = self._test_setup.activation_target
        if target is None:
            return None
        try:
            return abs(convert_pressure(target, self._test_setup.units_label or 'PSI', 'PSI'))
        except Exception:
            return None

    def _near_atmosphere_precision_delta_psi(self) -> Optional[float]:
        """Smallest absolute PSI distance between the PTP target/window and atmosphere."""
        if self._test_setup is None:
            return None
        candidates: list[float] = []
        units_label = self._test_setup.units_label or 'PSI'
        target = self._test_setup.activation_target
        if target is not None:
            try:
                candidates.append(convert_pressure(target, units_label, 'PSI'))
            except Exception:
                pass
        for band_name in ('increasing', 'decreasing'):
            band = self._test_setup.bands.get(band_name, {})
            for key in ('lower', 'upper'):
                raw = band.get(key)
                if raw is None or not math.isfinite(raw):
                    continue
                try:
                    candidates.append(convert_pressure(raw, units_label, 'PSI'))
                except Exception:
                    continue
        if not candidates:
            return None

        baro = self._get_barometric_psi(self._port_id)
        absolute_candidates = [self._to_absolute(value) for value in candidates]
        finite_deltas = [
            abs(value - baro)
            for value in absolute_candidates
            if math.isfinite(value)
        ]
        if not finite_deltas:
            return None
        return min(finite_deltas)

    def start(self) -> None:
        """Start the test sequence in a background thread."""
        self._cancel_event.clear()
        self._alicat_setpoint_ref = None
        self._thread = threading.Thread(
            target=self._run,
            name=f'TestExecutor-{self._port_id}',
            daemon=True,
        )
        self._thread.start()

    def request_cancel(self) -> None:
        """Request cancellation of the running test."""
        self._cancel_event.set()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    def run_debug_sweep_pass(
        self,
        bounds: tuple[float, float],
        direction: int,
        rate_psi_per_sec: float,
    ) -> Optional[tuple[float, float]]:
        """Run one out/back sweep pass and return activation/deactivation edges.

        This is a lightweight public entry point for debug workflows that need the
        same edge-detection behavior as production precision sweeps.
        """
        min_psi, max_psi = bounds
        target_out = max_psi if direction > 0 else min_psi
        target_back = min_psi if direction > 0 else max_psi

        result = self._execute_out_back_sweep(
            target_out=target_out,
            target_back=target_back,
            direction=direction,
            rate_psi_per_sec=rate_psi_per_sec,
            fail_on_rate_error=False,
        )
        if result.result is None:
            return None
        return (result.result.activation_psi, result.result.deactivation_psi)

    # ------------------------------------------------------------------
    # Internal: main loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Main test execution sequence (runs in background thread)."""
        try:
            self._emit_event('run_started')
            self._ensure_alicat_units()
            self._lock_alicat_setpoint_reference()
            self._run_atmosphere_psi = None
            pressure_ref = (
                str(self._test_setup.pressure_reference).strip().lower()
                if self._test_setup and self._test_setup.pressure_reference
                else 'absolute'
            )
            if self._ptp_limits_use_psia_scale() or pressure_ref == 'absolute':
                self._run_atmosphere_psi = self._get_barometric_psi(self._port_id)
            else:
                self._run_atmosphere_psi = 0.0
            sweep_mode = self._resolve_sweep_mode()
            bounds = self._resolve_sweep_bounds()
            baro_psi = self._get_barometric_psi(self._port_id)
            logger.info(
                '%s: Sweep config mode=%s ref=%s bounds=%.4f..%.4f psi '
                'test_atm=%.4f baro=%.4f psi vacuum_no_open=%s',
                self._port_id,
                sweep_mode,
                pressure_ref,
                bounds[0],
                bounds[1],
                self._run_atmosphere_psi,
                baro_psi,
                self._vacuum_switch_trips_on_no_open(),
            )

            if sweep_mode == 'vacuum':
                self._ensure_vacuum_solenoid_route()

            # ---- Pre-approach: fast ramp to near edge of test range ----
            self._cycle_phase_runner.run_pre_approach(sweep_mode, bounds)
            if self._cancel_and_emit():
                return

            # ---- Cycling phase ----
            for cycle in range(1, self._num_cycles + 1):
                if self._cancel_and_emit():
                    return

                logger.info('%s: Cycle %d/%d', self._port_id, cycle, self._num_cycles)
                self._emit_event('cycle_started', cycle_index=cycle, cycle_total=self._num_cycles)
                self._run_single_cycle(sweep_mode, bounds)

            if self._on_cycling_complete:
                self._on_cycling_complete()

            if self._wait_for_precision_slot and not self._wait_for_precision_slot():
                logger.info('%s: Precision slot wait ended before grant', self._port_id)
                return

            if self._cancel_and_emit():
                return

            # ---- Precision test phase ----
            # Hand off directly from the last cycling reset into precision.
            # This avoids a needless return to atmosphere while still using
            # cycle-estimate targets to approach from the correct side.
            self._emit_event('precision_started', sweep_mode=sweep_mode)
            self._last_precision_missing_edge = None
            result = self._run_precision_sweep(
                sweep_mode,
                bounds,
                skip_atmosphere_gate=True,
            )

            if self._cancel_and_emit():
                return

            if result is None:
                if self._last_precision_missing_edge == 'first':
                    failure_message = 'Activation edge not detected during precision out-sweep'
                elif self._last_precision_missing_edge == 'second':
                    failure_message = 'Deactivation edge not detected during precision return-sweep'
                else:
                    failure_message = 'No edges detected during precision sweep'
                self._fail(
                    TestFailureCode.EDGE_NOT_FOUND,
                    failure_message,
                )
                return

            # Return to atmosphere
            self._safe_vent()

            if self._on_edges_captured:
                self._on_edges_captured(result.activation_psi, result.deactivation_psi)
            self._emit_event(
                'run_completed',
                activation_psi=result.activation_psi,
                deactivation_psi=result.deactivation_psi,
            )

        except TestFailure as failure:
            logger.error('Test failure for %s: %s', self._port_id, failure)
            self._abort_with_error(failure)
        except Exception as exc:
            logger.error('Test executor error for %s: %s', self._port_id, exc, exc_info=True)
            self._abort_with_error(
                TestFailure(TestFailureCode.INTERNAL_ERROR, str(exc)),
            )
        finally:
            self._run_atmosphere_psi = None

    # ------------------------------------------------------------------
    # Cycling
    # ------------------------------------------------------------------

    def _run_single_cycle(self, sweep_mode: str, bounds: tuple[float, float]) -> None:
        self._cycle_phase_runner.run_single_cycle(sweep_mode, bounds)

    def _resolve_cycle_targets(
        self,
        sweep_mode: str,
        min_psi: float,
        max_psi: float,
        overshoot: float,
        hw_min_psi: float,
        hw_max_psi: float,
    ) -> tuple[float, float]:
        pressure_ref = self._test_setup.pressure_reference if self._test_setup else None
        activation, deactivation = resolve_cycle_ramp_targets(
            sweep_mode=sweep_mode,
            activation_direction=self._resolve_activation_sweep_direction(),
            min_psi=min_psi,
            max_psi=max_psi,
            overshoot=overshoot,
            barometric_psi=self._get_barometric_psi(self._port_id),
            hw_min_psi=hw_min_psi,
            hw_max_psi=hw_max_psi,
            pressure_reference=pressure_ref,
        )
        if (
            sweep_mode == 'vacuum'
            and self._resolve_activation_sweep_direction() > 0
            and (
                self._uses_nc_derived_vacuum_window()
                or not self._vacuum_switch_trips_on_no_open()
            )
        ):
            deactivation = min(hw_max_psi, max(hw_min_psi, max_psi + overshoot))
        return activation, deactivation

    def _adaptive_cycle_target(
        self,
        edge_type: str,
        full_target: float,
        direction: int,
        bounds: tuple[float, float],
        hw_bounds: tuple[float, float],
    ) -> float:
        """Use prior cycle edges to shorten later cycle traverses, with full-target fallback."""
        samples = (
            self._cycle_activation_samples
            if edge_type == 'activation'
            else self._cycle_deactivation_samples
        )
        estimate = self._mean_or_none(samples)
        if estimate is None:
            return full_target

        min_psi, max_psi = bounds
        hw_min, hw_max = hw_bounds
        margin = max(1.0, (max_psi - min_psi) * 0.25)
        if (edge_type == 'activation') == (direction > 0):
            target = min(hw_max, estimate + margin)
            return min(full_target, target)

        target = max(hw_min, estimate - margin)
        return max(full_target, target)

    def _try_accept_port_cycle_edge(
        self,
        edge_type: str,
        port_edges_before: int,
        samples_list: list[float],
    ) -> bool:
        """Accept an edge already recorded by Port._check_for_edge (same path as the UI)."""
        get_history = getattr(self._port, 'get_edge_history', None)
        if not callable(get_history):
            return False
        for edge in get_history()[port_edges_before:]:
            if not self._cycle_edge_switch_state_allowed(edge_type, bool(edge.activated)):
                continue
            pressure_test: Optional[float] = None
            reading = self._get_latest_reading(self._port_id)
            if reading is not None:
                pressure_test = self._reading_pressure_test_psi(reading)
            if pressure_test is None or not math.isfinite(pressure_test):
                pressure_test = self._absolute_to_test_reference(edge.pressure)
            if not math.isfinite(pressure_test):
                continue
            if not self._cycle_edge_pressure_allowed(edge_type, pressure_test):
                continue
            self._cycle_observed_edge_states[edge_type] = bool(edge.activated)
            samples_list.append(pressure_test)
            self._emit_cycle_estimate_from_samples()
            logger.info(
                '%s: %s edge accepted from port detector at %.4f PSI',
                self._port_id,
                edge_type,
                pressure_test,
            )
            return True
        return False

    def _wait_for_cycle_edge(
        self,
        target_psi: float,
        direction: int,
        edge_type: str,
        samples_before: int,
        timeout_s: float,
        fallback_target_psi: Optional[float] = None,
        port_edges_before: Optional[int] = None,
    ) -> tuple[bool, Optional[str]]:
        """Wait for a cycle edge (activation or deactivation) to be detected and committed by debounce system.
        
        Returns:
            Tuple of (success: bool, diagnostic_message: Optional[str])
            diagnostic_message contains failure details if success is False.
        """
        start = time.perf_counter()
        target_test = self._cycle_ramp_target_test_reference(target_psi)
        want_state = self._cycle_target_switch_state(edge_type)
        fallback_applied = False
        self._cycle_waiting_edge = edge_type
        get_history = getattr(self._port, 'get_edge_history', None)
        if port_edges_before is None:
            port_edges_before = len(get_history()) if callable(get_history) else 0

        # Determine which samples list to monitor based on edge type
        if edge_type == 'activation':
            samples_list = self._cycle_activation_samples
        elif edge_type == 'deactivation':
            samples_list = self._cycle_deactivation_samples
        else:
            logger.error('%s: Unknown edge_type "%s"', self._port_id, edge_type)
            return False, f'Unknown edge_type "{edge_type}"'

        # Track diagnostic information for failure reporting
        last_pressure: Optional[float] = None
        last_switch_state: Optional[bool] = None
        initial_switch_state: Optional[bool] = None
        initial_switch_valid: Optional[bool] = None
        switch_state_changed = False
        target_reached = False
        target_reached_at: Optional[float] = None
        no_switch_samples = 0  # Count samples where switch appears disconnected
        last_progress_log_s = start

        if self._try_accept_port_cycle_edge(edge_type, port_edges_before, samples_list):
            self._hold_after_cycle_edge(edge_type, samples_list[-1] if samples_list else None)
            self._cycle_waiting_edge = None
            return True, None

        priming = self._get_latest_reading(self._port_id)
        if priming is not None:
            _pa, priming_test = self._extract_pressures(priming, collect_cycle_edges=False)
            priming_switch = (
                bool(priming.switch.switch_activated) if priming.switch is not None else None
            )
            if self._cycle_edge_already_present(edge_type, priming_test, priming_switch):
                sample_p = priming_test if priming_test is not None else 0.0
                self._cycle_observed_edge_states[edge_type] = bool(priming_switch)
                samples_list.append(sample_p)
                self._emit_cycle_estimate_from_samples()
                logger.info(
                    '%s: %s edge already present at %.4f PSI (switch=%s)',
                    self._port_id,
                    edge_type,
                    sample_p,
                    priming_switch,
                )
                self._cycle_waiting_edge = None
                return True, None

        while time.perf_counter() - start < timeout_s:
            if self._cancel_event.is_set():
                self._cycle_waiting_edge = None
                return False, 'Test cancelled by user'

            now_s = time.perf_counter()
            if now_s - last_progress_log_s >= 5.0:
                logger.info(
                    '%s: Waiting for %s edge (%.0fs) pressure=%s switch=%s',
                    self._port_id,
                    edge_type,
                    now_s - start,
                    f'{last_pressure:.2f}' if last_pressure is not None else '--',
                    last_switch_state,
                )
                last_progress_log_s = now_s

            reading = self._get_latest_reading(self._port_id)
            if reading is None:
                time.sleep(0.01)
                continue

            # Extract pressures with cycle edge collection enabled
            # This calls _observe_cycle_switch_sample which updates the debounce state
            pressure_abs, pressure_test = self._extract_pressures(
                reading,
                collect_cycle_edges=True,
            )
            
            # Track diagnostic info
            if pressure_test is not None:
                last_pressure = pressure_test
            
            # Check switch state for "no switch" detection
            switch_valid = True
            current_switch_state: Optional[bool] = None
            if reading.switch is not None:
                sw = reading.switch
                current_switch_state = bool(sw.switch_activated)
                if initial_switch_state is None:
                    initial_switch_state = current_switch_state
                    initial_switch_valid = True
                elif current_switch_state != initial_switch_state:
                    switch_state_changed = True
                last_switch_state = current_switch_state
                if not getattr(sw, 'is_valid', True):
                    no_active = bool(getattr(sw, 'no_active', False))
                    nc_active = bool(getattr(sw, 'nc_active', False))
                    if not no_active and not nc_active:
                        no_switch_samples += 1
                        switch_valid = False
            else:
                switch_valid = False
                no_switch_samples += 1

            if pressure_test is not None:
                reached_target = False
                if direction > 0 and pressure_test >= target_test:
                    reached_target = True
                elif direction < 0 and pressure_test <= target_test:
                    reached_target = True
                
                if reached_target and not target_reached:
                    target_reached = True
                    target_reached_at = time.perf_counter()
                    logger.warning(
                        '%s: Reached %s target %.4f PSI without detecting edge (continuing to wait)',
                        self._port_id,
                        edge_type,
                        target_psi,
                    )

                if (
                    target_reached
                    and not fallback_applied
                    and fallback_target_psi is not None
                    and target_reached_at is not None
                    and time.perf_counter() - target_reached_at >= 0.25
                ):
                    logger.warning(
                        '%s: No %s edge at adaptive cycle target %.4f PSI; '
                        'continuing to full target %.4f PSI',
                        self._port_id,
                        edge_type,
                        target_psi,
                        fallback_target_psi,
                    )
                    target_psi = fallback_target_psi
                    target_abs = self._to_absolute(target_psi)
                    target_test = self._cycle_ramp_target_test_reference(target_psi)
                    self._set_pressure_or_raise(self._to_absolute(target_psi))
                    self._port.alicat.cancel_hold()
                    target_reached = False
                    target_reached_at = None
                    fallback_applied = True
                    continue

                if (
                    target_reached
                    and target_reached_at is not None
                    and not switch_state_changed
                    and initial_switch_state is not None
                ):
                    wait_at_target = time.perf_counter() - target_reached_at
                    if wait_at_target > self._cycle_no_switch_grace_s:
                        logger.warning(
                            '%s: No switch detected - reached target %.4f PSI but switch state unchanged after %.1fs - venting immediately',
                            self._port_id,
                            target_psi,
                            wait_at_target,
                        )
                        self._safe_vent()
                        self._cycle_waiting_edge = None
                        return False, 'NO_SWITCH_DETECTED'
            
            if self._try_accept_port_cycle_edge(edge_type, port_edges_before, samples_list):
                self._hold_after_cycle_edge(edge_type, samples_list[-1] if samples_list else None)
                self._cycle_waiting_edge = None
                return True, None

            # Check if debounce system committed an edge (samples list length increased)
            if len(samples_list) > samples_before:
                if not self._cycle_edge_pressure_allowed(edge_type, samples_list[-1]):
                    rejected = samples_list.pop()
                    logger.debug(
                        '%s: Rejected %s edge sample %.4f PSI outside cycle acceptance window',
                        self._port_id,
                        edge_type,
                        rejected,
                    )
                    continue
                logger.info(
                    '%s: %s edge detected and committed at %.4f PSI',
                    self._port_id,
                    edge_type,
                    samples_list[-1] if samples_list else 0.0,
                )
                self._hold_after_cycle_edge(edge_type, samples_list[-1] if samples_list else None)
                self._cycle_waiting_edge = None
                return True, None

            # Port UI detects single-sample transitions; accept when debounce lags.
            if (
                current_switch_state is not None
                and switch_state_changed
                and self._cycle_edge_switch_state_allowed(edge_type, current_switch_state)
                and pressure_test is not None
                and math.isfinite(pressure_test)
            ):
                if self._cycle_edge_pressure_allowed(edge_type, pressure_test):
                    self._cycle_observed_edge_states[edge_type] = bool(current_switch_state)
                    samples_list.append(pressure_test)
                    self._emit_cycle_estimate_from_samples()
                    logger.info(
                        '%s: %s edge accepted from switch transition at %.4f PSI',
                        self._port_id,
                        edge_type,
                        pressure_test,
                    )
                    self._hold_after_cycle_edge(edge_type, pressure_test)
                    self._cycle_waiting_edge = None
                    return True, None

            time.sleep(0.01)

        # Check for "no switch detected" condition
        # Criteria: switch state never changed AND we reached target pressure
        # OR switch readings show invalid state (both NO/NC inactive) consistently
        elapsed = time.perf_counter() - start
        no_switch_detected = False
        
        if target_reached and not switch_state_changed:
            # Reached target but switch never changed - likely no switch
            wait_after_target = elapsed - (target_reached_at - start) if target_reached_at else 0
            if wait_after_target > self._cycle_no_switch_grace_s:
                no_switch_detected = True
                logger.warning(
                    '%s: No switch detected - reached target %.4f PSI but switch state never changed (waited %.1fs)',
                    self._port_id,
                    target_psi,
                    wait_after_target,
                )
        
        # Also check if switch readings show invalid state consistently
        # Lower threshold - detect if more than 30% of samples show invalid switch
        total_samples = int(elapsed * 100)  # Rough estimate: ~100 samples per second
        if total_samples > 0 and no_switch_samples > max(30, total_samples * 0.3):
            no_switch_detected = True
            logger.warning(
                '%s: No switch detected - switch readings show invalid state (both NO/NC inactive) in %d/%d samples',
                self._port_id,
                no_switch_samples,
                total_samples,
            )
        
        if no_switch_detected:
            self._cycle_waiting_edge = None
            return False, 'NO_SWITCH_DETECTED'

        # Timeout occurred - build diagnostic message
        diagnostic_parts = [
            f'Timeout after {elapsed:.1f}s waiting for {edge_type} edge',
            f'Target pressure: {target_psi:.4f} PSI',
        ]
        
        if last_pressure is not None:
            diagnostic_parts.append(f'Final pressure reached: {last_pressure:.4f} PSI')
            pressure_error = abs(last_pressure - target_psi)
            if pressure_error > 0.1:
                diagnostic_parts.append(f'Pressure error: {pressure_error:.4f} PSI from target')
        
        if last_switch_state is not None:
            expected_state = self._cycle_target_switch_state(edge_type)
            diagnostic_parts.append(
                f'Switch state: {"activated" if last_switch_state else "deactivated"} '
                f'(expected: {"activated" if expected_state else "deactivated"})'
            )
            if last_switch_state == expected_state:
                diagnostic_parts.append('WARNING: Switch is already in expected state - may indicate wiring issue or switch stuck')
        elif initial_switch_valid is False:
            diagnostic_parts.append('Switch readings show invalid state - check wiring')
        
        if target_reached:
            wait_after_target = elapsed - (target_reached_at - start) if target_reached_at else 0
            diagnostic_parts.append(
                f'Target reached at {target_reached_at - start:.1f}s, waited additional {wait_after_target:.1f}s'
            )
        else:
            diagnostic_parts.append('Target pressure was not reached before timeout')
        
        diagnostic_msg = '; '.join(diagnostic_parts)
        logger.warning(
            '%s: Timeout waiting for %s edge (target=%.4f PSI) - %s',
            self._port_id,
            edge_type,
            target_psi,
            diagnostic_msg,
        )
        self._cycle_waiting_edge = None
        return False, diagnostic_msg

    def _hold_after_cycle_edge(self, edge_type: str, pressure_test: Optional[float]) -> None:
        if edge_type != 'deactivation' or pressure_test is None or not math.isfinite(pressure_test):
            return
        self._set_pressure_or_raise(self._to_absolute(pressure_test))
        self._port.alicat.cancel_hold()
        logger.debug('%s: Holding at cycle reset edge %.4f PSI', self._port_id, pressure_test)

    def _wait_until_near_target(
        self,
        target_psi: float,
        timeout_s: float,
        tolerance_psi: float,
        settle_s: float,
    ) -> bool:
        """Wait until pressure stays within tolerance around target."""
        start = time.perf_counter()
        target_test = (
            target_psi
            if self._ptp_limits_use_psia_scale()
            else self._absolute_to_test_reference(self._to_absolute(target_psi))
        )
        near_since: Optional[float] = None

        while time.perf_counter() - start < timeout_s:
            if self._cancel_event.is_set():
                return False

            reading = self._get_latest_reading(self._port_id)
            if reading is None:
                time.sleep(0.02)
                continue

            pressure_abs = self._reading_pressure_abs_psi(reading)
            if pressure_abs is None:
                time.sleep(0.02)
                continue
            pressure_test = self._absolute_to_test_reference(pressure_abs)
            error = abs(pressure_test - target_test)
            if error <= tolerance_psi:
                now = time.perf_counter()
                if near_since is None:
                    near_since = now
                elif now - near_since >= max(0.0, settle_s):
                    return True
            else:
                near_since = None

            time.sleep(0.02)

        logger.warning(
            '%s: Timeout (%.1fs) waiting near approach target=%.4f tol=%.4f',
            self._port_id,
            timeout_s,
            target_psi,
            tolerance_psi,
        )
        return False

    def _read_pressure_and_switch_state(self) -> tuple[Optional[float], Optional[bool]]:
        reading = self._get_latest_reading(self._port_id)
        if reading is None:
            return None, None
        pressure = self._reading_pressure_test_psi(reading)
        if reading.switch is None:
            return pressure, None
        return pressure, self._effective_switch_state(reading.switch)

    def _prepare_switch_for_cycle_edge(
        self,
        *,
        sweep_mode: str,
        min_psi: float,
        max_psi: float,
        direction: int,
        edge_type: str,
        overshoot: float,
        hw_min_psi: float,
        hw_max_psi: float,
    ) -> None:
        """Move to the opposite side of the edge before a fast cycle ramp.

        If the switch is already in the target state for the upcoming leg (e.g. NO
        already closed before the activation ramp), cycling would otherwise sit in
        _wait_for_cycle_edge until the full timeout with no ACT/DEC estimates.
        """
        pressure, switch_state = self._read_pressure_and_switch_state()
        if switch_state is None:
            self._cycle_debounce_state = SpdtDebounceState()
            return

        nudge_psi = max(overshoot, self._precision_prepass_nudge_psi)

        if (
            edge_type == 'activation'
            and self._resolve_sweep_mode() == 'vacuum'
            and self._resolve_activation_sweep_direction() < 0
            and self._vacuum_switch_trips_on_no_open()
        ):
            nudge_target = min(hw_max_psi, max_psi + nudge_psi)
            prep_state = not self._cycle_target_switch_state(edge_type)
            target_state = self._cycle_target_switch_state(edge_type)
            # Very low absolute windows can sit near the band after a prior pull;
            # reset them above the band so the next vacuum leg sees a real edge.
            low_absolute_window = max_psi <= convert_pressure(50.0, 'Torr', 'PSI')
            already_on_activation_side = (
                not low_absolute_window
                and switch_state == target_state
                and pressure is not None
                and math.isfinite(pressure)
                and pressure > min_psi
            )
            needs_reset_nudge = (
                not already_on_activation_side
                and (
                    switch_state != prep_state
                    or (
                        pressure is not None
                        and math.isfinite(pressure)
                        and pressure <= min_psi
                    )
                )
            )
            if needs_reset_nudge:
                logger.info(
                    '%s: Cycle pressure-prep for decreasing-vacuum activation leg; '
                    'nudging to %.4f PSI for reset-side switch state',
                    self._port_id,
                    nudge_target,
                )
                self._emit_substate('cycling.prep_activation')
                self._set_active_test_route(sweep_mode, 'cycle prep')
                if not self._port.alicat.set_ramp_rate(self._fast_rate_psi):
                    self._fail(
                        TestFailureCode.RAMP_RATE_FAILURE,
                        f'Failed to set ramp rate for cycle prep on {self._port_id}',
                    )
                self._set_pressure_or_raise(self._to_absolute(nudge_target))
                self._port.alicat.cancel_hold()
                prep_timeout_s = min(3.0, self._edge_timeout_s)
                prep_start = time.perf_counter()
                near_target_since: Optional[float] = None
                while time.perf_counter() - prep_start < prep_timeout_s:
                    if self._cancel_event.is_set():
                        return
                    pressure_now, state = self._read_pressure_and_switch_state()
                    if state == prep_state:
                        break
                    if (
                        pressure_now is not None
                        and abs(pressure_now - nudge_target)
                        <= max(0.15, self._precision_approach_tolerance_psi)
                    ):
                        now = time.perf_counter()
                        if near_target_since is None:
                            near_target_since = now
                        elif now - near_target_since >= 0.5:
                            break
                    else:
                        near_target_since = None
                    time.sleep(0.02)
            else:
                logger.info(
                    '%s: Cycle prep skipped for decreasing-vacuum activation leg; '
                    'pressure %s is already on the reset side',
                    self._port_id,
                    f'{pressure:.4f} PSI' if pressure is not None else '--',
                )
            reading = self._get_latest_reading(self._port_id)
            if reading is not None and reading.switch is not None:
                self._cycle_debounce_state = self._spdt_debounce_from_switch(reading.switch)
            else:
                self._cycle_debounce_state = SpdtDebounceState()
            return

        if edge_type == 'activation' and pressure is not None and math.isfinite(pressure):
            starts_before_edge = (
                (direction > 0 and pressure <= min_psi)
                or (direction < 0 and pressure >= max_psi)
            )
            prep_state = not self._cycle_target_switch_state(edge_type)
            if starts_before_edge and switch_state == prep_state:
                reading = self._get_latest_reading(self._port_id)
                if reading is not None and reading.switch is not None:
                    self._cycle_debounce_state = self._spdt_debounce_from_switch(reading.switch)
                else:
                    self._cycle_debounce_state = SpdtDebounceState()
                logger.info(
                    '%s: Cycle prep skipped for %s leg; pressure %.4f PSI is already before edge band',
                    self._port_id,
                    edge_type,
                    pressure,
                )
                return

        if (
            edge_type == 'deactivation'
            and self._resolve_sweep_mode() == 'vacuum'
            and self._resolve_activation_sweep_direction() < 0
            and self._vacuum_switch_trips_on_no_open()
        ):
            reading = self._get_latest_reading(self._port_id)
            if reading is not None and reading.switch is not None:
                self._cycle_debounce_state = self._spdt_debounce_from_switch(reading.switch)
            else:
                self._cycle_debounce_state = SpdtDebounceState()
            logger.info(
                '%s: Cycle prep skipped for decreasing-vacuum deactivation leg; returning through reset edge',
                self._port_id,
            )
            return

        prep_state = not self._cycle_target_switch_state(edge_type)
        if switch_state == prep_state:
            reading = self._get_latest_reading(self._port_id)
            if reading is not None and reading.switch is not None:
                self._cycle_debounce_state = self._spdt_debounce_from_switch(reading.switch)
            else:
                self._cycle_debounce_state = SpdtDebounceState()
            return

        if prep_state:
            if direction > 0:
                nudge_target = max(hw_min_psi, min_psi - nudge_psi)
            else:
                nudge_target = min(hw_max_psi, max_psi + nudge_psi)
            substate = 'cycling.prep_activated'
        else:
            if direction > 0:
                nudge_target = max(hw_min_psi, min_psi - nudge_psi)
            else:
                nudge_target = min(hw_max_psi, max_psi + nudge_psi)
            substate = 'cycling.prep_deactivated'

        logger.info(
            '%s: Switch %s at %.4f PSI before %s leg (need %s); nudging to %.4f PSI',
            self._port_id,
            'activated' if switch_state else 'deactivated',
            pressure if pressure is not None else float('nan'),
            edge_type,
            'activated' if prep_state else 'deactivated',
            nudge_target,
        )
        self._emit_substate(substate)

        self._set_active_test_route(sweep_mode, 'cycle prep')
        if not self._port.alicat.set_ramp_rate(self._fast_rate_psi):
            self._fail(
                TestFailureCode.RAMP_RATE_FAILURE,
                f'Failed to set ramp rate for cycle prep on {self._port_id}',
            )
        self._set_pressure_or_raise(self._to_absolute(nudge_target))
        self._port.alicat.cancel_hold()

        prep_timeout_s = min(5.0, self._edge_timeout_s)
        prep_start = time.perf_counter()
        near_target_since: Optional[float] = None
        while time.perf_counter() - prep_start < prep_timeout_s:
            if self._cancel_event.is_set():
                return
            pressure_now, state = self._read_pressure_and_switch_state()
            if state == prep_state:
                reading = self._get_latest_reading(self._port_id)
                if reading is not None and reading.switch is not None:
                    self._cycle_debounce_state = self._spdt_debounce_from_switch(reading.switch)
                else:
                    self._cycle_debounce_state = SpdtDebounceState()
                logger.info(
                    '%s: Cycle prep complete — switch now %s',
                    self._port_id,
                    'activated' if state else 'deactivated',
                )
                return
            if pressure_now is not None and abs(pressure_now - nudge_target) <= max(0.15, self._precision_approach_tolerance_psi):
                now = time.perf_counter()
                if near_target_since is None:
                    near_target_since = now
                elif now - near_target_since >= 1.0:
                    break
            else:
                near_target_since = None
            time.sleep(0.02)

        self._fail(
            TestFailureCode.NO_SWITCH_DETECTED,
            (
                f'Switch did not reach {"activated" if prep_state else "deactivated"} '
                f'state at cycle prep target {nudge_target:.3f} PSI on {self._port_id}'
            ),
        )

    # ------------------------------------------------------------------
    # Precision sweep
    # ------------------------------------------------------------------

    def _run_precision_sweep(
        self,
        sweep_mode: str,
        bounds: tuple[float, float],
        skip_atmosphere_gate: bool = False,
    ) -> Optional[SweepResult]:
        return self._precision_phase_runner.run_precision_sweep(sweep_mode, bounds, skip_atmosphere_gate)

    def _run_sweep_pass(
        self,
        target_out: float,
        target_back: float,
        direction: int,
        rate_psi_per_sec: float,
    ) -> SweepPassOutcome:
        """Single sweep pass: out to edge, then back to edge."""

        logger.info(
            '%s: Precision pass direction=%s out=%.4f back=%.4f psi',
            self._port_id,
            'increasing' if direction > 0 else 'decreasing',
            target_out,
            target_back,
        )

        return self._execute_out_back_sweep(
            target_out=target_out,
            target_back=target_back,
            direction=direction,
            rate_psi_per_sec=rate_psi_per_sec,
            fail_on_rate_error=True,
        )

    def _run_window_precision_pass(
        self,
        low_target: float,
        high_target: float,
        rate_psi_per_sec: float,
    ) -> SweepPassOutcome:
        """Measure a two-edge window switch, then assign results by pressure order."""
        if not self._port.alicat.set_ramp_rate(rate_psi_per_sec):
            self._fail(TestFailureCode.RAMP_RATE_FAILURE, f'Failed to set sweep ramp rate for {self._port_id}')
            return SweepPassOutcome(result=None, missing_edge='rate_error')

        self._emit_substate('precision.window_low')
        low_edge = self._sweep_to_edge(low_target, -1, edge_type=None)
        if self._cancel_event.is_set():
            return SweepPassOutcome(result=None, missing_edge='cancelled')
        if low_edge is None:
            logger.warning('%s: Window lower edge not detected target=%.3f', self._port_id, low_target)
            return SweepPassOutcome(result=None, missing_edge='first')
        direction = self._resolve_activation_sweep_direction()
        low_edge_type = 'activation' if direction < 0 else 'deactivation'
        if self._on_edge_detected:
            self._on_edge_detected(low_edge_type, low_edge.pressure_psi)

        if not self._port.alicat.set_ramp_rate(rate_psi_per_sec):
            self._fail(TestFailureCode.RAMP_RATE_FAILURE, f'Failed to set return sweep ramp rate for {self._port_id}')
            return SweepPassOutcome(result=None, missing_edge='rate_error')

        self._emit_substate('precision.window_high')
        high_edge = self._sweep_to_edge(high_target, 1, edge_type=None)
        if self._cancel_event.is_set():
            return SweepPassOutcome(result=None, missing_edge='cancelled')
        if high_edge is None:
            logger.warning('%s: Window upper edge not detected target=%.3f', self._port_id, high_target)
            return SweepPassOutcome(result=None, missing_edge='second')
        high_edge_type = 'deactivation' if direction < 0 else 'activation'
        if self._on_edge_detected:
            self._on_edge_detected(high_edge_type, high_edge.pressure_psi)

        low_pressure = min(low_edge.pressure_psi, high_edge.pressure_psi)
        high_pressure = max(low_edge.pressure_psi, high_edge.pressure_psi)
        if direction > 0:
            result = SweepResult(activation_psi=high_pressure, deactivation_psi=low_pressure)
        else:
            result = SweepResult(activation_psi=low_pressure, deactivation_psi=high_pressure)

        return SweepPassOutcome(result=result, missing_edge=None)

    def _execute_out_back_sweep(
        self,
        target_out: float,
        target_back: float,
        direction: int,
        rate_psi_per_sec: float,
        fail_on_rate_error: bool,
    ) -> SweepPassOutcome:
        if not self._port.alicat.set_ramp_rate(rate_psi_per_sec):
            if fail_on_rate_error:
                self._fail(TestFailureCode.RAMP_RATE_FAILURE, f'Failed to set sweep ramp rate for {self._port_id}')
            return SweepPassOutcome(result=None, missing_edge='rate_error')

        # First edge is activation (sweeping in activation direction)
        edge_out = self._sweep_to_edge(target_out, direction, edge_type='activation')
        if self._cancel_event.is_set():
            return SweepPassOutcome(result=None, missing_edge='cancelled')
        if edge_out is None:
            logger.warning(
                '%s: No first edge detected in precision out-sweep target=%.3f',
                self._port_id,
                target_out,
            )
            return SweepPassOutcome(result=None, missing_edge='first')

        # Give the UI thread one frame to render the activation marker before
        # the return setpoint jumps upward. Measurement and ramp rate are unchanged.
        time.sleep(0.02)

        # Second edge is deactivation (sweeping in opposite direction)
        hw_min_psi, hw_max_psi = self._resolve_hardware_limits_test_reference()
        return_target = target_back + (self._precision_return_overshoot_psi * (-direction))
        return_target = min(hw_max_psi, max(hw_min_psi, return_target))
        if not math.isclose(return_target, target_back, abs_tol=1e-6):
            logger.debug(
                '%s: Precision return target expanded %.4f -> %.4f PSI (overshoot=%.4f)',
                self._port_id,
                target_back,
                return_target,
                self._precision_return_overshoot_psi,
            )
        if not self._port.alicat.set_ramp_rate(rate_psi_per_sec):
            if fail_on_rate_error:
                self._fail(
                    TestFailureCode.RAMP_RATE_FAILURE,
                    f'Failed to set return sweep ramp rate for {self._port_id}',
                )
            return SweepPassOutcome(result=None, missing_edge='rate_error')
        edge_back = self._sweep_to_edge(return_target, -direction, edge_type='deactivation')
        if self._cancel_event.is_set():
            return SweepPassOutcome(result=None, missing_edge='cancelled')
        if edge_back is None:
            logger.warning(
                '%s: Second edge not detected in precision return-sweep target=%.3f',
                self._port_id,
                return_target,
            )
            return SweepPassOutcome(result=None, missing_edge='second')

        return SweepPassOutcome(
            result=SweepResult(
                activation_psi=edge_out.pressure_psi,
                deactivation_psi=edge_back.pressure_psi,
            ),
            missing_edge=None,
        )

    def _sweep_to_edge(
        self,
        target_psi: float,
        direction: int,
        edge_type: Optional[str] = None,
    ) -> Optional[EdgeDetection]:
        """Sweep toward target, returning the first stable edge detected.
        
        Args:
            target_psi: Target pressure to sweep toward
            direction: Sweep direction (>0 for increasing, <0 for decreasing)
            edge_type: Optional type label ('activation' or 'deactivation') for immediate callback
        """
        target_abs = self._to_absolute(target_psi)
        self._set_pressure_or_raise(target_abs)

        reading_start = self._get_latest_reading(self._port_id)
        dynamic_timeout_s = self._edge_timeout_s
        rate_psi_per_sec = self._current_rate_psi_per_sec()
        if reading_start is not None:
            start_abs = self._reading_pressure_abs_psi(reading_start)
            if start_abs is not None:
                travel_psi = abs(target_abs - start_abs)
                estimated_travel_s = travel_psi / max(1e-4, rate_psi_per_sec)
                dynamic_timeout_s = max(self._edge_timeout_s, estimated_travel_s * 1.35 + 8.0)

        # Pre-initialize debounce with the current switch state from
        # reading_start so that the sweep can detect an edge even when the
        # approach target is very close to the activation/deactivation point.
        # Without this, the first loop sample might already be on the "other
        # side" of the edge and the debounce would treat it as the starting
        # state — never detecting a transition.
        debounce_state = SpdtDebounceState()
        if reading_start is not None and reading_start.switch is not None:
            debounce_state = self._spdt_debounce_from_switch(reading_start.switch)
        initial_switch_state: Optional[bool] = debounce_state.committed_activated

        if edge_type in ('activation', 'deactivation') and initial_switch_state is not None:
            want_activated = self._target_switch_state_for_edge(edge_type)
            if self._cycle_edge_switch_state_allowed(edge_type, initial_switch_state):
                start_abs = self._reading_pressure_abs_psi(reading_start) if reading_start else None
                if start_abs is not None:
                    edge_pressure = self._absolute_to_test_reference(start_abs)
                    if math.isfinite(edge_pressure):
                        tolerance = max(0.5, self._precision_approach_tolerance_psi)
                        if direction < 0 and edge_pressure > target_psi + tolerance:
                            logger.debug(
                                '%s: Initial %s switch state matches at %.4f PSI, '
                                'but pressure is not past target %.4f; waiting for transition',
                                self._port_id,
                                edge_type,
                                edge_pressure,
                                target_psi,
                            )
                        elif direction > 0 and edge_pressure < target_psi - tolerance:
                            logger.debug(
                                '%s: Initial %s switch state matches at %.4f PSI, '
                                'but pressure is not past target %.4f; waiting for transition',
                                self._port_id,
                                edge_type,
                                edge_pressure,
                                target_psi,
                            )
                        else:
                            if self._on_edge_detected:
                                self._on_edge_detected(edge_type, edge_pressure)
                            return EdgeDetection(
                                pressure_psi=edge_pressure,
                                activated=want_activated,
                            )

        start = time.perf_counter()
        target_reached_since: Optional[float] = None
        settle_window_s = max(0.08, self._stable_count * 0.02)
        target_grace_s = settle_window_s + self._precision_post_target_grace_s

        while time.perf_counter() - start < dynamic_timeout_s:
            if self._cancel_event.is_set():
                return None

            reading = self._get_latest_reading(self._port_id)
            if reading is None or reading.switch is None:
                time.sleep(0.01)
                continue

            pressure_abs = self._reading_pressure_abs_psi(reading)
            if pressure_abs is None:
                time.sleep(0.01)
                continue
            pressure = self._absolute_to_test_reference(pressure_abs)
            sw = reading.switch
            no_active = bool(sw.no_active)
            nc_active = bool(sw.nc_active)
            current_state = collapse_switch_activated(no_active=no_active, nc_active=nc_active)

            if (
                debounce_state.last_no is not None
                and (no_active != debounce_state.last_no or nc_active != debounce_state.last_nc)
            ):
                logger.debug(
                    '%s: Switch transition during sweep at %.4f PSI (to %s)',
                    self._port_id,
                    pressure,
                    current_state,
                )

            debounce_state, committed_state, committed_pressure = observe_spdt_transition(
                debounce_state,
                no_active,
                nc_active,
                self._stable_count,
                self._min_edge_interval_s,
                time.perf_counter(),
                current_pressure=pressure,
            )
            if committed_state is not None and math.isfinite(pressure):
                if edge_type in ('activation', 'deactivation'):
                    if not self._cycle_edge_switch_state_allowed(edge_type, committed_state):
                        continue
                # Use the first-detection pressure for better accuracy
                edge_pressure = committed_pressure if committed_pressure is not None else pressure
                # Report edge immediately if callback is available
                if self._on_edge_detected and edge_type:
                    self._on_edge_detected(edge_type, edge_pressure)
                return EdgeDetection(pressure_psi=edge_pressure, activated=committed_state)

            reached_target = False
            if direction > 0 and pressure_abs >= target_abs:
                reached_target = True
            if direction < 0 and pressure_abs <= target_abs:
                reached_target = True

            if reached_target:
                now = time.perf_counter()
                if target_reached_since is None:
                    target_reached_since = now
                elif now - target_reached_since >= target_grace_s:
                    logger.debug(
                        '%s: Sweep target reached without stable edge target=%.4f dir=%s hold=%.3fs',
                        self._port_id,
                        target_psi,
                        'increasing' if direction > 0 else 'decreasing',
                        now - target_reached_since,
                    )
                    break
            else:
                target_reached_since = None

            time.sleep(0.01)

        return None

    def _current_rate_psi_per_sec(self) -> float:
        """Best-effort current commanded precision sweep rate in PSI/s."""
        return max(1e-4, self._slow_edge_rate_psi)

    def _emit_substate(self, substate: str) -> None:
        if self._on_substate_update:
            self._on_substate_update(substate)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _wait_for_atmosphere(
        self,
        atmosphere_psi: float,
        timeout_s: float,
        collect_cycle_edges: bool = False,
        hold_s: float = 0.0,
        tolerance_psi: Optional[float] = None,
    ) -> bool:
        """Wait for port pressure to return near atmosphere after venting."""
        threshold_psi = max(0.05, tolerance_psi if tolerance_psi is not None else self._atmosphere_tolerance_psi)
        start = time.perf_counter()
        near_since: Optional[float] = None

        while time.perf_counter() - start < timeout_s:
            if self._cancel_event.is_set():
                return False

            reading = self._get_latest_reading(self._port_id)
            pressure = self._reading_pressure_for_wait(reading, collect_cycle_edges)
            if pressure is None:
                time.sleep(0.05)
                continue

            if abs(pressure - atmosphere_psi) <= threshold_psi:
                now = time.perf_counter()
                if near_since is None:
                    near_since = now
                    if hold_s > 0.0:
                        logger.info(
                            '%s: Atmosphere reached pressure=%.4f target=%.4f tol=%.3f hold=%.2fs',
                            self._port_id,
                            pressure,
                            atmosphere_psi,
                            threshold_psi,
                            hold_s,
                        )
                if now - near_since >= max(0.0, hold_s):
                    if hold_s > 0.0:
                        logger.info('%s: Atmosphere hold complete (%.2fs)', self._port_id, hold_s)
                    return True
            else:
                near_since = None

            time.sleep(0.05)

        logger.warning(
            '%s: Timeout (%.1fs) waiting for atmosphere target=%.4f tol=%.3f',
            self._port_id,
            timeout_s,
            atmosphere_psi,
            threshold_psi,
        )
        return False

    def _reading_pressure_for_wait(
        self,
        reading: Optional[PortReading],
        collect_cycle_edges: bool,
    ) -> Optional[float]:
        _pressure_abs, pressure_test = self._extract_pressures(
            reading,
            collect_cycle_edges=collect_cycle_edges,
        )
        if pressure_test is None:
            return None
        return pressure_test

    def _extract_pressures(
        self,
        reading: Optional[PortReading],
        collect_cycle_edges: bool = False,
    ) -> tuple[Optional[float], Optional[float]]:
        if reading is None:
            return None, None
        pressure_abs = self._reading_pressure_abs_psi(reading)
        if pressure_abs is None:
            return None, None
        pressure_test = self._absolute_to_test_reference(pressure_abs)
        if collect_cycle_edges:
            self._observe_cycle_switch_sample(pressure_test, reading.switch)
        return pressure_abs, pressure_test

    def _resolve_activation_sweep_direction(self) -> int:
        """Resolve precision sweep direction from PTP activation direction."""
        direction = (self._test_setup.activation_direction or '').strip().lower() if self._test_setup else ''
        if direction.startswith('decreas') or direction in {'down', 'falling'}:
            return -1
        return 1

    def _resolve_precision_targets(
        self,
        min_psi: float,
        max_psi: float,
        activation_direction: int,
    ) -> tuple[float, float, float, str]:
        """Resolve precision approach/out/back targets.

        Close-limit semantics:
        - Fast approach to the nearest deactivation boundary from atmosphere.
        - Slow sweep from that close limit to the activation-side boundary.
        - Reverse back to the close limit to capture the return edge.
        """
        activation_estimate, deactivation_estimate = self._ordered_cycle_estimates()

        if activation_estimate is not None and deactivation_estimate is not None:
            offset = self._precision_close_limit_offset_psi
            margin = self._precision_deactivation_margin_psi
            # Get hardware limits as absolute bounds (allow going slightly outside PTP bounds if needed)
            hw_min, hw_max = self._resolve_hardware_limits_test_reference()

            # Resolve the PTP activation band so the sweep is guaranteed to
            # cover the full acceptance window even when the cycle estimate
            # is biased away from the real activation point.
            act_band = self._resolve_activation_band_psi(activation_direction, min_psi, max_psi)

            if activation_direction < 0:
                # Decreasing activation: activation at lower pressure,
                # deactivation at higher pressure.
                # Approach from above, just above the activation point.
                approach_target = min(max_psi, activation_estimate + offset)
                # Slow sweep down past activation to find the activation edge.
                target_out = max(hw_min, activation_estimate - offset)
                # Widen to cover the activation band if the estimate is off
                if act_band:
                    approach_target = max(approach_target, act_band[1] + offset * 0.25)
                    target_out = min(target_out, act_band[0] - offset * 0.25)
                    approach_target = min(hw_max, approach_target)
                    target_out = max(hw_min, target_out)
                # Reverse sweep up past deactivation to find the deactivation edge.
                target_back = min(hw_max, deactivation_estimate + margin)
                if self._resolve_sweep_mode() == 'vacuum':
                    target_back = min(hw_max, max(target_back, max_psi + offset))
            else:
                # Increasing activation: activation at higher pressure,
                # deactivation at lower pressure.
                # Approach from below, just below the activation point.
                approach_target = max(min_psi, activation_estimate - offset)
                # Slow sweep up past activation to find the activation edge.
                target_out = min(hw_max, activation_estimate + offset)
                # Widen to cover the activation band if the estimate is off
                if act_band:
                    approach_target = min(approach_target, act_band[0] - offset * 0.25)
                    target_out = max(target_out, act_band[1] + offset * 0.25)
                    approach_target = max(hw_min, approach_target)
                    target_out = min(hw_max, target_out)
                # Reverse sweep down past deactivation to find the deactivation edge.
                target_back = max(hw_min, deactivation_estimate - margin)
                if self._uses_nc_derived_vacuum_window():
                    approach_target = max(hw_min, min(approach_target, min_psi - offset * 0.25))
            validation_error = self._validate_cycle_estimate_targets(
                activation_direction=activation_direction,
                approach_target=approach_target,
                target_out=target_out,
                target_back=target_back,
                activation_estimate=activation_estimate,
                deactivation_estimate=deactivation_estimate,
            )
            if validation_error is None:
                oob_parts: list[str] = []
                if activation_direction < 0:
                    if target_out < min_psi:
                        oob_parts.append(f'target_out below PTP min={min_psi:.4f}')
                    if target_back > max_psi:
                        oob_parts.append(f'target_back above PTP max={max_psi:.4f}')
                else:
                    if target_out > max_psi:
                        oob_parts.append(f'target_out above PTP max={max_psi:.4f}')
                    if target_back < min_psi:
                        oob_parts.append(f'target_back below PTP min={min_psi:.4f}')
                out_of_bounds_note = f' ({"; ".join(oob_parts)})' if oob_parts else ''
                logger.info(
                    '%s: Precision targets from cycle estimates: approach=%.4f out=%.4f back=%.4f '
                    '(act_est=%.4f deact_est=%.4f offset=%.4f margin=%.4f)%s',
                    self._port_id,
                    approach_target,
                    target_out,
                    target_back,
                    activation_estimate,
                    deactivation_estimate,
                    offset,
                    margin,
                    out_of_bounds_note,
                )
                return (approach_target, target_out, target_back, 'cycle-estimate-offset-close-limit')
            logger.warning(
                '%s: Rejecting cycle-estimate precision targets: %s '
                '(direction=%s act_est=%.4f deact_est=%.4f approach=%.4f out=%.4f back=%.4f)',
                self._port_id,
                validation_error,
                'increasing' if activation_direction > 0 else 'decreasing',
                activation_estimate,
                deactivation_estimate,
                approach_target,
                target_out,
                target_back,
            )

        if self._test_setup:
            if activation_direction < 0:
                activation_band = self._band_limits_to_psi(
                    self._test_setup.bands.get('decreasing'),
                    min_psi,
                    max_psi,
                )
                deactivation_band = self._band_limits_to_psi(
                    self._test_setup.bands.get('increasing'),
                    min_psi,
                    max_psi,
                )
                if activation_band and deactivation_band:
                    act_low, _act_high = activation_band
                    _deact_low, deact_high = deactivation_band
                    if deact_high > act_low:
                        close_limit = deact_high
                        logger.info(
                            '%s: Precision targets from PTP close-limit (decreasing): '
                            'approach=%.4f out=%.4f back=%.4f',
                            self._port_id,
                            close_limit,
                            act_low,
                            close_limit,
                        )
                        return (close_limit, act_low, close_limit, 'ptp-close-limit')
            else:
                activation_band = self._band_limits_to_psi(
                    self._test_setup.bands.get('increasing'),
                    min_psi,
                    max_psi,
                )
                deactivation_band = self._band_limits_to_psi(
                    self._test_setup.bands.get('decreasing'),
                    min_psi,
                    max_psi,
                )
                if activation_band and deactivation_band:
                    _act_low, act_high = activation_band
                    deact_low, _deact_high = deactivation_band
                    if act_high > deact_low:
                        close_limit = deact_low
                        logger.info(
                            '%s: Precision targets from PTP close-limit (increasing): '
                            'approach=%.4f out=%.4f back=%.4f',
                            self._port_id,
                            close_limit,
                            act_high,
                            close_limit,
                        )
                        return (close_limit, act_high, close_limit, 'ptp-close-limit')

        if activation_direction < 0:
            logger.info(
                '%s: Precision targets from bounds close-limit (decreasing): '
                'approach=%.4f out=%.4f back=%.4f',
                self._port_id,
                max_psi,
                min_psi,
                max_psi,
            )
            return (max_psi, min_psi, max_psi, 'bounds-close-limit')
        logger.info(
            '%s: Precision targets from bounds close-limit (increasing): '
            'approach=%.4f out=%.4f back=%.4f',
            self._port_id,
            min_psi,
            max_psi,
            min_psi,
        )
        return (min_psi, max_psi, min_psi, 'bounds-close-limit')

    def _validate_cycle_estimate_targets(
        self,
        activation_direction: int,
        approach_target: float,
        target_out: float,
        target_back: float,
        activation_estimate: float,
        deactivation_estimate: float,
    ) -> Optional[str]:
        eps = 1e-6
        if activation_direction > 0:
            if activation_estimate <= deactivation_estimate + eps:
                return 'activation estimate is not above deactivation estimate for increasing direction'
            if not (approach_target < target_out):
                return 'approach/out ordering does not sweep upward for increasing direction'
            if not (approach_target <= activation_estimate <= target_out):
                return 'activation estimate is not bracketed by increasing out-sweep'
            if not (target_back <= deactivation_estimate <= target_out):
                return 'deactivation estimate is not bracketed by return sweep'
            return None

        if activation_estimate >= deactivation_estimate - eps:
            return 'activation estimate is not below deactivation estimate for decreasing direction'
        if not (approach_target > target_out):
            return 'approach/out ordering does not sweep downward for decreasing direction'
        if not (approach_target >= activation_estimate >= target_out):
            return 'activation estimate is not bracketed by decreasing out-sweep'
        if not (target_back >= deactivation_estimate >= target_out):
            return 'deactivation estimate is not bracketed by return sweep'
        return None

    def _band_limits_to_psi(
        self,
        band: Optional[Dict[str, Optional[float]]],
        min_psi: float,
        max_psi: float,
    ) -> Optional[tuple[float, float]]:
        if not band or not self._test_setup:
            return None
        units_label = self._test_setup.units_label or 'PSI'
        lower = band.get('lower')
        upper = band.get('upper')
        if lower is None or upper is None:
            return None
        lower_psi = convert_pressure(lower, units_label, 'PSI')
        upper_psi = convert_pressure(upper, units_label, 'PSI')
        if not math.isfinite(lower_psi):
            lower_psi = min_psi
        if not math.isfinite(upper_psi):
            upper_psi = max_psi
        low, high = sorted((lower_psi, upper_psi))
        if not math.isfinite(low) or not math.isfinite(high):
            return None
        return (low, high)

    def _resolve_activation_band_psi(
        self,
        activation_direction: int,
        min_psi: float,
        max_psi: float,
    ) -> Optional[tuple[float, float]]:
        """Return the PTP activation acceptance band in PSI, if available."""
        if not self._test_setup:
            return None
        band_key = 'decreasing' if activation_direction < 0 else 'increasing'
        return self._band_limits_to_psi(
            self._test_setup.bands.get(band_key), min_psi, max_psi,
        )

    def _ensure_vacuum_solenoid_route(self) -> None:
        """Vent on atmosphere, then switch to vacuum (same path as verify_switch_config)."""
        if self._cancel_event.is_set():
            return
        baro_psi = self._get_barometric_psi(self._port_id)
        logger.info(
            '%s: Preparing vacuum route (vent then vacuum solenoid, baro=%.2f psia)',
            self._port_id,
            baro_psi,
        )
        prepare = getattr(self._port, 'prepare_vacuum_route_for_test', None)
        if callable(prepare):
            if not prepare(baro_psi):
                self._fail(
                    TestFailureCode.ROUTE_FAILURE,
                    f'Failed to set vacuum solenoid route on {self._port_id}',
                )
            return
        self._port.vent_to_atmosphere()
        if not self._port.set_solenoid(to_vacuum=True):
            self._fail(
                TestFailureCode.ROUTE_FAILURE,
                f'Failed to set vacuum solenoid route on {self._port_id}',
            )

    def _set_active_test_route(self, sweep_mode: str, context: str) -> None:
        """Connect the DUT to the Alicat-controlled test line for an active move."""
        if sweep_mode == 'pressure':
            connect = getattr(self._port, 'connect_test_route', None)
            success = connect() if callable(connect) else self._port.set_solenoid(to_vacuum=True)
        else:
            success = self._port.set_solenoid(to_vacuum=True)
        if not success:
            self._fail(
                TestFailureCode.ROUTE_FAILURE,
                f'Failed to set active test route for {context} on {self._port_id} ({sweep_mode})',
            )

    def _stage_atmosphere_setpoint_before_route(self) -> None:
        """Move the controller command to atmosphere before connecting the DUT."""
        staging_rate = max(self._fast_rate_psi * 10.0, 50.0)
        if not self._port.alicat.set_ramp_rate(staging_rate):
            logger.warning(
                '%s: High atmosphere staging ramp %.4f PSI/s rejected; falling back to %.4f PSI/s',
                self._port_id,
                staging_rate,
                self._fast_rate_psi,
            )
            staging_rate = self._fast_rate_psi
            if not self._port.alicat.set_ramp_rate(staging_rate):
                self._fail(
                    TestFailureCode.RAMP_RATE_FAILURE,
                    f'Failed to set atmosphere staging ramp rate for {self._port_id}',
                )
        baro_psi = self._get_barometric_psi(self._port_id)
        self._set_pressure_or_raise(baro_psi)
        self._port.alicat.cancel_hold()
        time.sleep(min(3.0, max(0.35, baro_psi / max(staging_rate, 0.1) + 0.1)))

    def _resolve_sweep_mode(self) -> str:
        """Determine whether to sweep in pressure or vacuum direction."""
        return resolve_sweep_mode(
            self._test_setup,
            atmosphere_psi=self._get_barometric_psi(self._port_id),
        )

    def _resolve_sweep_bounds(self) -> tuple[float, float]:
        """Get sweep bounds from PTP or config fallback."""
        labjack_cfg = self._config.get('hardware', {}).get('labjack', {})
        port_cfg = labjack_cfg.get(self._port_id, {})
        return resolve_sweep_bounds(self._test_setup, port_cfg)

    def _vacuum_switch_trips_on_no_open(self) -> bool:
        port_cfg = self._config.get('hardware', {}).get('labjack', {}).get(self._port_id, {})
        # PTP resolution can change which physical contact the single sensed
        # DB9 pin represents from part to part. Prefer the live resolved mode;
        # the port-level knob is only a fallback/default when no PTP-derived
        # single-sense mode has been applied yet.
        daq = getattr(self._port, 'daq', None)
        if daq is not None:
            derived_from_no = bool(getattr(daq, 'switch_nc_derived_from_no', False))
            if derived_from_no:
                return True
            derived_from_nc = bool(getattr(daq, 'switch_no_derived_from_nc', False))
            if derived_from_nc:
                return False

        if 'vacuum_switch_trips_on_no_open' in port_cfg:
            return _config_bool(port_cfg.get('vacuum_switch_trips_on_no_open'))

        # Single-wire switch sensing derives NC from NO. On the current vacuum
        # bench wiring, the trip opens NO; default that way when the more
        # explicit knob is missing so activation/deactivation are not inverted.
        return _config_bool(port_cfg.get('switch_nc_derived_from_no'))

    def _cycle_target_switch_state(self, edge_type: str) -> bool:
        """``switch_activated`` value that means this cycle edge (vacuum bench may invert)."""
        if (
            self._resolve_sweep_mode() == 'pressure'
            and self._resolve_activation_sweep_direction() < 0
        ):
            daq = getattr(self._port, 'daq', None)
            if daq is not None and bool(getattr(daq, 'switch_nc_derived_from_no', False)):
                return edge_type != 'activation'
        if edge_type == 'activation':
            if self._resolve_sweep_mode() == 'vacuum' and self._vacuum_switch_trips_on_no_open():
                return False
            return True
        if self._resolve_sweep_mode() == 'vacuum' and self._vacuum_switch_trips_on_no_open():
            return True
        return False

    def _cycle_edge_switch_state_allowed(self, edge_type: str, switch_activated: bool) -> bool:
        """Return whether a cycle transition can represent the requested edge."""
        observed_state = self._cycle_observed_edge_states.get(edge_type)
        if observed_state is not None:
            return switch_activated == observed_state
        if (
            self._resolve_sweep_mode() == 'vacuum'
            and self._resolve_activation_sweep_direction() < 0
            and self._vacuum_switch_trips_on_no_open()
        ):
            return True
        return switch_activated == self._cycle_target_switch_state(edge_type)

    def _target_switch_state_for_edge(self, edge_type: str) -> bool:
        """``switch_activated`` value that represents the requested physical edge."""
        observed_state = self._cycle_observed_edge_states.get(edge_type)
        if observed_state is not None:
            return observed_state
        return self._cycle_target_switch_state(edge_type)

    def _cycle_edge_already_present(
        self,
        edge_type: str,
        pressure_test: Optional[float],
        switch_state: Optional[bool],
    ) -> bool:
        """True when the switch is already in the post-edge state for this leg."""
        if pressure_test is None or switch_state is None or not math.isfinite(pressure_test):
            return False
        if switch_state != self._cycle_target_switch_state(edge_type):
            return False
        bounds = self._resolve_sweep_bounds()
        if edge_type == 'activation' and self._resolve_sweep_mode() == 'vacuum':
            # Only accept priming deep on the vacuum side of the band.  Narrow
            # low-absolute Torr switches can sit near the previous reset edge
            # between cycles; treating that as a fresh activation starves the
            # following reset leg of the real transition.
            if (
                self._resolve_activation_sweep_direction() < 0
                and self._vacuum_switch_trips_on_no_open()
            ):
                return False
            return (
                bounds[0] - 0.5 <= pressure_test <= bounds[0] + 0.5
                and switch_state == self._cycle_target_switch_state(edge_type)
            )
        if edge_type == 'deactivation' and self._resolve_sweep_mode() == 'vacuum':
            return False
        return True

    def _cycle_edge_pressure_allowed(self, edge_type: str, pressure_test: float) -> bool:
        """Return False for cycle edges that are clearly outside the PTP traverse window."""
        if not math.isfinite(pressure_test):
            return False
        if edge_type == 'activation' and self._resolve_activation_sweep_direction() < 0:
            min_psi, max_psi = self._resolve_sweep_bounds()
            if self._resolve_sweep_mode() == 'pressure':
                # Gauge-pressure parts reset above the deactivation band; the
                # activation edge can occur anywhere from the actuation band up
                # to that reset side during the descending sweep.
                return pressure_test <= max_psi + 0.5
            activation_band = self._resolve_activation_band_psi(-1, min_psi, max_psi)
            deactivation_band = (
                self._band_limits_to_psi(
                    self._test_setup.bands.get('increasing'),
                    min_psi,
                    max_psi,
                )
                if self._test_setup
                else None
            )
            if activation_band and deactivation_band:
                _act_low, act_high = activation_band
                _deact_low, deact_high = deactivation_band
                if deact_high > act_high:
                    return pressure_test <= act_high + ((deact_high - act_high) * 0.5)
            return pressure_test <= max_psi + 0.5
        if edge_type == 'deactivation' and self._resolve_sweep_mode() == 'vacuum':
            min_psi, max_psi = self._resolve_sweep_bounds()
            return min_psi - 0.5 <= pressure_test <= max_psi + 1.0
        return True

    def _ptp_limits_use_psia_scale(self) -> bool:
        from app.services.sweep_utils import ptp_limits_use_psia_scale

        labjack_cfg = self._config.get('hardware', {}).get('labjack', {})
        port_cfg = labjack_cfg.get(self._port_id, {})
        return ptp_limits_use_psia_scale(
            self._test_setup,
            port_cfg,
            self._get_barometric_psi(self._port_id),
        )

    def _determine_atmosphere_psi(self) -> float:
        """Return the atmosphere target in test-reference PSI (live baro when PSIA scale)."""
        if self._ptp_limits_use_psia_scale():
            return self._get_barometric_psi(self._port_id)
        pressure_ref = self._test_setup.pressure_reference if self._test_setup else None
        if str(pressure_ref or '').strip().lower() == 'absolute':
            return self._get_barometric_psi(self._port_id)
        return 0.0

    def _to_absolute(self, value_psi: float) -> float:
        """Convert a PSI value to absolute if PTP is gauge-referenced."""
        if self._ptp_limits_use_psia_scale():
            return value_psi
        pressure_ref = self._test_setup.pressure_reference if self._test_setup else None
        return to_absolute_pressure(value_psi, pressure_ref, self._get_barometric_psi(self._port_id))

    def _cycle_ramp_target_test_reference(self, target_psi: float) -> float:
        """Express cycle traverse limits in the same reference as ``pressure_test``."""
        if self._ptp_limits_use_psia_scale():
            return float(target_psi)
        return float(target_psi)

    @staticmethod
    def _effective_switch_state(switch_state: Any) -> bool:
        if switch_state is None:
            return False
        return collapse_switch_activated(
            no_active=bool(getattr(switch_state, 'no_active', False)),
            nc_active=bool(getattr(switch_state, 'nc_active', False)),
        )

    @staticmethod
    def _spdt_debounce_from_switch(switch_state: Any) -> SpdtDebounceState:
        no_active = bool(getattr(switch_state, 'no_active', False))
        nc_active = bool(getattr(switch_state, 'nc_active', False))
        activated = collapse_switch_activated(no_active=no_active, nc_active=nc_active)
        return SpdtDebounceState(
            last_no=no_active,
            last_nc=nc_active,
            committed_activated=activated,
        )

    def _emit_cycle_estimate_from_samples(self) -> None:
        activation, deactivation = self._ordered_cycle_estimates()
        count = max(len(self._cycle_activation_samples), len(self._cycle_deactivation_samples))
        if self._on_cycle_estimate:
            self._on_cycle_estimate(activation, deactivation, count)

    def _observe_cycle_switch_sample(self, pressure_test_psi: float, switch_state: Any) -> None:
        if switch_state is None:
            return
        no_active = bool(getattr(switch_state, 'no_active', False))
        nc_active = bool(getattr(switch_state, 'nc_active', False))
        before_debounce = self._cycle_debounce_state
        self._cycle_debounce_state, committed_state, committed_pressure = observe_spdt_transition(
            self._cycle_debounce_state,
            no_active,
            nc_active,
            self._cycle_stable_count,
            self._min_edge_interval_s,
            time.perf_counter(),
            current_pressure=pressure_test_psi,
        )
        if committed_state is None:
            return
        activated = bool(committed_state)

        if not self._cycle_edge_switch_state_allowed(self._cycle_waiting_edge or '', activated):
            self._cycle_debounce_state = before_debounce
            return

        if (
            self._cycle_waiting_edge in {'activation', 'deactivation'}
            and not self._cycle_edge_pressure_allowed(self._cycle_waiting_edge, pressure_test_psi)
        ):
            logger.debug(
                '%s: Ignoring %s edge at %.4f PSI outside cycle acceptance window',
                self._port_id,
                self._cycle_waiting_edge,
                pressure_test_psi,
            )
            self._cycle_debounce_state = before_debounce
            return

        if not getattr(switch_state, 'is_valid', True):
            logger.debug(
                '%s: Switch edge with complementary NO/NC (NO=%s NC=%s)',
                self._port_id,
                no_active,
                nc_active,
            )

        # Use the first-detection pressure for better accuracy during fast ramps
        sample_pressure = committed_pressure if committed_pressure is not None else pressure_test_psi
        if (
            self._cycle_waiting_edge in {'activation', 'deactivation'}
            and not self._cycle_edge_pressure_allowed(self._cycle_waiting_edge, sample_pressure)
        ):
            logger.debug(
                '%s: Ignoring %s first-detection pressure %.4f PSI outside cycle acceptance window',
                self._port_id,
                self._cycle_waiting_edge,
                sample_pressure,
            )
            self._cycle_debounce_state = before_debounce
            return
        if self._cycle_waiting_edge == 'activation':
            self._cycle_observed_edge_states['activation'] = activated
            self._cycle_activation_samples.append(sample_pressure)
        elif self._cycle_waiting_edge == 'deactivation':
            self._cycle_observed_edge_states['deactivation'] = activated
            self._cycle_deactivation_samples.append(sample_pressure)

        activation, deactivation = self._ordered_cycle_estimates()
        count = max(len(self._cycle_activation_samples), len(self._cycle_deactivation_samples))
        logger.info(
            '%s: Cycle %s sample at %.4f PSI (act=%s deact=%s count=%d)',
            self._port_id,
            self._cycle_waiting_edge or 'edge',
            sample_pressure,
            f'{activation:.4f}' if activation is not None else '--',
            f'{deactivation:.4f}' if deactivation is not None else '--',
            count,
        )
        self._emit_cycle_estimate_from_samples()

    def _ordered_cycle_estimates(self) -> tuple[Optional[float], Optional[float]]:
        """Return cycle estimates ordered consistently with the activation direction.

        For increasing activation, activation should be above deactivation.
        For decreasing activation, activation should be below deactivation.
        If the raw switch-state labels produced the opposite ordering, swap
        them so downstream consumers (display and precision targets) see
        the correct semantics.
        """
        activation = self._mean_or_none(self._cycle_activation_samples)
        deactivation = self._mean_or_none(self._cycle_deactivation_samples)
        if activation is not None and deactivation is not None:
            direction = self._resolve_activation_sweep_direction()
            needs_swap = (
                (direction > 0 and activation < deactivation)
                or (direction < 0 and activation > deactivation)
            )
            if needs_swap:
                logger.debug(
                    '%s: Reordering cycle estimates for %s direction: '
                    'raw act=%.4f deact=%.4f -> swapped',
                    self._port_id,
                    'increasing' if direction > 0 else 'decreasing',
                    activation,
                    deactivation,
                )
                activation, deactivation = deactivation, activation
        return activation, deactivation

    def _uses_nc_derived_vacuum_window(self) -> bool:
        daq = getattr(self._port, 'daq', None)
        return (
            self._resolve_sweep_mode() == 'vacuum'
            and self._resolve_activation_sweep_direction() > 0
            and bool(getattr(daq, 'switch_no_derived_from_nc', False))
        )

    @staticmethod
    def _mean_or_none(values: list[float]) -> Optional[float]:
        if not values:
            return None
        return sum(values) / len(values)

    def _reading_pressure_abs_psi(self, reading: PortReading) -> Optional[float]:
        """Pressure for ramp/wait/edge control — transducer first (physical line)."""
        measurement_settings = get_measurement_settings(self._config)
        barometric_psi = self._get_barometric_psi(self._port_id)
        pressure_abs, _source_used = select_ui_pressure_abs_psi(
            reading=reading,
            settings=measurement_settings,
            barometric_psi=barometric_psi,
        )
        if pressure_abs is not None:
            return pressure_abs
        pressure_abs, _source_used = select_main_pressure_abs_psi(
            reading=reading,
            settings=measurement_settings,
            barometric_psi=barometric_psi,
        )
        return pressure_abs

    def _absolute_to_test_reference(self, pressure_abs_psi: float) -> float:
        if self._ptp_limits_use_psia_scale():
            return pressure_abs_psi
        pressure_ref = self._test_setup.pressure_reference if self._test_setup else None
        if str(pressure_ref or '').strip().lower() == 'gauge':
            return pressure_abs_psi - self._get_barometric_psi(self._port_id)
        return pressure_abs_psi

    def _reading_pressure_test_psi(self, reading: PortReading) -> Optional[float]:
        pressure_abs = self._reading_pressure_abs_psi(reading)
        if pressure_abs is None:
            return None
        return self._absolute_to_test_reference(pressure_abs)

    def _resolve_hardware_limits_test_reference(self) -> tuple[float, float]:
        labjack_cfg = self._config.get('hardware', {}).get('labjack', {})
        port_cfg = labjack_cfg.get(self._port_id, {})
        min_abs = float(port_cfg.get('transducer_pressure_min', 0.0))
        max_abs = float(port_cfg.get('transducer_pressure_max', 115.0))
        min_ref = self._absolute_to_test_reference(min_abs)
        max_ref = self._absolute_to_test_reference(max_abs)
        if min_ref <= max_ref:
            return (min_ref, max_ref)
        return (max_ref, min_ref)

    def _safe_vent(self) -> None:
        """Safely vent to atmosphere."""
        try:
            self._port.vent_to_atmosphere()
        except Exception as exc:
            logger.error('%s: Failed to vent: %s', self._port_id, exc)

    def _ensure_alicat_units(self) -> None:
        """Best-effort sync of Alicat display units with current test setup."""
        units_code = self._test_setup.units_code if self._test_setup else None
        if not units_code:
            return
        try:
            ok = self._port.alicat.configure_units_from_ptp(units_code)
            if not ok:
                logger.warning(
                    '%s: Alicat units verify failed (requested code=%s); continuing with current controller units',
                    self._port_id,
                    units_code,
                )
        except Exception as exc:
            logger.warning(
                '%s: Failed to enforce Alicat units %s: %s; continuing with current controller units',
                self._port_id,
                units_code,
                exc,
            )

    def _lock_alicat_setpoint_reference(self) -> None:
        baro = self._get_barometric_psi(self._port_id)
        ptp_ref = (
            self._test_setup.pressure_reference if self._test_setup else None
        )
        alicat_cfg = self._config.get('hardware', {}).get('alicat', {})
        port_cfg = alicat_cfg.get(self._port_id, {})
        config_ref = port_cfg.get('setpoint_reference')
        self._alicat_setpoint_ref = resolve_alicat_setpoint_reference_for_test(
            ptp_pressure_reference=ptp_ref,
            ptp_units_label=self._test_setup.units_label if self._test_setup else None,
            config_reference=str(config_ref) if config_ref is not None else None,
            reading=self._get_latest_reading(self._port_id),
            barometric_psi=baro,
        )
        logger.info(
            '%s: Alicat setpoint reference locked to %s for this test run',
            self._port_id,
            self._alicat_setpoint_ref,
        )

    def _resolve_alicat_setpoint_reference(self) -> str:
        if self._alicat_setpoint_ref:
            return self._alicat_setpoint_ref
        self._lock_alicat_setpoint_reference()
        return self._alicat_setpoint_ref or 'absolute'

    def _set_pressure_or_raise(self, target_abs_psi: float) -> None:
        """Set pressure with one recovery retry before raising."""
        baro = self._get_barometric_psi(self._port_id)
        setpoint_ref = self._resolve_alicat_setpoint_reference()
        command_psi = to_alicat_setpoint_psi(
            target_abs_psi,
            barometric_psi=baro,
            setpoint_reference=setpoint_ref,
        )
        logger.info(
            '%s: Alicat setpoint command %.4f PSI (target_abs=%.4f ref=%s baro=%.4f)',
            self._port_id,
            command_psi,
            target_abs_psi,
            setpoint_ref,
            baro,
        )
        if self._port.set_pressure(command_psi):
            return

        logger.warning(
            '%s: Set pressure failed once (command=%.4f target_abs=%.4f); retrying after unit sync',
            self._port_id,
            command_psi,
            target_abs_psi,
        )
        self._ensure_alicat_units()
        self._port.alicat.cancel_hold()
        time.sleep(0.05)
        command_psi = to_alicat_setpoint_psi(
            target_abs_psi,
            barometric_psi=baro,
            setpoint_reference=setpoint_ref,
        )

        if self._port.set_pressure(command_psi):
            return

        self._fail(
            TestFailureCode.PRESSURE_COMMAND_FAILURE,
            f'Failed to set pressure to {target_abs_psi:.4f} PSI absolute '
            f'(command {command_psi:.4f} PSI {setpoint_ref})',
        )

    def _cancel_and_emit(self) -> bool:
        if not self._cancel_event.is_set():
            return False
        logger.info('%s: Test sequence cancelled before completion', self._port_id)
        self._emit_event('run_cancelled')
        self._safe_vent()
        if self._on_cancelled:
            self._on_cancelled()
        return True

    def _abort_with_error(self, failure: TestFailure) -> None:
        """Abort test execution due to failure.
        
        Order of operations:
        1. Emit failure event (for logging/tracking)
        2. Notify error callback (triggers state machine transition)
        3. Safely vent port (after error is logged/recorded)
        
        Note: For NO_SWITCH_DETECTED failures, venting should already have happened
        in the cycle phase runner, but we ensure it here as well.
        """
        logger.error(
            '%s: Test aborted with failure [%s]: %s',
            self._port_id,
            failure.code.value,
            failure.message,
        )
        self._emit_event('run_failed', code=failure.code.value, message=failure.message)
        self._notify_error(failure)
        # Vent after error is logged and state machine is notified
        # This ensures error state is set before hardware cleanup
        # For NO_SWITCH_DETECTED, venting already happened, but ensure it's done
        self._safe_vent()

    def _notify_error(self, failure: TestFailure) -> None:
        if self._on_error:
            self._on_error(f'[{failure.code.value}] {failure.message}')

    def _emit_event(self, event_type: str, **data: Any) -> None:
        if not self._on_event:
            return
        self._on_event(TestEvent(event_type=event_type, port_id=self._port_id, data=data))

    @staticmethod
    def _fail(code: TestFailureCode, message: str) -> None:
        raise TestFailure(code, message)
