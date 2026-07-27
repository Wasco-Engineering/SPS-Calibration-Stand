"""Tests for Nexus stand-id helpers and PTP repository wiring."""

from __future__ import annotations

from app.services.nexus_service import (
    DEFAULT_EQUIPMENT_ID,
    STAND_KEYS,
    normalize_equipment_id,
    resolve_stand_key,
)
from app.services.ptp_service import configure_ptp_repository, load_ptp_from_db


def test_normalize_legacy_stinger_ids():
    assert normalize_equipment_id("STINGER_01") == "CA-SPS-01"
    assert normalize_equipment_id("STINGER_02") == "CA-SPS-02"
    assert normalize_equipment_id("STINGER_03") == "ID-SPS-01"
    assert normalize_equipment_id("STINGER_04") == "ID-SPS-02"
    assert normalize_equipment_id("CA-SPS-01") == "CA-SPS-01"
    assert normalize_equipment_id("") == DEFAULT_EQUIPMENT_ID


def test_resolve_stand_key_from_equipment_id():
    config = {
        "test_parameters": {"equipment_id": "ID-SPS-01"},
        "nexus": {"stand_keys": STAND_KEYS, "stand_key": None},
    }
    assert resolve_stand_key(config) == STAND_KEYS["ID-SPS-01"]


def test_resolve_stand_key_explicit_override():
    config = {
        "test_parameters": {"equipment_id": "ID-SPS-01"},
        "nexus": {"stand_key": "wsk_explicit"},
    }
    assert resolve_stand_key(config) == "wsk_explicit"


def test_load_ptp_from_db_uses_configured_repository(monkeypatch):
    class FakeSeq:
        parameters = {"ActivationTarget": "10", "UnitsOfMeasure": "1"}

    class FakePtp:
        source = "primary"
        primary_sequence = FakeSeq()

    class FakeRepo:
        def get_ptp(self, part_id, sequence=None):
            assert part_id == "PART"
            assert sequence == "300"
            return FakePtp()

    configure_ptp_repository(FakeRepo())
    try:
        params = load_ptp_from_db("PART", "300")
        assert params["ActivationTarget"] == "10"
    finally:
        configure_ptp_repository(None)


def test_load_ptp_falls_back_to_sql_when_postgres_empty(monkeypatch):
    class FakePtp:
        source = "primary"
        primary_sequence = None

    class FakeRepo:
        def get_ptp(self, part_id, sequence=None):
            return FakePtp()

    called = {"sql": False}

    def fake_sql(part_id, sequence_id):
        called["sql"] = True
        return {"ActivationTarget": "1"}

    configure_ptp_repository(FakeRepo())
    monkeypatch.setattr(
        "app.services.ptp_service.load_test_parameters",
        fake_sql,
    )
    try:
        params = load_ptp_from_db("PART", "300")
        assert called["sql"] is True
        assert params["ActivationTarget"] == "1"
    finally:
        configure_ptp_repository(None)
