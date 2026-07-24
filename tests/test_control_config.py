"""Unit tests for parsing typed control config."""

from __future__ import annotations

import pytest

from app.services.control_config import ControlConfigError, parse_control_config


def test_parse_control_config_defaults() -> None:
    parsed = parse_control_config({})
    assert parsed.ramps.precision_sweep_rate_torr_per_sec == 5.0
    assert parsed.ramps.precision_edge_rate_torr_per_sec == 5.0
    assert parsed.ramps.low_pressure_precision_threshold_psi == 0.0
    assert parsed.ramps.low_pressure_precision_sweep_rate_torr_per_sec == 5.0
    assert parsed.ramps.fast_cycle_rate_psi_per_sec == 100.0
    assert parsed.ramps.pre_approach_rate_multiplier == 3.0
    assert parsed.cycling.num_cycles == 3
    assert parsed.edge_detection.precision_post_target_grace_sec == 0.35
    assert parsed.edge_detection.precision_return_overshoot_torr == 30.0
    assert parsed.edge_detection.precision_approach_settle_sec == 0.75
    assert parsed.debounce.stable_sample_count == 3


def test_parse_control_config_rejects_unknown_keys() -> None:
    with pytest.raises(ControlConfigError, match='Unknown keys in control.control'):
        parse_control_config({'control': {'unexpected': 1}})


def test_parse_control_config_rejects_non_mapping_sections() -> None:
    with pytest.raises(ControlConfigError, match='control section must be a mapping'):
        parse_control_config({'control': 'bad'})

    with pytest.raises(ControlConfigError, match='control subsections must be mappings'):
        parse_control_config({'control': {'ramps': [], 'cycling': {}, 'edge_detection': {}, 'debounce': {}}})


def test_parse_control_config_accepts_fast_cycle_rate_knobs() -> None:
    parsed = parse_control_config(
        {
            'control': {
                'ramps': {
                    'fast_cycle_rate_psi_per_sec': 25.0,
                    'pre_approach_rate_multiplier': 1.0,
                    'low_pressure_precision_threshold_psi': 1.0,
                    'low_pressure_precision_sweep_rate_torr_per_sec': 1.5,
                },
                'cycling': {},
                'edge_detection': {},
                'debounce': {},
            }
        }
    )
    assert parsed.ramps.fast_cycle_rate_psi_per_sec == 25.0
    assert parsed.ramps.pre_approach_rate_multiplier == 1.0
    assert parsed.ramps.low_pressure_precision_threshold_psi == 1.0
    assert parsed.ramps.low_pressure_precision_sweep_rate_torr_per_sec == 1.5
