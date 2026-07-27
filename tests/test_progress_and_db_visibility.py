from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import local_cache, operations
from app.database.models import Base, OrderCalibrationDetail, OrderCalibrationMaster
from app.services import work_order_controller
from app.services.work_order_controller import WorkOrderController
from app.ui.main_window import MainWindow


class _FakeUiBridge:
    def __init__(self, work_order: dict[str, object]) -> None:
        self._current_work_order = work_order
        self._port_serials = {'port_a': 7}
        self.status_updates: list[dict[str, str]] = []
        self.allocated_serials: list[tuple[str, int]] = []
        self.progress_updates: list[tuple[int, int, int, int]] = []

    def get_pressure_unit(self) -> str:
        return 'PSI'

    def update_database_status(self, status: str, last_write: str = '--', queue: str = '0') -> None:
        self.status_updates.append(
            {
                'status': status,
                'last_write': last_write,
                'queue': queue,
            }
        )

    def allocate_serial(self, port_id: str, serial: int) -> None:
        self._port_serials[port_id] = serial
        self.allocated_serials.append((port_id, serial))

    def update_progress(self, completed: int, total: int, passed: int, failed: int) -> None:
        self.progress_updates.append((completed, total, passed, failed))


class _FakeStateMachine:
    def __init__(self) -> None:
        self._increasing_activation = 12.3
        self._decreasing_deactivation = 9.8
        self._attempt_count = 0
        self.reset_calls = 0

    def reset_for_new_unit(self) -> None:
        self.reset_calls += 1


def _make_save_controller(
    *,
    test_mode: bool = False,
    activation_direction: str = 'Increasing',
) -> WorkOrderController:
    controller = WorkOrderController.__new__(WorkOrderController)
    controller._ui_bridge = _FakeUiBridge(
        {
            'test_mode': test_mode,
            'shop_order': 'WO-1',
            'part_id': 'PART-1',
            'sequence_id': '1',
            'operator_id': 'OP-1',
        }
    )
    controller._state_machines = {'port_a': _FakeStateMachine()}
    controller._current_test_setup = SimpleNamespace(
        units_label='PSI',
        pressure_reference=None,
        activation_direction=activation_direction,
    )
    controller._config = {'test_parameters': {'equipment_id': 'CA-SPS-01'}}
    controller._db_connection_status = 'Connected'
    controller._db_last_write = '--'
    controller._db_queue = '0'
    controller._db_activity_status = None
    controller._db_activity_deadline = 0.0
    controller._last_db_status = 'Connected'
    controller._to_display_pressure = lambda _port_id, value, _units, _ref: value
    controller._get_barometric_pressure = lambda _port_id: 14.61
    controller._max_test_pressure_display = {'port_a': 31.25}
    return controller


def test_format_progress_display_caps_percent_for_overrun() -> None:
    progress_text, percent_text, progress_max, progress_value, tooltip = (
        MainWindow._format_progress_display(16, 1)
    )

    assert progress_text == '16 / 1 (+15)'
    assert percent_text == '100%'
    assert progress_max == 1
    assert progress_value == 1
    assert 'exceed the work order quantity by 15' in tooltip


def test_format_progress_display_uses_completed_count_not_remaining() -> None:
    progress_text, percent_text, progress_max, progress_value, tooltip = (
        MainWindow._format_progress_display(3, 10)
    )

    assert progress_text == '3 / 10'
    assert percent_text == '30%'
    assert progress_max == 10
    assert progress_value == 3
    assert tooltip == ''


def test_normalize_progress_counts_uses_completed_when_total_missing() -> None:
    completed, total = WorkOrderController._normalize_progress_counts(
        0,
        5,
        context='test progress',
    )

    assert completed == 5
    assert total == 5


def test_progress_and_next_serial_include_legacy_sequence_format(monkeypatch) -> None:
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        session.add_all(
            [
                OrderCalibrationDetail(
                    ShopOrder='51020234',
                    SequenceID='399',
                    PartID='17021',
                    SerialNumber=1,
                    ActivationID=1,
                    IncreasingActivation=1.2,
                    DecreasingDeactivation=0.8,
                    TemperatureC=22.0,
                    InSpec=True,
                    UnitsOfMeasure='Torr',
                    InspectionDate=datetime.now(),
                    OperatorID='NB',
                    EquipmentID='CA-SPS-01',
                ),
                OrderCalibrationDetail(
                    ShopOrder='51020234',
                    SequenceID='399',
                    PartID='17021',
                    SerialNumber=2,
                    ActivationID=1,
                    IncreasingActivation=1.3,
                    DecreasingDeactivation=0.9,
                    TemperatureC=22.0,
                    InSpec=False,
                    UnitsOfMeasure='Torr',
                    InspectionDate=datetime.now(),
                    OperatorID='NB',
                    EquipmentID='CA-SPS-01',
                ),
                OrderCalibrationDetail(
                    ShopOrder='51020234',
                    SequenceID='399',
                    PartID='17021',
                    SerialNumber=3,
                    ActivationID=1,
                    IncreasingActivation=1.4,
                    DecreasingDeactivation=1.0,
                    TemperatureC=22.0,
                    InSpec=True,
                    UnitsOfMeasure='Torr',
                    InspectionDate=datetime.now(),
                    OperatorID='NB',
                    EquipmentID='CA-SPS-01',
                ),
                OrderCalibrationDetail(
                    ShopOrder='51020234',
                    SequenceID='399',
                    PartID='17021',
                    SerialNumber=3,
                    ActivationID=2,
                    IncreasingActivation=1.5,
                    DecreasingDeactivation=1.1,
                    TemperatureC=22.0,
                    InSpec=False,
                    UnitsOfMeasure='Torr',
                    InspectionDate=datetime.now(),
                    OperatorID='NB',
                    EquipmentID='CA-SPS-01',
                ),
            ]
        )
        session.commit()

    @contextmanager
    def _session_scope():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(operations, 'session_scope', _session_scope)

    progress = operations.get_work_order_progress('51020234', '17021', '0399')

    assert progress == {'completed': 3, 'passed': 2, 'failed': 1}
    assert operations.get_next_serial_number('51020234', '17021', '0399') == 4


def test_save_result_reports_saved_status(monkeypatch) -> None:
    controller = _make_save_controller()
    monkeypatch.setattr(work_order_controller, 'save_test_result', lambda **_kwargs: True)
    monkeypatch.setattr(work_order_controller, 'get_local_queue_count', lambda: 0)

    result = controller._save_result('port_a', force_pass=True)

    assert result == 'saved'
    assert controller._ui_bridge.status_updates[-1]['status'] == 'Saved'
    assert controller._ui_bridge.status_updates[-1]['last_write'] != '--'
    assert controller._ui_bridge.status_updates[-1]['queue'] == '0'


def test_save_result_maps_increasing_direction_edges_to_database_fields(monkeypatch) -> None:
    controller = _make_save_controller(activation_direction='Increasing')
    captured = {}
    monkeypatch.setattr(
        work_order_controller,
        'save_test_result',
        lambda **kwargs: captured.update(kwargs) or True,
    )
    monkeypatch.setattr(work_order_controller, 'get_local_queue_count', lambda: 0)

    assert controller._save_result('port_a', force_pass=True) == 'saved'

    assert captured['increasing_activation'] == 12.3
    assert captured['decreasing_deactivation'] == 9.8
    assert captured['max_pressure_achieved'] == 31.25
    assert captured['gage_reference_diff'] == 14.61


def test_detail_equipment_ids_are_port_specific() -> None:
    assert work_order_controller._detail_equipment_id('CA-SPS-01', 'port_a') == 'CA-SPS-01-L'
    assert work_order_controller._detail_equipment_id('CA-SPS-01', 'port_b') == 'CA-SPS-01-R'
    assert work_order_controller._detail_equipment_id('CA-SPS-01', 'other') == 'CA-SPS-01'


def test_save_result_maps_decreasing_direction_edges_to_database_fields(monkeypatch) -> None:
    controller = _make_save_controller(activation_direction='Decreasing')
    controller._state_machines['port_a']._attempt_count = 2
    captured = {}
    monkeypatch.setattr(
        work_order_controller,
        'save_test_result',
        lambda **kwargs: captured.update(kwargs) or True,
    )
    monkeypatch.setattr(work_order_controller, 'get_local_queue_count', lambda: 0)

    assert controller._save_result('port_a', force_pass=True) == 'saved'

    assert captured['increasing_activation'] == 9.8
    assert captured['decreasing_deactivation'] == 12.3
    assert captured['activation_id'] == 1


def test_save_result_uses_zero_sentinel_for_no_switch_failure(monkeypatch) -> None:
    controller = _make_save_controller()
    controller._state_machines['port_a']._increasing_activation = None
    controller._state_machines['port_a']._decreasing_deactivation = None
    captured = {}
    monkeypatch.setattr(
        work_order_controller,
        'save_test_result',
        lambda **kwargs: captured.update(kwargs) or True,
    )
    monkeypatch.setattr(work_order_controller, 'get_local_queue_count', lambda: 0)

    result = controller._save_result(
        'port_a',
        force_pass=False,
        allow_null_measurements=True,
    )

    assert result == 'saved'
    assert captured['increasing_activation'] == 0.0
    assert captured['decreasing_deactivation'] == 0.0
    assert captured['in_spec'] is False


def test_save_result_reports_test_mode_skip(monkeypatch) -> None:
    controller = _make_save_controller(test_mode=True)

    def _unexpected_save(**_kwargs):
        raise AssertionError('save_test_result should not be called in test mode')

    monkeypatch.setattr(work_order_controller, 'save_test_result', _unexpected_save)

    result = controller._save_result('port_a', force_pass=False)

    assert result == 'skipped'
    assert controller._ui_bridge.status_updates[-1] == {
        'status': 'Test Mode',
        'last_write': 'Skipped',
        'queue': '0',
    }


def test_save_result_reports_failed_write(monkeypatch) -> None:
    controller = _make_save_controller()

    def _raise_runtime_error(**_kwargs):
        raise RuntimeError('Database not initialized')

    monkeypatch.setattr(work_order_controller, 'save_test_result', _raise_runtime_error)
    monkeypatch.setattr(work_order_controller, 'get_local_queue_count', lambda: 1)

    result = controller._save_result('port_a', force_pass=True)

    assert result == 'failed'
    assert controller._ui_bridge.status_updates[-1]['status'] == 'Write Failed'
    assert controller._ui_bridge.status_updates[-1]['queue'] == '1'


def test_advance_serial_increments_current_port_without_db_next(monkeypatch) -> None:
    controller = _make_save_controller()
    controller._ui_bridge._port_serials = {'port_a': 1, 'port_b': 2}

    def _unexpected_next_serial(*_args, **_kwargs):
        raise AssertionError('post-record advance should not ask DB for next serial')

    monkeypatch.setattr(work_order_controller, 'get_next_serial_number', _unexpected_next_serial)
    monkeypatch.setattr(
        work_order_controller,
        'get_work_order_progress',
        lambda *_args, **_kwargs: {'completed': 4, 'passed': 3, 'failed': 1},
    )

    controller._advance_serial('port_a')

    assert controller._ui_bridge._port_serials == {'port_a': 3, 'port_b': 2}
    assert controller._ui_bridge.allocated_serials[-1] == ('port_a', 3)
    assert controller._state_machines['port_a'].reset_calls == 1


def test_bump_serial_increment_skips_other_port() -> None:
    controller = _make_save_controller()
    controller._ui_bridge._port_serials = {'port_a': 1, 'port_b': 2}

    controller._bump_serial('port_a', 1)

    assert controller._ui_bridge.allocated_serials[-1] == ('port_a', 3)
    assert controller._ui_bridge._port_serials['port_b'] == 2


def test_bump_serial_increment_does_not_jump_to_far_other_port() -> None:
    controller = _make_save_controller()
    controller._ui_bridge._port_serials = {'port_a': 1, 'port_b': 25}

    controller._bump_serial('port_a', 1)

    assert controller._ui_bridge.allocated_serials[-1] == ('port_a', 2)
    assert controller._ui_bridge._port_serials['port_b'] == 25


def test_save_test_result_queues_locally_for_unexpected_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv('STINGER_CONFIG_DIR', str(tmp_path))

    @contextmanager
    def _broken_scope():
        raise RuntimeError('Database not initialized')
        yield

    monkeypatch.setattr(operations, 'session_scope', _broken_scope)

    result = operations.save_test_result(
        shop_order='WO-1',
        part_id='PART-1',
        sequence_id='1',
        serial_number=1,
        increasing_activation=12.3,
        decreasing_deactivation=9.8,
        in_spec=True,
        temperature_c=25.0,
        units_of_measure='PSI',
        operator_id='OP-1',
        equipment_id='CA-SPS-01',
        max_pressure_achieved=31.25,
        gage_reference_diff=14.61,
    )

    assert result is True
    assert local_cache.queued_count() == 1


def test_save_test_result_accepts_zero_no_switch_sentinel(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv('STINGER_CONFIG_DIR', str(tmp_path))
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    @contextmanager
    def _session_scope():
        session = Session()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr(operations, 'session_scope', _session_scope)

    result = operations.save_test_result(
        shop_order='WO-1',
        part_id='PART-1',
        sequence_id='1',
        serial_number=1,
        increasing_activation=0.0,
        decreasing_deactivation=0.0,
        in_spec=False,
        temperature_c=25.0,
        units_of_measure='PSI',
        operator_id='OP-1',
        equipment_id='CA-SPS-01',
        max_pressure_achieved=0.0,
        gage_reference_diff=14.61,
    )

    assert result is True
    with Session() as session:
        saved = session.query(OrderCalibrationDetail).one()
        master = session.query(OrderCalibrationMaster).one()
        assert saved.IncreasingActivation == 0.0
        assert saved.DecreasingDeactivation == 0.0
        assert saved.MaxPressureAchieved == 0.0
        assert float(saved.GageReferenceDiff) == 14.61
        assert saved.InSpec is False
        assert master.CreatedBy == 'CA-SPS-01'


def test_detail_writer_refuses_cross_stand_overwrite() -> None:
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        existing = OrderCalibrationDetail(
            ShopOrder='WO-1',
            SequenceID='1',
            PartID='PART-1',
            SerialNumber=1,
            ActivationID=1,
            IncreasingActivation=20.0,
            DecreasingDeactivation=10.0,
            TemperatureC=25.0,
            MaxPressureAchieved=30.0,
            InSpec=True,
            UnitsOfMeasure='PSI',
            EquipmentID='NCRS',
            OperatorID='OP-2',
        )
        session.add(existing)
        session.flush()

        detail = operations._build_detail_payload(
            shop_order='WO-1',
            part_id='PART-1',
            sequence_id='1',
            serial_number=1,
            activation_id=1,
            increasing_activation=21.0,
            decreasing_deactivation=11.0,
            in_spec=False,
            temperature_c=25.0,
            units_of_measure='PSI',
            operator_id='OP-1',
            equipment_id='CA-SPS-01',
            max_pressure_achieved=31.0,
            gage_reference_diff=14.6,
        )

        with pytest.raises(operations.DetailWriteConflict):
            operations._save_detail_payload_to_session(session, detail)

        session.refresh(existing)
        assert existing.EquipmentID == 'NCRS'
        assert existing.IncreasingActivation == 20.0


def test_save_test_result_normalizes_legacy_activation_ids_to_one(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv('STINGER_CONFIG_DIR', str(tmp_path))
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    @contextmanager
    def _session_scope():
        session = Session()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr(operations, 'session_scope', _session_scope)

    assert operations.save_test_result(
        shop_order='WO-1',
        part_id='PART-1',
        sequence_id='1',
        serial_number=1,
        increasing_activation=12.3,
        decreasing_deactivation=9.8,
        in_spec=True,
        temperature_c=25.0,
        units_of_measure='PSI',
        operator_id='OP-1',
        equipment_id='CA-SPS-01',
        activation_id=4,
    ) is True

    assert operations.save_test_result(
        shop_order='WO-1',
        part_id='PART-1',
        sequence_id='1',
        serial_number=1,
        increasing_activation=13.4,
        decreasing_deactivation=10.2,
        in_spec=False,
        temperature_c=26.0,
        units_of_measure='PSI',
        operator_id='OP-2',
        equipment_id='CA-SPS-01',
        activation_id=2,
    ) is True

    with Session() as session:
        rows = session.query(OrderCalibrationDetail).all()
        assert len(rows) == 1
        assert rows[0].ActivationID == 1
        assert float(rows[0].IncreasingActivation) == pytest.approx(13.4)
        assert float(rows[0].DecreasingDeactivation) == pytest.approx(10.2)


def test_save_result_keeps_shared_master_equipment_id(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv('STINGER_CONFIG_DIR', str(tmp_path))
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    @contextmanager
    def _session_scope():
        session = Session()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr(operations, 'session_scope', _session_scope)

    assert operations.save_test_result(
        shop_order='WO-1',
        part_id='PART-1',
        sequence_id='1',
        serial_number=1,
        increasing_activation=12.3,
        decreasing_deactivation=9.8,
        in_spec=True,
        temperature_c=25.0,
        units_of_measure='PSI',
        operator_id='OP-1',
        equipment_id='CA-SPS-01-R',
        master_equipment_id='CA-SPS-01',
    )
    with Session() as session:
        detail = session.query(OrderCalibrationDetail).one()
        master = session.query(OrderCalibrationMaster).one()
        assert detail.EquipmentID == 'CA-SPS-01-R'
        assert master.EquipmentID == 'CA-SPS-01'


def test_ensure_work_order_master_uses_composite_key_without_rewriting_other_part(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv('STINGER_CONFIG_DIR', str(tmp_path))
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        session.add(
            OrderCalibrationMaster(
                ShopOrder='51026425',
                PartID='200300',
                LastSequenceCalibrated='300',
                OrderQTY=40,
                OperatorID='CP-01',
                EquipmentID='CA-SPS-01',
                CalibrationDate=datetime.now(),
                ModificationDate=datetime.now(),
                CreatedBy='CA-SPS-01',
                CreationDate=datetime.now(),
                ModifiedBy='CA-SPS-01',
            )
        )
        session.commit()

    @contextmanager
    def _session_scope():
        session = Session()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr(operations, 'session_scope', _session_scope)

    assert operations.ensure_work_order_master(
        shop_order='51026425',
        part_id='SPS-17123',
        sequence_id='300',
        order_qty=40,
        operator_id='CP-01',
        equipment_id='CA-SPS-01',
    )

    with Session() as session:
        rows = {
            row.PartID: row
            for row in session.query(OrderCalibrationMaster)
            .filter_by(ShopOrder='51026425')
            .all()
        }
        assert set(rows) == {'200300', 'SPS-17123'}
        assert rows['200300'].LastSequenceCalibrated == '300'
        assert rows['SPS-17123'].LastSequenceCalibrated == '300'


def test_save_test_result_accepts_null_measurements(monkeypatch) -> None:
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    @contextmanager
    def _session_scope():
        session = Session()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr(operations, 'session_scope', _session_scope)

    result = operations.save_test_result(
        shop_order='WO-1',
        part_id='PART-1',
        sequence_id='1',
        serial_number=1,
        increasing_activation=None,
        decreasing_deactivation=None,
        in_spec=False,
        temperature_c=25.0,
        units_of_measure='PSI',
        operator_id='OP-1',
        equipment_id='CA-SPS-01',
    )

    assert result is True
    with Session() as session:
        saved = session.query(OrderCalibrationDetail).one()
        assert saved.IncreasingActivation is None
        assert saved.DecreasingDeactivation is None
        assert saved.InSpec is False


def test_save_test_result_rejects_overlength_fixed_width_fields(caplog) -> None:
    result = operations.save_test_result(
        shop_order='SHOPORDER123',
        part_id='PART-1',
        sequence_id='1',
        serial_number=1,
        increasing_activation=12.3,
        decreasing_deactivation=9.8,
        in_spec=True,
        temperature_c=25.0,
        units_of_measure='PSI',
        operator_id='TOO-LONG',
        equipment_id='CA-SPS-01',
    )

    assert result is False
    assert 'exceeds max length' in caplog.text
