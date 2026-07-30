"""Tests for quality-calibration CAL 10 profile scheduling."""

from __future__ import annotations

from pathlib import Path

import pytest

from quality_cal.config import (
    CAL10_WCS02075_PRESSURE_POINTS_PSIA,
    PROFILE_CAL10_WCS02075,
    build_pressure_points_for_profile,
    get_profile_ids,
    load_config,
    parse_quality_settings,
    point_timing_for_target,
)

_REPO_CAL_CONFIG = (
    Path(__file__).resolve().parents[1]
    / 'configs'
    / 'CA-MAN-SPS-01'
    / 'quality_cal_config.yaml'
)


def _repo_config() -> dict:
    return load_config(_REPO_CAL_CONFIG)


def test_only_cal10_profile_in_repo_config() -> None:
    profile_ids = get_profile_ids(_repo_config())
    assert profile_ids == [PROFILE_CAL10_WCS02075]


def test_cal10_wcs02075_profile_matches_work_instruction_order() -> None:
    config = _repo_config()
    points = build_pressure_points_for_profile(PROFILE_CAL10_WCS02075, config['quality'])
    assert points == CAL10_WCS02075_PRESSURE_POINTS_PSIA
    settings = parse_quality_settings(config, profile_id=PROFILE_CAL10_WCS02075)
    assert settings.require_mensor is True
    assert settings.mensor_max_psia == pytest.approx(165.0)
    assert settings.prompt_disconnect_mensor_above_psi is None
    assert len(points) == 18
    assert max(points) == 115.0
    assert min(points) == 0.05


def test_point_timing_tiers() -> None:
    settings = parse_quality_settings(_repo_config(), profile_id=PROFILE_CAL10_WCS02075)
    assert point_timing_for_target(1.0, settings) == (2.0, 5.0)
    assert point_timing_for_target(10.0, settings) == (1.5, 3.0)
    assert point_timing_for_target(50.0, settings) == (1.5, 3.0)
