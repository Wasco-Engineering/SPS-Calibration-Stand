from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import local_cache, operations
from app.database.models import Base, OrderCalibrationDetail


def _patch_sql_session(monkeypatch, engine):
    Session = sessionmaker(bind=engine)

    @contextmanager
    def _session_scope():
        session = Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(operations, 'session_scope', _session_scope)
    return Session


def _detail(**overrides):
    detail = {
        'ShopOrder': 'WO-1',
        'SequenceID': '399',
        'PartID': 'PART-1',
        'SerialNumber': 1,
        'ActivationID': 1,
        'InspectionDate': datetime.now(),
        'TemperatureC': 25.0,
        'EquipmentID': 'CA-SPS-01',
        'OperatorID': 'OP-1',
        'IncreasingActivation': 12.3,
        'DecreasingDeactivation': 9.8,
        'IncreasingGap': 0.0,
        'DecreasingGap': 0.0,
        'MaxPressureAchieved': 31.25,
        'UnitsOfMeasure': 'PSI',
        'GageReferenceDiff': 14.61,
        'InSpec': True,
    }
    detail.update(overrides)
    return detail


def test_sync_local_cache_uploads_queued_detail(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv('STINGER_CONFIG_DIR', str(tmp_path))
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    Session = _patch_sql_session(monkeypatch, engine)

    local_cache.upsert_master(
        {
            'ShopOrder': 'WO-1',
            'PartID': 'PART-1',
            'SequenceID': '399',
            'OrderQTY': 1,
            'OperatorID': 'OP-1',
            'EquipmentID': 'CA-SPS-01',
        },
        source='queued',
    )
    local_cache.upsert_detail(_detail(), sync_status=local_cache.SYNC_QUEUED)

    result = operations.sync_local_cache()

    assert result['synced'] == 1
    assert result['conflicts'] == 0
    assert local_cache.queued_count() == 0
    with Session() as session:
        saved = session.query(OrderCalibrationDetail).one()
        assert saved.MaxPressureAchieved == 31.25
        assert float(saved.GageReferenceDiff) == 14.61


def test_sync_local_cache_marks_conflict_for_other_equipment(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv('STINGER_CONFIG_DIR', str(tmp_path))
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    Session = _patch_sql_session(monkeypatch, engine)

    with Session() as session:
        session.add(
            OrderCalibrationDetail(
                ShopOrder='WO-1',
                SequenceID='399',
                PartID='PART-1',
                SerialNumber=1,
                ActivationID=1,
                InspectionDate=datetime.now(),
                TemperatureC=25.0,
                EquipmentID='OTHER',
                OperatorID='OP-2',
                IncreasingActivation=12.0,
                DecreasingDeactivation=9.5,
                IncreasingGap=0.0,
                DecreasingGap=0.0,
                MaxPressureAchieved=30.0,
                UnitsOfMeasure='PSI',
                GageReferenceDiff=14.7,
                InSpec=True,
            )
        )
        session.commit()

    local_cache.upsert_detail(_detail(), sync_status=local_cache.SYNC_QUEUED)

    result = operations.sync_local_cache()

    assert result['synced'] == 0
    assert result['conflicts'] == 1
    with local_cache.connect() as conn:
        row = conn.execute('SELECT sync_status FROM order_calibration_detail').fetchone()
    assert row['sync_status'] == local_cache.SYNC_CONFLICT
