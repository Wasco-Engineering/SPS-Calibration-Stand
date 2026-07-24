"""Focused pressure conversion and UI display behavior tests."""

from __future__ import annotations

import pytest

from app.hardware.alicat import AlicatReading
from app.services.pressure_domain import (
    infer_barometric_pressure_from_alicat,
    infer_setpoint_reference,
    is_plausible_barometric_psi,
    resolve_alicat_setpoint_reference_for_test,
    resolve_barometric_psi,
    to_alicat_setpoint_psi,
)
from app.services.ptp_service import build_pressure_visualization, convert_pressure, derive_test_setup
from app.services.ui_bridge import UIBridge
from app.services.work_order_controller import WorkOrderController
from tests.fixtures.pressure_data import build_port_reading


class _DisplayUnitBridge:
    def __init__(self) -> None:
        self.unit = 'PSIA'
        self.reference = None
        self.viz_updates: list[tuple[str, dict]] = []

    def set_pressure_unit(self, unit: str) -> None:
        self.unit = unit

    def get_pressure_unit(self) -> str:
        return self.unit

    def set_display_reference(self, reference: str | None) -> None:
        self.reference = reference

    def update_pressure_viz(self, port_id: str, viz: dict) -> None:
        self.viz_updates.append((port_id, viz))


def test_convert_pressure_round_trip_torr() -> None:
    torr_value = convert_pressure(14.7, 'PSI', 'Torr')
    assert torr_value == pytest.approx(760.0, rel=1e-3)
    assert convert_pressure(torr_value, 'Torr', 'PSI') == pytest.approx(14.7, rel=1e-3)


def test_debug_setpoint_recomputes_when_switching_psig_psia() -> None:
    bridge = UIBridge({})
    events: list[tuple[str, float, float | None, float | None, float | None]] = []
    bridge.debug_chart_updated.connect(
        lambda port_id, ts, pressure, setpoint, alicat: events.append((port_id, ts, pressure, setpoint, alicat))
    )
    bridge.set_pressure_unit('PSIG')
    bridge.update_pressure(
        'port_a',
        build_port_reading(
            timestamp=1.0,
            transducer_pressure=9.7,
            transducer_reference='absolute',
            alicat_pressure=9.7,
            alicat_setpoint=9.7,
            barometric_pressure=14.7,
            gauge_pressure=-5.0,
        ),
    )
    assert events[-1][3] == pytest.approx(-5.0, rel=1e-3)
    bridge.set_pressure_unit('PSIA')
    assert events[-1][3] == pytest.approx(9.7, rel=1e-3)


def test_set_pressure_accepts_gauge_input_for_absolute_display() -> None:
    bridge = UIBridge({})
    events: list[tuple[str, float, str]] = []
    bridge.pressure_updated.connect(lambda port_id, pressure, unit: events.append((port_id, pressure, unit)))
    bridge.set_pressure_unit('PSIA')
    bridge.set_pressure('port_a', -5.0, 'PSIG')
    assert events[-1][1] == pytest.approx(9.7, rel=1e-3)
    assert events[-1][2] == 'PSIA'


def test_debug_setpoint_uses_inferred_barometric_when_direct_value_missing() -> None:
    bridge = UIBridge({})
    events: list[tuple[str, float, float | None, float | None, float | None]] = []
    bridge.debug_chart_updated.connect(
        lambda port_id, ts, pressure, setpoint, alicat: events.append((port_id, ts, pressure, setpoint, alicat))
    )
    reading = build_port_reading(
        timestamp=1.0,
        transducer_pressure=1.0,
        transducer_reference='absolute',
        alicat_pressure=1.0,
        alicat_setpoint=-12.6,
        barometric_pressure=0.0,
        gauge_pressure=-12.6,
    )
    assert reading.alicat is not None
    reading.alicat.barometric_pressure = None
    bridge.set_pressure_unit('PSIA')
    bridge.update_pressure('port_a', reading)
    assert events[-1][3] == pytest.approx(1.0, rel=1e-3)


def test_derive_setup_defaults_missing_pressure_reference_to_absolute_for_torr() -> None:
    setup = derive_test_setup(
        '17025',
        '399',
        {
            'ActivationTarget': '400.000000',
            'IncreasingLowerLimit': '390.000000',
            'IncreasingUpperLimit': '410.000000',
            'DecreasingLowerLimit': '380.000000',
            'DecreasingUpperLimit': '395.000000',
            'ResetBandLowerLimit': '360.000000',
            'ResetBandUpperLimit': '370.000000',
            'TargetActivationDirection': 'Decreasing',
            'UnitsOfMeasure': '21',
            'CommonTerminal': '3',
            'NormallyOpenTerminal': '2',
            'NormallyClosedTerminal': '1',
        },
    )
    assert setup.units_label == 'Torr'
    assert setup.pressure_reference == 'absolute'


def test_derive_setup_normalizes_pressure_reference_alias() -> None:
    setup = derive_test_setup(
        '17025',
        '399',
        {
            'ActivationTarget': '400.000000',
            'IncreasingLowerLimit': '390.000000',
            'IncreasingUpperLimit': '410.000000',
            'DecreasingLowerLimit': '380.000000',
            'DecreasingUpperLimit': '395.000000',
            'ResetBandLowerLimit': '360.000000',
            'ResetBandUpperLimit': '370.000000',
            'TargetActivationDirection': 'Decreasing',
            'UnitsOfMeasure': '21',
            'PressureReference': 'Gage',
            'CommonTerminal': '3',
            'NormallyOpenTerminal': '2',
            'NormallyClosedTerminal': '1',
        },
    )
    assert setup.pressure_reference == 'gauge'


@pytest.mark.parametrize(
    ('direction', 'activation_label', 'deactivation_label'),
    [
        ('Increasing', 'ACT/INC', 'DEACT/DEC'),
        ('Decreasing', 'ACT/DEC', 'DEACT/INC'),
    ],
)
def test_pressure_visualization_labels_follow_activation_direction(
    direction: str,
    activation_label: str,
    deactivation_label: str,
) -> None:
    setup = derive_test_setup(
        '17025',
        '399',
        {
            'ActivationTarget': '400.000000',
            'IncreasingLowerLimit': '390.000000',
            'IncreasingUpperLimit': '410.000000',
            'DecreasingLowerLimit': '380.000000',
            'DecreasingUpperLimit': '395.000000',
            'ResetBandLowerLimit': '360.000000',
            'ResetBandUpperLimit': '370.000000',
            'TargetActivationDirection': direction,
            'UnitsOfMeasure': '21',
            'PressureReference': 'Absolute',
            'CommonTerminal': '3',
            'NormallyOpenTerminal': '2',
            'NormallyClosedTerminal': '1',
        },
    )

    viz = build_pressure_visualization(setup, {})

    assert viz['activation_label'] == activation_label
    assert viz['deactivation_label'] == deactivation_label


def test_torr_pressure_visualization_keeps_atmosphere_on_torr_scale() -> None:
    setup = derive_test_setup(
        '17021',
        '399',
        {
            'ActivationTarget': '75.000000',
            'IncreasingLowerLimit': '-Inf',
            'IncreasingUpperLimit': '145.000000',
            'DecreasingLowerLimit': '70.000000',
            'DecreasingUpperLimit': '80.000000',
            'ResetBandLowerLimit': '-Inf',
            'ResetBandUpperLimit': 'Inf',
            'TargetActivationDirection': 'Decreasing',
            'UnitsOfMeasure': '21',
            'PressureReference': 'Absolute',
            'CommonTerminal': '4',
            'NormallyOpenTerminal': '3',
            'NormallyClosedTerminal': '1',
        },
    )

    viz = build_pressure_visualization(setup, {})

    assert viz['atmosphere_psi'] == pytest.approx(760.0, rel=1e-3)
    assert viz['max_psi'] < 1000.0


def test_pressure_visualization_keeps_psig_atmosphere_on_gauge_scale() -> None:
    setup = derive_test_setup(
        '17123',
        '399',
        {
            'ActivationTarget': '10.000000',
            'IncreasingLowerLimit': '9.500000',
            'IncreasingUpperLimit': '10.500000',
            'DecreasingLowerLimit': '7.000000',
            'DecreasingUpperLimit': '9.500000',
            'ResetBandLowerLimit': '-Inf',
            'ResetBandUpperLimit': 'Inf',
            'TargetActivationDirection': 'Increasing',
            'UnitsOfMeasure': '1',
            'PressureReference': 'Gauge',
            'CommonTerminal': '1',
            'NormallyOpenTerminal': '2',
            'NormallyClosedTerminal': '0',
        },
    )

    viz = build_pressure_visualization(
        setup,
        {},
        atmosphere_override=14.7,
        display_units_override='PSIG',
    )

    assert viz['atmosphere_psi'] == pytest.approx(0.0, abs=1e-6)
    assert viz['activation_band'] == pytest.approx((9.5, 10.5))
    assert viz['deactivation_band'] == pytest.approx((7.0, 9.5))


def test_barometric_plausibility_guard() -> None:
    assert is_plausible_barometric_psi(14.7)
    assert not is_plausible_barometric_psi(0.2635)


def test_infer_barometric_ignores_exh_status_packet() -> None:
    """EXH is not local atmosphere on Stinger — do not use line P as baro."""
    reading = AlicatReading(
        pressure=13.51,
        setpoint=0.0,
        timestamp=0.0,
        raw_response='B +013.51 +000.00 EXH',
    )
    assert infer_barometric_pressure_from_alicat(reading) is None


def test_infer_barometric_ignores_exh_with_stale_setpoint() -> None:
    reading = AlicatReading(
        pressure=13.51,
        setpoint=8.0,
        timestamp=0.0,
        raw_response='B +013.51 +008.00 EXH',
    )
    assert infer_barometric_pressure_from_alicat(reading) is None


def test_resolve_barometric_holds_last_value_during_short_exh_status() -> None:
    reading = build_port_reading(
        alicat_pressure=13.83,
        alicat_setpoint=9.4,
    )
    assert reading.alicat is not None
    reading.alicat.barometric_pressure = None
    reading.alicat.raw_response = 'A +0715.2 +0486.2 EXH'

    assert resolve_barometric_psi(reading, last_value=14.61) == pytest.approx(14.61)


def test_infer_barometric_ignores_transient_low_exh_pressure() -> None:
    reading = AlicatReading(
        pressure=11.73,
        setpoint=13.0,
        timestamp=0.0,
        raw_response='B +011.73 +013.00 EXH',
    )
    assert infer_barometric_pressure_from_alicat(reading) is None


def test_infer_barometric_from_idle_hold_near_anchor() -> None:
    reading = AlicatReading(
        pressure=13.50,
        setpoint=13.48,
        timestamp=0.0,
        raw_response='A +013.50 +013.48 HLD',
    )
    assert infer_barometric_pressure_from_alicat(
        reading, anchor_baro_psi=13.48
    ) == pytest.approx(13.50, rel=1e-6)


def test_infer_barometric_ignores_pressurized_hold() -> None:
    reading = AlicatReading(
        pressure=16.20,
        setpoint=16.20,
        timestamp=0.0,
        raw_response='A +016.20 +016.20 HLD',
    )
    assert infer_barometric_pressure_from_alicat(reading, anchor_baro_psi=13.48) is None
    assert infer_barometric_pressure_from_alicat(reading) is None


def test_infer_barometric_not_from_vacuum_setpoint() -> None:
    """Deep vacuum with zero setpoint must not be mistaken for atmosphere."""
    reading = AlicatReading(
        pressure=8.5,
        setpoint=0.0,
        timestamp=0.0,
        raw_response='B +008.50 +000.00',
    )
    assert infer_barometric_pressure_from_alicat(reading) is None


def test_to_alicat_setpoint_psi_gauge_reference() -> None:
    assert to_alicat_setpoint_psi(7.8, barometric_psi=14.7, setpoint_reference='gauge') == pytest.approx(
        7.8, rel=1e-6
    )
    assert to_alicat_setpoint_psi(14.7, barometric_psi=14.7, setpoint_reference='gauge') == pytest.approx(
        0.0, rel=1e-6
    )


def test_to_alicat_setpoint_psi_absolute_reference() -> None:
    assert to_alicat_setpoint_psi(7.8, barometric_psi=14.7, setpoint_reference='absolute') == pytest.approx(
        7.8, rel=1e-6
    )


def test_resolve_alicat_setpoint_reference_for_test_defaults_absolute_for_ptp_gauge() -> None:
    reading = build_port_reading(
        alicat_pressure=13.5,
        alicat_setpoint=14.7,
        barometric_pressure=14.7,
    )
    assert (
        resolve_alicat_setpoint_reference_for_test(
            ptp_pressure_reference='gauge',
            reading=reading,
            barometric_psi=14.7,
        )
        == 'absolute'
    )


def test_resolve_alicat_setpoint_reference_for_test_honors_configured_gauge() -> None:
    assert (
        resolve_alicat_setpoint_reference_for_test(
            ptp_pressure_reference='gauge',
            config_reference='gauge',
            barometric_psi=14.7,
        )
        == 'gauge'
    )


def test_resolve_alicat_setpoint_reference_for_non_psi_units_uses_absolute_commands() -> None:
    reading = build_port_reading(
        alicat_pressure=14.7,
        alicat_setpoint=14.7,
        barometric_pressure=14.7,
    )
    assert (
        resolve_alicat_setpoint_reference_for_test(
            ptp_pressure_reference='gauge',
            ptp_units_label='mmHg @ 0 C',
            reading=reading,
            barometric_psi=14.7,
        )
        == 'absolute'
    )


def test_work_order_display_keeps_psia_scale_limits_absolute() -> None:
    setup = derive_test_setup(
        '17029',
        '399',
        {
            'ActivationTarget': '8.300000',
            'IncreasingLowerLimit': '-Inf',
            'IncreasingUpperLimit': '11.000000',
            'DecreasingLowerLimit': '7.800000',
            'DecreasingUpperLimit': '8.800000',
            'ResetBandLowerLimit': '-Inf',
            'ResetBandUpperLimit': 'Inf',
            'TargetActivationDirection': 'Decreasing',
            'UnitsOfMeasure': '1',
            'PressureReference': 'Gauge',
            'CommonTerminal': '4',
            'NormallyOpenTerminal': '1',
            'NormallyClosedTerminal': '3',
        },
    )
    controller = WorkOrderController.__new__(WorkOrderController)
    controller._current_test_setup = setup
    controller._get_barometric_pressure = lambda _port_id: 14.7

    assert controller._to_display_pressure('port_b', 8.3, 'PSIA', 'gauge') == pytest.approx(8.3)


def test_apply_ptp_to_ui_keeps_torr_display_units_for_torr_ptp() -> None:
    setup = derive_test_setup(
        '17021',
        '399',
        {
            'ActivationTarget': '75.000000',
            'IncreasingLowerLimit': '-Inf',
            'IncreasingUpperLimit': '145.000000',
            'DecreasingLowerLimit': '70.000000',
            'DecreasingUpperLimit': '80.000000',
            'ResetBandLowerLimit': '-Inf',
            'ResetBandUpperLimit': 'Inf',
            'TargetActivationDirection': 'Decreasing',
            'UnitsOfMeasure': '21',
            'PressureReference': 'Absolute',
        },
    )
    bridge = _DisplayUnitBridge()
    controller = WorkOrderController.__new__(WorkOrderController)
    controller._current_test_setup = setup
    controller._ui_bridge = bridge
    controller._config = {'ui': {}}
    controller._precision_zoom_active = {'port_a': False, 'port_b': False}
    controller._cycle_estimates_abs_psi = {}

    controller._apply_ptp_to_ui(atmosphere_override=14.7)

    assert bridge.unit == 'Torr'
    assert bridge.reference == 'absolute'
    assert bridge.viz_updates
    assert bridge.viz_updates[-1][1]['atmosphere_psi'] == pytest.approx(760.0, rel=1e-3)


def test_apply_ptp_to_ui_treats_mmhg_at_0c_as_positive_gauge_display() -> None:
    setup = derive_test_setup(
        '17025',
        '700',
        {
            'ActivationTarget': '400.000000',
            'IncreasingLowerLimit': '-Inf',
            'IncreasingUpperLimit': '500.000000',
            'DecreasingLowerLimit': '380.000000',
            'DecreasingUpperLimit': '420.000000',
            'ResetBandLowerLimit': '-Inf',
            'ResetBandUpperLimit': 'Inf',
            'TargetActivationDirection': 'Decreasing',
            'UnitsOfMeasure': '19',
            'PressureReference': 'Gauge',
            'CommonTerminal': '4',
            'NormallyOpenTerminal': '3',
            'NormallyClosedTerminal': '1',
        },
    )
    bridge = _DisplayUnitBridge()
    controller = WorkOrderController.__new__(WorkOrderController)
    controller._current_test_setup = setup
    controller._ui_bridge = bridge
    controller._config = {'ui': {}}
    controller._precision_zoom_active = {'port_a': False, 'port_b': False}
    controller._cycle_estimates_abs_psi = {}

    controller._apply_ptp_to_ui(atmosphere_override=14.7)

    assert bridge.unit == 'mmHg @ 0 C'
    assert bridge.reference == 'gauge'
    assert bridge.viz_updates[-1][1]['atmosphere_psi'] == pytest.approx(0.0, abs=1e-6)


def test_mmhg_gauge_pressure_converts_above_atmosphere() -> None:
    setup = derive_test_setup(
        '17025',
        '700',
        {
            'ActivationTarget': '400.000000',
            'IncreasingLowerLimit': '-Inf',
            'IncreasingUpperLimit': '500.000000',
            'DecreasingLowerLimit': '380.000000',
            'DecreasingUpperLimit': '420.000000',
            'ResetBandLowerLimit': '-Inf',
            'ResetBandUpperLimit': 'Inf',
            'TargetActivationDirection': 'Decreasing',
            'UnitsOfMeasure': '19',
            'PressureReference': 'Gauge',
            'CommonTerminal': '4',
            'NormallyOpenTerminal': '3',
            'NormallyClosedTerminal': '1',
        },
    )
    controller = WorkOrderController.__new__(WorkOrderController)
    controller._current_test_setup = setup
    controller._get_barometric_pressure = lambda _port_id: 14.7
    target_psi = convert_pressure(380.0, 'mmHg @ 0 C', 'PSI')

    assert controller._to_absolute_pressure('port_b', target_psi, 'gauge') == pytest.approx(
        14.7 + target_psi
    )
    assert controller._determine_atmosphere_psi('port_b', 'gauge') == pytest.approx(0.0)


def test_pressurize_reach_check_accepts_mmhg_gauge_target_tolerance() -> None:
    target_gauge = convert_pressure(414.0, 'mmHg @ 0 C', 'PSI')
    observed_gauge = convert_pressure(419.85, 'mmHg @ 0 C', 'PSI')
    tolerance = max(0.15, abs(target_gauge) * 0.03, convert_pressure(10.0, 'Torr', 'PSI'))

    assert WorkOrderController._pressurize_target_reached(
        observed_gauge,
        target_gauge,
        direction=1,
        tolerance_psi=tolerance,
    )


def test_qal15_increasing_mmhg_pressurize_targets_above_far_band() -> None:
    setup = derive_test_setup(
        'SPS01640-02',
        '300',
        {
            'ActivationTarget': '587.000000',
            'IncreasingLowerLimit': '580.000000',
            'IncreasingUpperLimit': '594.000000',
            'DecreasingLowerLimit': '380.000000',
            'DecreasingUpperLimit': '420.000000',
            'ResetBandLowerLimit': '-Inf',
            'ResetBandUpperLimit': 'Inf',
            'TargetActivationDirection': 'Increasing',
            'UnitsOfMeasure': '19',
            'PressureReference': 'Gauge',
        },
    )
    controller = WorkOrderController.__new__(WorkOrderController)
    controller._config = {'control': {'manual_pressurize_overshoot_torr': 120.0}}
    bounds = (
        convert_pressure(380.0, 'mmHg @ 0 C', 'PSI'),
        convert_pressure(594.0, 'mmHg @ 0 C', 'PSI'),
    )

    target = controller._resolve_qal15_pressurize_target_psi(setup, bounds, 14.61)

    target_mmhg = convert_pressure(target, 'PSI', 'mmHg @ 0 C')
    assert target_mmhg == pytest.approx(714.0, rel=0.02)


def test_qal15_decreasing_mmhg_pressurize_targets_above_far_band() -> None:
    setup = derive_test_setup(
        'SPS01439-02',
        '300',
        {
            'ActivationTarget': '400.000000',
            'IncreasingLowerLimit': '562.900000',
            'IncreasingUpperLimit': '585.540000',
            'DecreasingLowerLimit': '395.000000',
            'DecreasingUpperLimit': '405.000000',
            'ResetBandLowerLimit': '-Inf',
            'ResetBandUpperLimit': 'Inf',
            'TargetActivationDirection': 'Decreasing',
            'UnitsOfMeasure': '19',
            'PressureReference': 'Gauge',
        },
    )
    controller = WorkOrderController.__new__(WorkOrderController)
    controller._config = {'control': {'manual_pressurize_overshoot_torr': 120.0}}
    bounds = (
        convert_pressure(395.0, 'mmHg @ 0 C', 'PSI'),
        convert_pressure(585.54, 'mmHg @ 0 C', 'PSI'),
    )

    target = controller._resolve_qal15_pressurize_target_psi(setup, bounds, 14.61)

    target_mmhg = convert_pressure(target, 'PSI', 'mmHg @ 0 C')
    assert target_mmhg == pytest.approx(705.54, rel=0.02)


def test_infer_setpoint_reference_sub_atmospheric_psia_while_at_atmosphere() -> None:
    assert (
        infer_setpoint_reference(
            setpoint=7.8,
            absolute_pressure=14.5,
            gauge_pressure=-0.2,
            barometric_psi=14.7,
        )
        == 'absolute'
    )
    assert (
        infer_setpoint_reference(
            setpoint=-6.9,
            absolute_pressure=14.5,
            gauge_pressure=-0.2,
            barometric_psi=14.7,
        )
        == 'gauge'
    )
