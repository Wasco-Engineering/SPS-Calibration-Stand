"""Tests for Mensor pressure parsing and channel selection."""

from __future__ import annotations

import pytest

from quality_cal.core.mensor_reader import MensorReader


def test_parse_scientific_psia_field() -> None:
    assert MensorReader._parse_pressure('+1.34419E+01') == 13.4419


def test_parse_comma_separated_scientific() -> None:
    assert MensorReader._parse_pressure('0.159,+1.09813E+01,other') == 10.9813


def test_parse_legacy_e_prefix_field() -> None:
    assert MensorReader._parse_pressure('E+1.09813E+01') == 10.9813


def test_parse_e_prefix_near_zero_negative() -> None:
    assert MensorReader._parse_pressure('E-9.16004E-03') == pytest.approx(-0.00916004)


def test_parse_near_zero_positive() -> None:
    assert MensorReader._parse_pressure('E+4.33254E-03') == pytest.approx(0.00433254)


def test_resolve_channel_default_and_port_map() -> None:
    reader = MensorReader(
        {
            'port': 'COM5',
            'channel': 'B',
            'port_channels': {'port_a': 'A', 'port_b': 'B'},
        }
    )
    assert reader.default_channel == 'B'
    assert reader.resolve_channel() == 'B'
    assert reader.resolve_channel('A') == 'A'
    assert reader.resolve_channel(port_id='port_a') == 'A'
    assert reader.resolve_channel(port_id='port_b') == 'B'
    assert reader._query_command('A') == 'A?'
    assert reader._query_command('B') == 'B?'
    assert reader._query_command(None) == '?'


def test_normalize_channel_aliases() -> None:
    assert MensorReader._normalize_channel('a') == 'A'
    assert MensorReader._normalize_channel('channel B') == 'B'
    assert MensorReader._normalize_channel('BAROMETER') == 'BARO'
    assert MensorReader._normalize_channel('active') is None
    with pytest.raises(ValueError):
        MensorReader._normalize_channel('Z')
