"""Tests for configurable main pressure measurement source behavior."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from app.core.config import load_config, save_config
from app.hardware.labjack import SwitchState
from app.services.measurement_source import (
    MEASUREMENT_SOURCE_ALICAT,
    MEASUREMENT_SOURCE_BLEND,
    MEASUREMENT_SOURCE_TRANSDUCER,
    MeasurementSettings,
    _transducer_pressure_abs_psi,
    get_measurement_settings,
    select_main_pressure_abs_psi,
    select_ui_pressure_abs_psi,
)
from app.services.ptp_service import TestSetup
from app.services.test_executor import TestExecutor as _TestExecutor
from app.services.ui_bridge import UIBridge
from tests.fixtures.pressure_data import build_port_reading


def _auto_settings(**overrides: Any) -> MeasurementSettings:
    base = get_measurement_settings(
        {
            'hardware': {
                'measurement': {
                    'preferred_source': 'auto',
                    'fallback_on_unavailable': True,
                    'transducer_only_below_psi': 10.0,
                    'alicat_only_above_psi': 31.0,
                    'switch_pivot_min_psi': 8.0,
                },
            },
        }
    )
    return MeasurementSettings(
        preferred_source=overrides.get('preferred_source', base.preferred_source),
        fallback_on_unavailable=overrides.get(
            'fallback_on_unavailable',
            base.fallback_on_unavailable,
        ),
        sensor_disagreement_fallback_enabled=overrides.get(
            'sensor_disagreement_fallback_enabled',
            False,
        ),
        transducer_only_below_psi=overrides.get(
            'transducer_only_below_psi',
            base.transducer_only_below_psi,
        ),
        alicat_only_above_psi=overrides.get(
            'alicat_only_above_psi',
            base.alicat_only_above_psi,
        ),
        switch_pivot_min_psi=overrides.get(
            'switch_pivot_min_psi',
            base.switch_pivot_min_psi,
        ),
        sensor_disagreement_max_psi=overrides.get(
            'sensor_disagreement_max_psi',
            base.sensor_disagreement_max_psi,
        ),
    )


def _executor_config(preferred_source: str) -> dict[str, Any]:
    return {
        'hardware': {
            'measurement': {
                'preferred_source': preferred_source,
                'fallback_on_unavailable': True,
                'sensor_disagreement_fallback_enabled': False,
            },
        },
        'control': {
            'cycling': {'num_cycles': 1},
            'ramps': {'precision_sweep_rate_torr_per_sec': 10.0},
            'edge_detection': {'timeout_sec': 1.0},
            'debounce': {},
        },
    }


class _FakeAlicat:
    def configure_units_from_ptp(self, _units_code: str) -> bool:
        return True

    def cancel_hold(self) -> bool:
        return True

    def set_ramp_rate(self, _rate: float) -> bool:
        return True


class _FakePort:
    def __init__(self) -> None:
        self.alicat = _FakeAlicat()

    def set_pressure(self, _setpoint: float) -> bool:
        return True

    def set_solenoid(self, _to_vacuum: bool) -> bool:
        return True

    def vent_to_atmosphere(self) -> bool:
        return True


def _build_executor(preferred_source: str) -> _TestExecutor:
    setup = TestSetup(
        part_id='17025',
        sequence_id='399',
        units_code='1',
        units_label='PSI',
        activation_direction='Increasing',
        activation_target=20.0,
        pressure_reference='absolute',
        terminals={},
        bands={
            'increasing': {'lower': 19.0, 'upper': 21.0},
            'decreasing': {'lower': 18.0, 'upper': 20.0},
            'reset': {'lower': 17.0, 'upper': 22.0},
        },
        raw={},
    )
    return _TestExecutor(
        port_id='port_a',
        port=cast(Any, _FakePort()),
        test_setup=setup,
        config=_executor_config(preferred_source),
        get_latest_reading=lambda _pid: None,
        get_barometric_psi=lambda _pid: 14.7,
    )


def _base_config() -> dict[str, Any]:
    return load_config()


def test_load_config_applies_measurement_defaults_when_missing(tmp_path: Path) -> None:
    cfg = _base_config()
    cfg['hardware'].pop('measurement', None)
    path = tmp_path / 'stinger_config.yaml'
    with path.open('w', encoding='utf-8') as handle:
        yaml.safe_dump(cfg, handle, sort_keys=False)

    loaded = load_config(path)
    measurement_cfg = loaded['hardware']['measurement']
    assert measurement_cfg['preferred_source'] == 'auto'
    assert measurement_cfg['fallback_on_unavailable'] is True
    assert measurement_cfg['transducer_only_below_psi'] == 10.0
    assert measurement_cfg['alicat_only_above_psi'] == 31.0


def test_save_config_persists_normalized_measurement_source(tmp_path: Path) -> None:
    cfg = _base_config()
    cfg.setdefault('hardware', {})['measurement'] = {
        'preferred_source': 'Alicat',
        'fallback_on_unavailable': True,
    }
    source_path = tmp_path / 'in.yaml'
    with source_path.open('w', encoding='utf-8') as handle:
        yaml.safe_dump(cfg, handle, sort_keys=False)
    loaded = load_config(source_path)

    output_path = tmp_path / 'out.yaml'
    save_config(loaded, output_path)
    with output_path.open('r', encoding='utf-8') as handle:
        persisted = cast(dict[str, Any], yaml.safe_load(handle))
    assert persisted['hardware']['measurement']['preferred_source'] == 'alicat'


def test_select_main_pressure_abs_psi_prefers_requested_source_with_fallback() -> None:
    reading = build_port_reading(transducer_pressure=10.0, alicat_pressure=0.0)
    assert reading.alicat is not None
    reading.alicat.pressure = None
    settings = MeasurementSettings(
        preferred_source='alicat',
        fallback_on_unavailable=True,
    )
    selected, source = select_main_pressure_abs_psi(
        reading=reading,
        settings=settings,
        barometric_psi=14.7,
    )
    assert selected == 10.0
    assert source == MEASUREMENT_SOURCE_TRANSDUCER


def test_gauge_labeled_transducer_near_baro_is_treated_as_absolute() -> None:
    reading = build_port_reading(
        transducer_pressure=14.6,
        transducer_reference='gauge',
        alicat_pressure=14.6,
        barometric_pressure=14.7,
    )

    assert _transducer_pressure_abs_psi(reading, 14.7) == pytest.approx(14.6)


def test_auto_mode_uses_transducer_below_cutover() -> None:
    reading = build_port_reading(transducer_pressure=8.0, alicat_pressure=8.5)
    selected, source = select_main_pressure_abs_psi(
        reading=reading,
        settings=_auto_settings(),
        barometric_psi=14.7,
    )
    assert selected == 8.0
    assert source == MEASUREMENT_SOURCE_TRANSDUCER


def test_auto_mode_uses_alicat_above_cutover() -> None:
    reading = build_port_reading(transducer_pressure=31.0, alicat_pressure=31.2)
    selected, source = select_main_pressure_abs_psi(
        reading=reading,
        settings=_auto_settings(),
        barometric_psi=14.7,
    )
    assert selected == pytest.approx(31.2)
    assert source == MEASUREMENT_SOURCE_ALICAT


def test_auto_mode_supports_hard_five_psi_cutover() -> None:
    settings = _auto_settings(
        transducer_only_below_psi=5.0,
        alicat_only_above_psi=5.0,
        switch_pivot_min_psi=5.0,
        sensor_disagreement_fallback_enabled=True,
        sensor_disagreement_max_psi=0.1,
    )

    low_reading = build_port_reading(transducer_pressure=4.9, alicat_pressure=8.0)
    selected, source = select_main_pressure_abs_psi(
        reading=low_reading,
        settings=settings,
        barometric_psi=14.7,
    )
    assert selected == pytest.approx(4.9)
    assert source == MEASUREMENT_SOURCE_TRANSDUCER

    high_reading = build_port_reading(transducer_pressure=5.0, alicat_pressure=8.0)
    selected, source = select_main_pressure_abs_psi(
        reading=high_reading,
        settings=settings,
        barometric_psi=14.7,
    )
    assert selected == pytest.approx(8.0)
    assert source == MEASUREMENT_SOURCE_ALICAT


def test_auto_mode_blends_between_cutover_points() -> None:
    reading = build_port_reading(transducer_pressure=28.5, alicat_pressure=31.5)
    selected, source = select_main_pressure_abs_psi(
        reading=reading,
        settings=_auto_settings(),
        barometric_psi=14.7,
    )
    # t = (28.5 - 10) / (31 - 10) = 18.5/21 -> blend
    assert selected == pytest.approx(28.5 * (2.5 / 21) + 31.5 * (18.5 / 21))
    assert source == MEASUREMENT_SOURCE_BLEND


def test_auto_mode_switch_pivot_snaps_to_alicat() -> None:
    reading = build_port_reading(transducer_pressure=25.0, alicat_pressure=29.5)
    reading.switch = SwitchState(no_active=True, nc_active=False, timestamp=1.0)
    selected, source = select_main_pressure_abs_psi(
        reading=reading,
        settings=_auto_settings(),
        barometric_psi=14.7,
    )
    assert selected == pytest.approx(29.5)
    assert source == MEASUREMENT_SOURCE_ALICAT


def test_ui_bridge_uses_auto_source_for_display() -> None:
    bridge = UIBridge(
        {
            'hardware': {
                'measurement': {
                    'preferred_source': 'auto',
                    'fallback_on_unavailable': True,
                    'transducer_only_below_psi': 10.0,
                    'alicat_only_above_psi': 31.0,
                    'sensor_disagreement_fallback_enabled': False,
                }
            }
        }
    )
    emitted: list[tuple[str, float, str]] = []
    bridge.pressure_updated.connect(
        lambda port_id, pressure, unit: emitted.append((port_id, pressure, unit))
    )
    bridge.set_pressure_unit('PSIA')
    bridge.update_pressure(
        'port_a',
        build_port_reading(transducer_pressure=10.0, alicat_pressure=22.0),
    )

    assert emitted
    assert emitted[-1][1] == 10.0
    assert emitted[-1][2] == 'PSIA'


def test_auto_mode_falls_back_to_alicat_on_sensor_disagreement() -> None:
    """When transducer and Alicat disagree, prefer Alicat (no retry)."""
    reading = build_port_reading(transducer_pressure=10.0, alicat_pressure=10.5)
    selected, source = select_main_pressure_abs_psi(
        reading=reading,
        settings=_auto_settings(
            sensor_disagreement_fallback_enabled=True,
            sensor_disagreement_max_psi=0.1,
        ),
        barometric_psi=14.7,
    )
    assert selected == pytest.approx(10.5)
    assert source == MEASUREMENT_SOURCE_ALICAT

    agreeing = build_port_reading(transducer_pressure=9.5, alicat_pressure=9.52)
    selected2, source2 = select_main_pressure_abs_psi(
        reading=agreeing,
        settings=_auto_settings(
            sensor_disagreement_fallback_enabled=True,
            sensor_disagreement_max_psi=0.1,
        ),
        barometric_psi=14.7,
    )
    assert selected2 == 9.5
    assert source2 == MEASUREMENT_SOURCE_TRANSDUCER


def test_auto_mode_disagreement_overrides_blend() -> None:
    reading = build_port_reading(transducer_pressure=15.0, alicat_pressure=18.0)
    selected, source = select_main_pressure_abs_psi(
        reading=reading,
        settings=_auto_settings(
            transducer_only_below_psi=10.0,
            alicat_only_above_psi=31.0,
            sensor_disagreement_fallback_enabled=True,
            sensor_disagreement_max_psi=0.1,
        ),
        barometric_psi=14.7,
    )
    assert selected == pytest.approx(18.0)
    assert source == MEASUREMENT_SOURCE_ALICAT


def test_ui_pressure_prefers_transducer_over_auto_alicat() -> None:
    reading = build_port_reading(transducer_pressure=12.0, alicat_pressure=25.0)
    main_psi, _main_src = select_main_pressure_abs_psi(
        reading=reading,
        settings=_auto_settings(),
        barometric_psi=14.7,
    )
    ui_psi, ui_src = select_ui_pressure_abs_psi(
        reading=reading,
        settings=_auto_settings(),
        barometric_psi=14.7,
    )
    assert ui_psi == pytest.approx(12.0)
    assert ui_src == MEASUREMENT_SOURCE_TRANSDUCER
    assert main_psi != pytest.approx(12.0)


def test_ui_pressure_honors_explicit_alicat_source() -> None:
    reading = build_port_reading(transducer_pressure=0.2, alicat_pressure=13.5)
    ui_psi, ui_src = select_ui_pressure_abs_psi(
        reading=reading,
        settings=_auto_settings(preferred_source='alicat'),
        barometric_psi=13.5,
    )
    assert ui_psi == pytest.approx(13.5)
    assert ui_src == MEASUREMENT_SOURCE_ALICAT


def test_executor_control_pressure_prefers_transducer() -> None:
    executor = _build_executor('auto')
    reading = build_port_reading(transducer_pressure=10.0, alicat_pressure=26.0)
    assert executor._reading_pressure_abs_psi(reading) == 10.0

    high_reading = build_port_reading(transducer_pressure=31.0, alicat_pressure=31.5)
    assert executor._reading_pressure_abs_psi(high_reading) == pytest.approx(31.0)
