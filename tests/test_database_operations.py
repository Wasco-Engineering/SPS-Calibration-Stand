from __future__ import annotations

from app.database import operations


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _FakeConnection:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _sql, _params):
        return _FakeResult(self._row)

    def exec_driver_sql(self, _sql):
        return None


class _FakeEngine:
    def __init__(self, row):
        self._row = row
        self.disposed = False

    def connect(self):
        return _FakeConnection(self._row)

    def dispose(self):
        self.disposed = True


def test_lookup_shop_order_returns_normalized_details(monkeypatch) -> None:
    engine = _FakeEngine({
        'ORDNUM_147': '51034643',
        'PRTNUM_147': 'CERBERUS-575T-SEI        ',
        'CURQTY_147': 40.0,
        'CURDUE_147': None,
        'STATUS_147': ' ',
        'ALTBOM_147': ' BOM1 ',
        'ALTRTG_147': ' RTG1 ',
    })
    monkeypatch.setattr(operations, '_load_runtime_config', lambda: {'database': {}})
    monkeypatch.setattr(operations, '_create_mssql_engine', lambda _cfg: engine)

    result = operations.lookup_shop_order('51034643')

    assert result is not None
    assert result['ShopOrder'] == '51034643'
    assert result['PartID'] == 'CERBERUS-575T-SEI'
    assert result['SequenceID'] == ''
    assert result['OrderQTY'] == 40
    assert result['OrderQty'] == 40
    assert result['AlternateBOM'] == 'BOM1'
    assert result['AlternateRouting'] == 'RTG1'
    assert engine.disposed is True


def test_lookup_shop_order_returns_none_when_missing(monkeypatch) -> None:
    monkeypatch.setattr(operations, '_load_runtime_config', lambda: {'database': {}})
    monkeypatch.setattr(operations, '_create_mssql_engine', lambda _cfg: _FakeEngine(None))

    assert operations.lookup_shop_order('NOPE') is None


def test_validate_shop_order_custom_work_order_wins(monkeypatch) -> None:
    monkeypatch.setattr(
        operations,
        '_load_runtime_config',
        lambda: {
            'custom_work_orders': {
                'stinger228': {
                    'part_id': 'SPS00000',
                    'sequence_id': '300',
                    'order_qty': 1,
                },
            },
        },
    )

    def _unexpected_lookup(_shop_order):
        raise AssertionError('MAX lookup should not run for custom work orders')

    monkeypatch.setattr(operations, 'lookup_shop_order', _unexpected_lookup)

    result = operations.validate_shop_order('stinger228')

    assert result is not None
    assert result['PartID'] == 'SPS00000'
    assert result['SequenceID'] == '300'
    assert result['OrderQTY'] == 1


def test_validate_shop_order_uses_max_part_over_calibration_context(monkeypatch) -> None:
    monkeypatch.setattr(
        operations,
        'lookup_shop_order',
        lambda _shop_order: {
            'ShopOrder': '51026425',
            'PartID': 'SPS-17123',
            'SequenceID': '',
            'OrderQTY': 40,
            'OrderQty': 40,
        },
    )
    monkeypatch.setattr(
        operations,
        'lookup_work_order_master',
        lambda _shop_order, _part_id=None: {
            'ShopOrder': '51026425',
            'PartID': '200300',
            'SequenceID': '300',
            'OrderQTY': 40,
            'OrderQty': 40,
        },
    )
    monkeypatch.setattr(operations.local_cache, 'upsert_master', lambda *_args, **_kwargs: None)

    result = operations.validate_shop_order('51026425')

    assert result is not None
    assert result['PartID'] == 'SPS-17123'
    assert result['SequenceID'] == '300'


def test_validate_shop_order_uses_detail_sequence_when_exact_master_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        operations,
        'lookup_shop_order',
        lambda _shop_order: {
            'ShopOrder': '51026425',
            'PartID': 'SPS-17123',
            'SequenceID': '',
            'OrderQTY': 40,
            'OrderQty': 40,
        },
    )
    monkeypatch.setattr(operations, 'lookup_work_order_master', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(operations, '_lookup_detail_sequence_online', lambda *_args: '300')
    monkeypatch.setattr(operations.local_cache, 'upsert_master', lambda *_args, **_kwargs: None)

    result = operations.validate_shop_order('51026425')

    assert result is not None
    assert result['PartID'] == 'SPS-17123'
    assert result['SequenceID'] == '300'


def test_prefer_sps_entry_sequence_overrides_600_when_300_ptp_exists(monkeypatch) -> None:
    monkeypatch.setattr(
        operations,
        '_ptp_sequence_available',
        lambda part_id, sequence_id: sequence_id == '300',
    )
    assert operations.prefer_sps_entry_sequence('SPS01438-02', '600') == '300'
    assert operations.prefer_sps_entry_sequence('SPS01438-02', '650') == '300'
    assert operations.prefer_sps_entry_sequence('SPS01438-02', '') == '300'


def test_prefer_sps_entry_sequence_keeps_non_sps_and_keeps_300(monkeypatch) -> None:
    monkeypatch.setattr(operations, '_ptp_sequence_available', lambda *_args: True)
    assert operations.prefer_sps_entry_sequence('CERBERUS-575T-SEI', '600') == '600'
    assert operations.prefer_sps_entry_sequence('SPS01438-02', '300') == '300'
    assert operations.prefer_sps_entry_sequence('SPS01438-02', '399') == '399'


def test_prefer_sps_entry_sequence_keeps_600_when_no_300_ptp(monkeypatch) -> None:
    monkeypatch.setattr(operations, '_ptp_sequence_available', lambda *_args: False)
    assert operations.prefer_sps_entry_sequence('SPS01438-02', '600') == '600'


def test_validate_shop_order_prefers_300_over_master_600_for_sps(monkeypatch) -> None:
    monkeypatch.setattr(
        operations,
        'lookup_shop_order',
        lambda _shop_order: {
            'ShopOrder': '51036214',
            'PartID': 'SPS01438-02',
            'SequenceID': '',
            'OrderQTY': 10,
            'OrderQty': 10,
        },
    )
    monkeypatch.setattr(
        operations,
        'lookup_work_order_master',
        lambda *_args, **_kwargs: {
            'ShopOrder': '51036214',
            'PartID': 'SPS01438-02',
            'SequenceID': '600',
            'LastSequenceCalibrated': '600',
        },
    )
    monkeypatch.setattr(
        operations,
        '_ptp_sequence_available',
        lambda part_id, sequence_id: sequence_id == '300',
    )
    monkeypatch.setattr(operations.local_cache, 'upsert_master', lambda *_args, **_kwargs: None)

    result = operations.validate_shop_order('51036214')

    assert result is not None
    assert result['SequenceID'] == '300'


def test_validate_shop_order_does_not_use_master_when_max_says_missing(monkeypatch) -> None:
    monkeypatch.setattr(operations, 'lookup_shop_order', lambda _shop_order: None)

    def _unexpected_master_lookup(*_args, **_kwargs):
        raise AssertionError('Master fallback should only run when MAX lookup fails')

    monkeypatch.setattr(operations, 'lookup_work_order_master', _unexpected_master_lookup)

    assert operations.validate_shop_order('51039999') is None


def test_validate_shop_order_falls_back_to_master_when_max_unavailable(monkeypatch) -> None:
    def _raise_max_error(_shop_order):
        raise RuntimeError('MAX unavailable')

    monkeypatch.setattr(operations, 'lookup_shop_order', _raise_max_error)
    monkeypatch.setattr(
        operations,
        'lookup_work_order_master',
        lambda _shop_order, _part_id=None: {
            'ShopOrder': '51026425',
            'PartID': 'SPS-17123',
            'SequenceID': '300',
            'OrderQTY': 40,
            'OrderQty': 40,
        },
    )

    result = operations.validate_shop_order('51026425')

    assert result is not None
    assert result['PartID'] == 'SPS-17123'
    assert result['SequenceID'] == '300'
