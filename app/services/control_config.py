"""Typed control configuration for test execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ControlConfigError(ValueError):
    """Raised when control configuration is invalid in strict mode."""


@dataclass(frozen=True)
class RampsConfig:
    precision_sweep_rate_torr_per_sec: float
    precision_edge_rate_torr_per_sec: float
    low_pressure_precision_threshold_psi: float
    low_pressure_precision_sweep_rate_torr_per_sec: float
    fast_cycle_rate_psi_per_sec: float
    pre_approach_rate_multiplier: float


@dataclass(frozen=True)
class CyclingConfig:
    num_cycles: int


@dataclass(frozen=True)
class EdgeDetectionConfig:
    overshoot_beyond_limit_percent: float
    timeout_sec: float
    atmosphere_tolerance_psi: float
    precision_approach_tolerance_torr: float
    precision_approach_settle_sec: float
    precision_start_atmosphere_hold_sec: float
    precision_close_limit_offset_torr: float
    precision_prepass_nudge_torr: float
    precision_deactivation_margin_torr: float
    precision_post_target_grace_sec: float
    precision_return_overshoot_torr: float


@dataclass(frozen=True)
class DebounceConfig:
    stable_sample_count: int
    min_edge_interval_ms: float


@dataclass(frozen=True)
class ControlConfig:
    ramps: RampsConfig
    cycling: CyclingConfig
    edge_detection: EdgeDetectionConfig
    debounce: DebounceConfig


def _require_known_keys(section_name: str, payload: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(payload.keys()) - allowed)
    if unknown:
        raise ControlConfigError(f"Unknown keys in control.{section_name}: {', '.join(unknown)}")


def parse_control_config(config: dict[str, Any]) -> ControlConfig:
    control = config.get('control', {})
    if not isinstance(control, dict):
        raise ControlConfigError('control section must be a mapping')

    _require_known_keys('control', control, {'ramps', 'cycling', 'edge_detection', 'debounce'})

    ramps = control.get('ramps', {})
    cycling = control.get('cycling', {})
    edge = control.get('edge_detection', {})
    debounce = control.get('debounce', {})

    if not isinstance(ramps, dict) or not isinstance(cycling, dict) or not isinstance(edge, dict) or not isinstance(debounce, dict):
        raise ControlConfigError('control subsections must be mappings')

    _require_known_keys(
        'ramps',
        ramps,
        {
            'precision_sweep_rate_torr_per_sec',
            'precision_edge_rate_torr_per_sec',
            'low_pressure_precision_threshold_psi',
            'low_pressure_precision_sweep_rate_torr_per_sec',
            'fast_cycle_rate_psi_per_sec',
            'pre_approach_rate_multiplier',
        },
    )
    _require_known_keys('cycling', cycling, {'num_cycles'})
    _require_known_keys(
        'edge_detection',
        edge,
        {
            'overshoot_beyond_limit_percent',
            'timeout_sec',
            'atmosphere_tolerance_psi',
            'precision_approach_tolerance_torr',
            'precision_approach_settle_sec',
            'precision_start_atmosphere_hold_sec',
            'precision_close_limit_offset_torr',
            'precision_prepass_nudge_torr',
            'precision_deactivation_margin_torr',
            'precision_post_target_grace_sec',
            'precision_return_overshoot_torr',
        },
    )
    _require_known_keys('debounce', debounce, {'stable_sample_count', 'min_edge_interval_ms'})

    sweep_rate = float(ramps.get('precision_sweep_rate_torr_per_sec', 5.0))

    return ControlConfig(
        ramps=RampsConfig(
            precision_sweep_rate_torr_per_sec=sweep_rate,
            precision_edge_rate_torr_per_sec=float(ramps.get('precision_edge_rate_torr_per_sec', sweep_rate)),
            low_pressure_precision_threshold_psi=max(
                0.0,
                float(ramps.get('low_pressure_precision_threshold_psi', 0.0)),
            ),
            low_pressure_precision_sweep_rate_torr_per_sec=max(
                0.1,
                float(ramps.get('low_pressure_precision_sweep_rate_torr_per_sec', sweep_rate)),
            ),
            fast_cycle_rate_psi_per_sec=max(
                0.1,
                float(ramps.get('fast_cycle_rate_psi_per_sec', 100.0)),
            ),
            pre_approach_rate_multiplier=max(
                1.0,
                float(ramps.get('pre_approach_rate_multiplier', 3.0)),
            ),
        ),
        cycling=CyclingConfig(num_cycles=int(cycling.get('num_cycles', 3))),
        edge_detection=EdgeDetectionConfig(
            overshoot_beyond_limit_percent=float(edge.get('overshoot_beyond_limit_percent', 10.0)),
            timeout_sec=float(edge.get('timeout_sec', 60.0)),
            atmosphere_tolerance_psi=float(edge.get('atmosphere_tolerance_psi', 0.25)),
            precision_approach_tolerance_torr=float(edge.get('precision_approach_tolerance_torr', 8.0)),
            precision_approach_settle_sec=float(edge.get('precision_approach_settle_sec', 0.2)),
            precision_start_atmosphere_hold_sec=float(edge.get('precision_start_atmosphere_hold_sec', 1.0)),
            precision_close_limit_offset_torr=float(edge.get('precision_close_limit_offset_torr', 25.0)),
            precision_prepass_nudge_torr=float(edge.get('precision_prepass_nudge_torr', 20.0)),
            precision_deactivation_margin_torr=float(edge.get('precision_deactivation_margin_torr', 15.0)),
            precision_post_target_grace_sec=float(edge.get('precision_post_target_grace_sec', 0.35)),
            precision_return_overshoot_torr=float(edge.get('precision_return_overshoot_torr', 15.0)),
        ),
        debounce=DebounceConfig(
            stable_sample_count=int(debounce.get('stable_sample_count', 3)),
            min_edge_interval_ms=float(debounce.get('min_edge_interval_ms', 50)),
        ),
    )
