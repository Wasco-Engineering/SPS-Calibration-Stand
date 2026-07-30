from __future__ import annotations

from app.services.ptp_switch_resolver import resolve_ptp_switch_config


def test_seq300_style_ptp_observes_no_and_derives_nc() -> None:
    result = resolve_ptp_switch_config(
        ptp_params={
            'NormallyOpenTerminal': '3',
            'NormallyClosedTerminal': '1',
            'CommonTerminal': '4',
        },
        port_id='port_a',
        port_config={'switch_sensed_db9_pins': [3]},
    )

    assert result.is_valid
    assert result.common_dio == 3
    assert result.no_dio == 2
    assert result.nc_dio == 2
    assert result.derivation_mode == 'derive_nc_from_no'
    assert result.derive_nc_from_no
    assert not result.derive_no_from_nc


def test_seq600_style_ptp_observes_nc_and_derives_no() -> None:
    result = resolve_ptp_switch_config(
        ptp_params={
            'NormallyOpenTerminal': '1',
            'NormallyClosedTerminal': '3',
            'CommonTerminal': '4',
        },
        port_id='port_b',
        port_config={'switch_sensed_db9_pins': [3]},
    )

    assert result.is_valid
    assert result.common_dio == 12
    assert result.no_dio == 11
    assert result.nc_dio == 11
    assert result.derivation_mode == 'derive_no_from_nc'
    assert result.derive_no_from_nc
    assert not result.derive_nc_from_no


def test_dual_sense_ptp_reads_no_and_nc_directly() -> None:
    result = resolve_ptp_switch_config(
        ptp_params={
            'NormallyOpenTerminal': '3',
            'NormallyClosedTerminal': '1',
            'CommonTerminal': '4',
        },
        port_id='port_a',
        port_config={'switch_sensed_db9_pins': [1, 3]},
    )

    assert result.is_valid
    assert result.no_dio == 2
    assert result.nc_dio == 0
    assert result.derivation_mode == 'direct'
    assert not result.derive_nc_from_no
    assert not result.derive_no_from_nc


def test_not_connected_no_terminal_observes_nc_and_derives_no() -> None:
    result = resolve_ptp_switch_config(
        ptp_params={
            'NormallyOpenTerminal': '0',
            'NormallyClosedTerminal': '3',
            'CommonTerminal': '4',
        },
        port_id='port_a',
        port_config={'switch_sensed_db9_pins': [3]},
    )

    assert result.is_valid
    assert result.normally_open_terminal is None
    assert result.normally_closed_terminal == 3
    assert result.no_dio == 2
    assert result.nc_dio == 2
    assert result.derivation_mode == 'derive_no_from_nc'
    assert result.derive_no_from_nc
    assert any('NormallyOpenTerminal=0' in warning for warning in result.warnings)


def test_not_connected_nc_terminal_observes_no_and_derives_nc() -> None:
    result = resolve_ptp_switch_config(
        ptp_params={
            'NormallyOpenTerminal': '3',
            'NormallyClosedTerminal': '0',
            'CommonTerminal': '4',
        },
        port_id='port_a',
        port_config={'switch_sensed_db9_pins': [3]},
    )

    assert result.is_valid
    assert result.normally_open_terminal == 3
    assert result.normally_closed_terminal is None
    assert result.no_dio == 2
    assert result.nc_dio == 2
    assert result.derivation_mode == 'derive_nc_from_no'
    assert result.derive_nc_from_no
    assert any('NormallyClosedTerminal=0' in warning for warning in result.warnings)


def test_same_no_nc_terminal_is_treated_as_single_throw() -> None:
    result = resolve_ptp_switch_config(
        ptp_params={
            'NormallyOpenTerminal': '3',
            'NormallyClosedTerminal': '3',
            'CommonTerminal': '4',
        },
        port_id='port_a',
        port_config={'switch_sensed_db9_pins': [3]},
    )

    assert result.is_valid
    assert result.normally_open_terminal == 3
    assert result.normally_closed_terminal is None
    assert result.no_dio == 2
    assert result.nc_dio == 2
    assert result.derivation_mode == 'derive_nc_from_no'
    assert result.derive_nc_from_no
    assert any('share DB9 pin 3' in warning for warning in result.warnings)


def test_same_no_nc_terminal_uses_adapter_when_sense_pin_differs() -> None:
    result = resolve_ptp_switch_config(
        ptp_params={
            'NormallyOpenTerminal': '1',
            'NormallyClosedTerminal': '1',
            'CommonTerminal': '4',
        },
        port_id='port_a',
        port_config={
            'switch_sensed_db9_pins': [3],
            'switch_spst_read_source': 'adapter_common',
        },
    )

    assert result.is_valid
    assert result.normally_open_terminal == 1
    assert result.normally_closed_terminal is None
    assert result.drive_dio == 0  # drive NO pin 1
    assert result.no_dio == 2  # read stand sense pin 3
    assert result.nc_dio == 2
    assert result.drive_role == 'normally_open'
    assert result.derivation_mode == 'drive_no_read_adapter_common'
    assert result.derive_nc_from_no
    assert any('share DB9 pin 1' in warning for warning in result.warnings)
    assert any('SPST adapter fallback' in warning for warning in result.warnings)


def test_not_connected_no_terminal_can_read_common_and_drive_nc() -> None:
    result = resolve_ptp_switch_config(
        ptp_params={
            'NormallyOpenTerminal': '0',
            'NormallyClosedTerminal': '6',
            'CommonTerminal': '3',
        },
        port_id='port_a',
        port_config={'switch_sensed_db9_pins': [3]},
    )

    assert result.is_valid
    assert result.common_dio == 2
    assert result.drive_dio == 5
    assert result.drive_role == 'normally_closed'
    assert result.no_dio == 2
    assert result.nc_dio == 2
    assert result.derivation_mode == 'drive_nc_read_common'
    assert result.observed_terminals == ('common_as_normally_closed',)
    assert result.derive_no_from_nc


def test_not_connected_nc_terminal_can_read_common_and_drive_no() -> None:
    result = resolve_ptp_switch_config(
        ptp_params={
            'NormallyOpenTerminal': '6',
            'NormallyClosedTerminal': '0',
            'CommonTerminal': '3',
        },
        port_id='port_a',
        port_config={'switch_sensed_db9_pins': [3]},
    )

    assert result.is_valid
    assert result.common_dio == 2
    assert result.drive_dio == 5
    assert result.drive_role == 'normally_open'
    assert result.no_dio == 2
    assert result.nc_dio == 2
    assert result.derivation_mode == 'drive_no_read_common'
    assert result.observed_terminals == ('common_as_normally_open',)
    assert result.derive_nc_from_no


def test_spst_adapter_fallback_preferred_for_com1_no2_sense_pin3() -> None:
    """Fixture sense is DB9 pin 3; PTP product terminals are COM=1 / NO=2."""
    result = resolve_ptp_switch_config(
        ptp_params={
            'NormallyOpenTerminal': '2',
            'NormallyClosedTerminal': '0',
            'CommonTerminal': '1',
        },
        port_id='port_a',
        port_config={
            'switch_sensed_db9_pins': [3],
            'switch_spst_read_source': 'adapter_common',
        },
    )

    assert result.is_valid
    assert result.drive_dio == 1  # drive NO pin 2
    assert result.no_dio == 2  # read stand sense pin 3
    assert result.nc_dio == 2
    assert result.drive_role == 'normally_open'
    assert result.derivation_mode == 'drive_no_read_adapter_common'
    assert result.observed_terminals == ('adapter_common_as_normally_open',)
    assert result.derive_nc_from_no
    assert any('SPST adapter fallback' in warning for warning in result.warnings)


def test_spst_ptp_fallback_helper_reads_connected_no_throw() -> None:
    from app.services.ptp_switch_resolver import _resolve_single_throw_ptp_fallback

    warnings: list[str] = []
    fallback = _resolve_single_throw_ptp_fallback(
        'port_a',
        common_terminal=1,
        common_dio=0,
        no_terminal=2,
        no_dio=1,
        nc_terminal=None,
        nc_dio=None,
        sensed_pins=(3,),
        warnings=warnings,
    )

    assert fallback is not None
    drive_terminal, drive_dio, drive_role, no_dio, nc_dio, observed, mode, der_nc, der_no = fallback
    assert drive_terminal == 1
    assert drive_dio == 0
    assert drive_role == 'common'
    assert no_dio == 1
    assert nc_dio == 1
    assert observed == ('normally_open_single_throw',)
    assert mode == 'derive_nc_from_no'
    assert der_nc and not der_no
    assert any('SPST PTP fallback' in warning for warning in warnings)


def test_spst_default_uses_ptp_throw_until_adapter_topology_is_configured() -> None:
    result = resolve_ptp_switch_config(
        ptp_params={
            'NormallyOpenTerminal': '2',
            'NormallyClosedTerminal': '0',
            'CommonTerminal': '1',
        },
        port_id='port_a',
        port_config={'switch_sensed_db9_pins': [3]},
    )

    assert result.is_valid
    assert result.drive_dio == 0
    assert result.no_dio == 1
    assert result.derivation_mode == 'derive_nc_from_no'
    assert any('SPST PTP fallback' in warning for warning in result.warnings)


def test_spst_adapter_fallback_reads_sense_pin_for_nc_throw() -> None:
    result = resolve_ptp_switch_config(
        ptp_params={
            'NormallyOpenTerminal': '0',
            'NormallyClosedTerminal': '2',
            'CommonTerminal': '1',
        },
        port_id='port_b',
        port_config={
            'switch_sensed_db9_pins': [3],
            'switch_spst_read_source': 'adapter_common',
        },
    )

    assert result.is_valid
    assert result.drive_dio == 10  # drive NC pin 2 on port_b
    assert result.no_dio == 11  # read stand sense pin 3 -> DIO11
    assert result.nc_dio == 11
    assert result.drive_role == 'normally_closed'
    assert result.derivation_mode == 'drive_nc_read_adapter_common'
    assert result.observed_terminals == ('adapter_common_as_normally_closed',)
    assert result.derive_no_from_nc
    assert any('SPST adapter fallback' in warning for warning in result.warnings)


def test_both_throw_terminals_not_connected_fails() -> None:
    result = resolve_ptp_switch_config(
        ptp_params={
            'NormallyOpenTerminal': '0',
            'NormallyClosedTerminal': '0',
            'CommonTerminal': '4',
        },
        port_id='port_a',
        port_config={'switch_sensed_db9_pins': [3]},
    )

    assert not result.is_valid
    assert any('At least one' in error for error in result.errors)


def test_invalid_ptp_terminal_fails_without_fallback() -> None:
    result = resolve_ptp_switch_config(
        ptp_params={
            'NormallyOpenTerminal': '10',
            'NormallyClosedTerminal': '1',
            'CommonTerminal': '4',
        },
        port_id='port_a',
        port_config={'switch_sensed_db9_pins': [1, 3]},
    )

    assert not result.is_valid
    assert any('NormallyOpenTerminal' in error for error in result.errors)


def test_unobservable_ptp_terminals_fail_without_configured_pin_fallback() -> None:
    result = resolve_ptp_switch_config(
        ptp_params={
            'NormallyOpenTerminal': '2',
            'NormallyClosedTerminal': '5',
            'CommonTerminal': '4',
        },
        port_id='port_a',
        port_config={'switch_sensed_db9_pins': [3]},
    )

    assert not result.is_valid
    assert result.no_dio is None
    assert result.nc_dio is None
    assert any('not observable' in error for error in result.errors)


def test_com5_dual_throw_seq600_uses_drive_nc_read_common() -> None:
    result = resolve_ptp_switch_config(
        ptp_params={
            'NormallyOpenTerminal': '6',
            'NormallyClosedTerminal': '4',
            'CommonTerminal': '5',
        },
        port_id='port_a',
        port_config={'switch_sensed_db9_pins': [3]},
    )

    assert result.is_valid
    assert result.no_dio == 4
    assert result.nc_dio == 4
    assert result.drive_dio == 3
    assert result.drive_role == 'normally_closed'
    assert result.derivation_mode == 'drive_nc_read_common'
    assert result.derive_no_from_nc
    assert any('COM=5 bench' in warning for warning in result.warnings)


def test_com5_dual_throw_seq300_uses_drive_no_read_common() -> None:
    result = resolve_ptp_switch_config(
        ptp_params={
            'NormallyOpenTerminal': '4',
            'NormallyClosedTerminal': '6',
            'CommonTerminal': '5',
        },
        port_id='port_b',
        port_config={'switch_sensed_db9_pins': [3]},
    )

    assert result.is_valid
    assert result.no_dio == 13
    assert result.nc_dio == 13
    assert result.drive_dio == 12
    assert result.drive_role == 'normally_open'
    assert result.derivation_mode == 'drive_no_read_common'
    assert result.derive_nc_from_no


def test_com5_single_nc_throw_maps_sensed_pin3_to_nc() -> None:
    result = resolve_ptp_switch_config(
        ptp_params={
            'NormallyOpenTerminal': '0',
            'NormallyClosedTerminal': '4',
            'CommonTerminal': '5',
        },
        port_id='port_a',
        port_config={'switch_sensed_db9_pins': [3]},
    )

    assert result.is_valid
    assert result.normally_open_terminal is None
    assert result.drive_dio == 3
    assert result.derivation_mode == 'drive_nc_read_common'
    assert result.derive_no_from_nc
