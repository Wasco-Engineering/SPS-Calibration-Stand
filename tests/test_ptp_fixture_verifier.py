from __future__ import annotations

from typing import Any

from app.services.ptp_fixture_verifier import (
    BLOCKED_SWITCH,
    MISSING_PTP,
    PASS_DERIVED,
    discover_top_level_applications,
    has_blocked_verdict,
    verify_application_fixture,
)


def _config() -> dict[str, Any]:
    return {
        'hardware': {
            'labjack': {
                'port_a': {'switch_sensed_db9_pins': [3]},
                'port_b': {'switch_sensed_db9_pins': [3]},
            }
        }
    }


def _ptp(**overrides: str) -> dict[str, str]:
    params = {
        'ActivationTarget': '10',
        'IncreasingLowerLimit': '9',
        'IncreasingUpperLimit': '11',
        'DecreasingLowerLimit': '7',
        'DecreasingUpperLimit': '9',
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


def test_fixture_verifier_reports_derived_mapping_for_both_ports() -> None:
    rows = verify_application_fixture(
        part_id='SPS-TEST',
        sequence_id='300',
        ptp_params=_ptp(),
        config=_config(),
    )

    assert [row['status'] for row in rows] == [PASS_DERIVED, PASS_DERIVED]
    assert [row['derivation_mode'] for row in rows] == [
        'derive_nc_from_no',
        'derive_nc_from_no',
    ]
    assert not has_blocked_verdict(rows)


def test_fixture_verifier_blocks_unobservable_terminals() -> None:
    rows = verify_application_fixture(
        part_id='SPS-TEST',
        sequence_id='300',
        ptp_params=_ptp(
            CommonTerminal='6',
            NormallyOpenTerminal='1',
            NormallyClosedTerminal='8',
        ),
        config=_config(),
    )

    assert [row['status'] for row in rows] == [BLOCKED_SWITCH, BLOCKED_SWITCH]
    assert has_blocked_verdict(rows)


def test_fixture_verifier_marks_missing_live_ptp() -> None:
    rows = verify_application_fixture(
        part_id='17030',
        sequence_id='399',
        ptp_params={},
        config=_config(),
    )

    assert [row['status'] for row in rows] == [MISSING_PTP, MISSING_PTP]
    assert has_blocked_verdict(rows)


def test_top_level_discovery_filters_non_top_level_rows(monkeypatch) -> None:
    monkeypatch.setattr(
        'app.services.ptp_fixture_verifier._discover_applications',
        lambda **_: [('17025', '399'), ('1702', '399'), ('SPS01496-02', '399')],
    )

    assert discover_top_level_applications() == [('17025', '399')]
