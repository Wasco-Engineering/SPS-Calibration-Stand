from quality_cal.config import _merge_quality_hardware, build_pressure_points


def test_build_pressure_points_uses_default_schedule():
    points = build_pressure_points({})

    assert points[0] == 0.05
    assert 30.0 in points
    assert points[-1] == 30.0
    assert len(points) == 31


def test_build_pressure_points_uses_explicit_list():
    points = build_pressure_points({"pressure_points_psia": [10, 5, 5, 20]})

    assert points == [5.0, 10.0, 20.0]


def test_quality_cal_hardware_uses_stinger_wiring_and_mensor_overlay():
    merged = _merge_quality_hardware(
        {
            'labjack': {'port_a': {'transducer_installed': False, 'solenoid_dio': 19}},
            'alicat': {'port_a': {'com_port': 'COM3'}},
        },
        {
            'labjack': {'port_a': {'transducer_installed': True, 'solenoid_dio': 2}},
            'mensor': {'port': 'COM5'},
        },
        {'hardware_overrides': {'labjack': {'pressure_filter_alpha': 0.35}}},
    )

    assert merged['labjack']['port_a'] == {
        'transducer_installed': False,
        'solenoid_dio': 19,
    }
    assert merged['alicat']['port_a']['com_port'] == 'COM3'
    assert merged['mensor']['port'] == 'COM5'
    assert merged['labjack']['pressure_filter_alpha'] == 0.35
