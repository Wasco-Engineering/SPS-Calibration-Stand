from __future__ import annotations

from typing import Any

from scripts.certify_ptp_applications import (
    STATUS_BLOCKED_PTP,
    STATUS_BLOCKED_SWITCH,
    STATUS_PASS,
    ApplicationInput,
    certify_application,
)


def _config() -> dict[str, Any]:
    return {
        'hardware': {
            'labjack': {
                'port_a': {
                    'switch_sensed_db9_pins': [3],
                    'transducer_pressure_min': 0.0,
                    'transducer_pressure_max': 30.0,
                },
                'port_b': {
                    'switch_sensed_db9_pins': [3],
                    'transducer_pressure_min': 0.0,
                    'transducer_pressure_max': 30.0,
                },
            }
        },
        'control': {'edge_detection': {'overshoot_beyond_limit_percent': 10.0}},
        'ui': {'pressure_bar': {}},
    }


def _app(params: dict[str, str]) -> ApplicationInput:
    return ApplicationInput('SPS-TEST', '300', params, 'fixture')


def _base_ptp(**overrides: str) -> dict[str, str]:
    params = {
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
        'CommonTerminal': '4',
        'NormallyOpenTerminal': '3',
        'NormallyClosedTerminal': '1',
    }
    params.update(overrides)
    return params


def test_certification_passes_runnable_psig_application() -> None:
    row = certify_application(_app(_base_ptp()), _config())

    assert row['status'] == STATUS_PASS
    assert row['display_units'] == 'PSIG'
    assert row['sweep_mode'] == 'pressure'
    assert row['port_a_derivation_mode'] == 'derive_nc_from_no'


def test_certification_supports_same_no_nc_terminal_single_throw_shape() -> None:
    row = certify_application(
        _app(
            _base_ptp(
                NormallyOpenTerminal='3',
                NormallyClosedTerminal='3',
            )
        ),
        _config(),
    )

    assert row['status'] == STATUS_PASS
    assert row['port_a_derivation_mode'] == 'derive_nc_from_no'


def test_certification_blocks_incomplete_ptp() -> None:
    params = _base_ptp()
    del params['ActivationTarget']

    row = certify_application(_app(params), _config())

    assert row['status'] == STATUS_BLOCKED_PTP
    assert row['category'] == 'incomplete_ptp'


def test_certification_blocks_unobservable_switch_terminals() -> None:
    row = certify_application(
        _app(
            _base_ptp(
                CommonTerminal='6',
                NormallyOpenTerminal='1',
                NormallyClosedTerminal='8',
            )
        ),
        _config(),
    )

    assert row['status'] == STATUS_BLOCKED_SWITCH
    assert row['category'] == 'switch_resolution'
    assert 'not observable' in row['message']


def test_certification_blocks_zero_common_terminal_as_invalid_ptp() -> None:
    row = certify_application(
        _app(
            _base_ptp(
                CommonTerminal='0',
                NormallyOpenTerminal='0',
                NormallyClosedTerminal='0',
            )
        ),
        _config(),
    )

    assert row['status'] == STATUS_BLOCKED_PTP
    assert row['category'] == 'invalid_ptp'
    assert 'CommonTerminal must be a DB9 pin' in row['message']
