"""
Database operations for Stinger.

Provides high-level functions for:
- Validating shop orders
- Loading test parameters
- Saving/updating test results
- Serial number management
"""

import logging
from datetime import datetime
from typing import Any, Dict, Optional, Set

import sqlalchemy
from sqlalchemy import create_engine, func, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError

from . import local_cache
from .models import OrderCalibrationMaster, ProductTestParameters, OrderCalibrationDetail
from .session import session_scope

logger = logging.getLogger(__name__)

ORDER_CAL_DETAIL_LIMITS: Dict[str, int] = {
    'shop_order': 10,
    'sequence_id': 4,
    'part_id': 30,
    'operator_id': 5,
    'equipment_id': 20,
    'units_of_measure': 20,
}

ORDER_CAL_MASTER_LIMITS: Dict[str, int] = {
    'shop_order': 10,
    'part_id': 30,
    'operator_id': 5,
    'equipment_id': 20,
    'created_by': 20,
    'modified_by': 20,
    'activation_target': 50,
}


SHOP_ORDER_LOOKUP_SQL = text(
    """
    SELECT TOP 1
        ORDNUM_147,
        PRTNUM_147,
        CURQTY_147,
        CURDUE_147,
        STATUS_147,
        ALTBOM_147,
        ALTRTG_147
    FROM dbo.ShopOrder
    WHERE LTRIM(RTRIM(ORDNUM_147)) = :shop_order
    """
)


def _clean_string(value: Any) -> str:
    """Normalize string-ish database inputs before validation."""
    if value is None:
        return ''
    return str(value).strip()


def _validate_fixed_width(value: str, *, field_name: str, max_length: int) -> bool:
    """Reject values that exceed live SQL Server fixed-width column limits."""
    if len(value) <= max_length:
        return True
    logger.error(
        'Database write rejected: %s=%r exceeds max length %d',
        field_name,
        value,
        max_length,
    )
    return False


def _load_runtime_config() -> Dict[str, Any]:
    from app.core.config import load_config

    return load_config()


def _max_database_config(config: Dict[str, Any]) -> Dict[str, Any]:
    database_cfg = dict(config.get('database') or {})
    max_cfg = config.get('max_database') or config.get('shop_order_database') or {}
    if isinstance(max_cfg, dict):
        database_cfg.update(max_cfg)
    database_cfg['database'] = database_cfg.get('database') or 'ExactMAXWasco'
    if database_cfg['database'] == (config.get('database') or {}).get('database'):
        database_cfg['database'] = 'ExactMAXWasco'
    return database_cfg


def _create_mssql_engine(database_cfg: Dict[str, Any]):
    server = database_cfg.get('server', 'PASCAL')
    database = database_cfg.get('database', 'ExactMAXWasco')
    driver = database_cfg.get('driver', 'ODBC Driver 18 for SQL Server')
    timeout = int(float(database_cfg.get('connection_timeout_sec', 5)))
    username = database_cfg.get('username')
    password = database_cfg.get('password')

    query = {
        'driver': driver,
        'TrustServerCertificate': 'yes',
    }
    if username and password:
        connection_url = URL.create(
            'mssql+pyodbc',
            username=username,
            password=password,
            host=server,
            database=database,
            query=query,
        )
    else:
        query['Trusted_Connection'] = 'yes'
        connection_url = URL.create(
            'mssql+pyodbc',
            host=server,
            database=database,
            query=query,
        )

    return create_engine(
        connection_url,
        pool_pre_ping=True,
        connect_args={'timeout': timeout},
    )


def _parse_order_qty(value: Any) -> int:
    try:
        return max(int(float(value)), 0)
    except (TypeError, ValueError):
        return 0


def _as_optional_float(value: Any) -> Optional[float]:
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any, default: float = 0.0) -> float:
    parsed = _as_optional_float(value)
    return default if parsed is None else parsed


def _sequence_id_lookup_values(sequence_id: str) -> list[str]:
    """Return DB sequence formats seen in legacy and current result rows."""
    raw = _clean_string(sequence_id)
    values = [raw] if raw else []
    try:
        sequence_number = int(raw)
    except (TypeError, ValueError):
        candidates = [raw]
    else:
        candidates = [str(sequence_number), f'{sequence_number:04d}']
    for candidate in candidates:
        if candidate and candidate not in values:
            values.append(candidate)
    return values


def _sequence_id_default_storage_value(sequence_id: str) -> str:
    raw = _clean_string(sequence_id)
    try:
        return str(int(raw))
    except (TypeError, ValueError):
        return raw


def _resolve_sequence_id_for_write(session, part_id: str, sequence_id: str) -> str:
    """Choose the sequence representation already used for this part/sequence."""
    part_id_clean = _clean_string(part_id)
    sequence_values = _sequence_id_lookup_values(sequence_id)
    if not part_id_clean or not sequence_values:
        return _sequence_id_default_storage_value(sequence_id)

    try:
        recent_detail = (
            session.query(
                OrderCalibrationDetail.SequenceID,
                func.max(OrderCalibrationDetail.InspectionDate).label('last_seen'),
            )
            .filter(
                OrderCalibrationDetail.PartID == part_id_clean,
                OrderCalibrationDetail.SequenceID.in_(sequence_values),
            )
            .group_by(OrderCalibrationDetail.SequenceID)
            .order_by(func.max(OrderCalibrationDetail.InspectionDate).desc())
            .first()
        )
        if recent_detail and recent_detail.SequenceID:
            return _clean_string(recent_detail.SequenceID)

        ptp_sequence = (
            session.query(ProductTestParameters.SequenceID)
            .filter(
                ProductTestParameters.PartID == part_id_clean,
                ProductTestParameters.SequenceID.in_(sequence_values),
            )
            .first()
        )
        if ptp_sequence and ptp_sequence.SequenceID:
            return _clean_string(ptp_sequence.SequenceID)
    except SQLAlchemyError:
        logger.debug(
            'Failed resolving sequence format for %s/%s; using default',
            part_id,
            sequence_id,
            exc_info=True,
        )

    return _sequence_id_default_storage_value(sequence_id)


def _row_to_master_details(row: OrderCalibrationMaster) -> Dict[str, Any]:
    return {
        'ShopOrder': _clean_string(row.ShopOrder),
        'PartID': _clean_string(row.PartID),
        'WascoDescription': _clean_string(getattr(row, 'WascoDescription', '')),
        'SequenceID': _clean_string(row.LastSequenceCalibrated),
        'LastSequenceCalibrated': _clean_string(row.LastSequenceCalibrated),
        'OrderQTY': _parse_order_qty(row.OrderQTY),
        'OrderQty': _parse_order_qty(row.OrderQTY),
        'OperatorID': _clean_string(row.OperatorID),
        'EquipmentID': _clean_string(row.EquipmentID),
        'StartTime': row.StartTime,
        'FinishTime': row.FinishTime,
        'CalibrationDate': row.CalibrationDate,
        'ModificationDate': row.ModificationDate,
        'TemperatureC': row.TemperatureC,
        'ActivationTarget': _clean_string(row.ActivationTarget),
        'ActivationMaxAllowable': row.ActivationMaxAllowable,
        'ActivationMinAllowable': row.ActivationMinAllowable,
        'CreatedBy': _clean_string(getattr(row, 'CreatedBy', '')),
        'CreationDate': getattr(row, 'CreationDate', None),
        'ModifiedBy': _clean_string(getattr(row, 'ModifiedBy', '')),
    }


def _lookup_work_order_master_online(
    shop_order: str,
    part_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    shop_order_clean = _clean_string(shop_order)
    part_id_clean = _clean_string(part_id)
    if not shop_order_clean:
        return None
    with session_scope() as session:
        query = session.query(OrderCalibrationMaster).filter(
            OrderCalibrationMaster.ShopOrder == shop_order_clean
        )
        if part_id_clean:
            query = query.filter(OrderCalibrationMaster.PartID == part_id_clean)
        row = (
            query.order_by(
                OrderCalibrationMaster.ModificationDate.desc(),
                OrderCalibrationMaster.CalibrationDate.desc(),
                OrderCalibrationMaster.PartID.asc(),
            )
            .first()
        )
        if row is None:
            return None
        details = _row_to_master_details(row)
        local_cache.upsert_master(details, source='sql')
        return details


def lookup_work_order_master(
    shop_order: str,
    part_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return work-order master details, falling back to local cache when SQL is down."""
    part_id_clean = _clean_string(part_id)
    try:
        return _lookup_work_order_master_online(shop_order, part_id_clean)
    except Exception as exc:
        logger.warning('Online master lookup failed for %s: %s', shop_order, exc)
        cached = local_cache.get_master(shop_order)
        if part_id_clean and cached and _clean_string(cached.get('PartID')) != part_id_clean:
            return None
        return cached


def _lookup_detail_sequence_online(shop_order: str, part_id: str) -> str:
    """Return the most likely sequence from existing detail rows for a MAX part."""
    shop_order_clean = _clean_string(shop_order)
    part_id_clean = _clean_string(part_id)
    if not shop_order_clean or not part_id_clean:
        return ''
    try:
        with session_scope() as session:
            row = (
                session.query(
                    OrderCalibrationDetail.SequenceID,
                    func.count(OrderCalibrationDetail.SerialNumber).label('row_count'),
                    func.max(OrderCalibrationDetail.InspectionDate).label('last_seen'),
                )
                .filter(
                    OrderCalibrationDetail.ShopOrder == shop_order_clean,
                    OrderCalibrationDetail.PartID == part_id_clean,
                )
                .group_by(OrderCalibrationDetail.SequenceID)
                .order_by(
                    func.count(OrderCalibrationDetail.SerialNumber).desc(),
                    func.max(OrderCalibrationDetail.InspectionDate).desc(),
                )
                .first()
            )
        return _clean_string(row.SequenceID) if row and row.SequenceID else ''
    except Exception as exc:
        logger.debug(
            'Detail sequence lookup failed for %s/%s: %s',
            shop_order,
            part_id,
            exc,
            exc_info=True,
        )
        return ''


def is_calibration_database_available() -> bool:
    """Return whether WASCO_Calibration (PTP / results) can be reached."""
    try:
        config = _load_runtime_config()
        engine = _create_mssql_engine(dict(config.get('database') or {}))
        try:
            with engine.connect() as conn:
                conn.exec_driver_sql('SELECT 1')
            return True
        finally:
            engine.dispose()
    except Exception as exc:
        logger.error('Calibration database availability check failed: %s', exc)
        return False


def is_shop_order_database_available() -> bool:
    """Return whether the MAX ShopOrder source can be reached."""
    try:
        engine = _create_mssql_engine(_max_database_config(_load_runtime_config()))
        try:
            with engine.connect() as conn:
                conn.exec_driver_sql('SELECT 1')
            return True
        finally:
            engine.dispose()
    except Exception as exc:
        logger.error('ShopOrder database availability check failed: %s', exc)
        return False


def describe_database_connectivity() -> str:
    """Short operator-facing summary when both SQL sources fail."""
    config = _load_runtime_config()
    database_cfg = dict(config.get('database') or {})
    server = database_cfg.get('server', 'PASCAL')
    username = (database_cfg.get('username') or '').strip()
    auth = f'SQL login {username!r}' if username else 'Windows authentication'

    for label, cfg in (
        ('WASCO_Calibration', database_cfg),
        ('ExactMAXWasco', _max_database_config(config)),
    ):
        try:
            engine = _create_mssql_engine(cfg)
            try:
                with engine.connect() as conn:
                    conn.exec_driver_sql('SELECT 1')
            finally:
                engine.dispose()
        except Exception as exc:
            err = str(exc)
            if '18456' in err:
                return (
                    f'SQL login rejected on {server} ({auth}). '
                    'Check database.username and database.password in stinger_config.yaml.'
                )
            if '08001' in err or 'network' in err.lower():
                return f'Cannot reach SQL Server {server} ({label}): network or server name issue.'
            return f'Database error ({label}): {err[:160]}'

    return 'Database connection failed.'


def lookup_shop_order(shop_order: str) -> Optional[Dict[str, Any]]:
    """Look up a shop order in ExactMAXWasco.dbo.ShopOrder."""
    shop_order_clean = _clean_string(shop_order)
    if not shop_order_clean:
        return None

    engine = _create_mssql_engine(_max_database_config(_load_runtime_config()))
    try:
        with engine.connect() as conn:
            row = conn.execute(
                SHOP_ORDER_LOOKUP_SQL,
                {'shop_order': shop_order_clean},
            ).mappings().first()
        if not row:
            logger.warning('Shop order not found in MAX ShopOrder: %s', shop_order)
            return None

        order_qty = _parse_order_qty(row.get('CURQTY_147'))
        details = {
            'ShopOrder': _clean_string(row.get('ORDNUM_147')),
            'PartID': _clean_string(row.get('PRTNUM_147')),
            'SequenceID': '',
            'OrderQTY': order_qty,
            'OrderQty': order_qty,
            'DueDate': row.get('CURDUE_147'),
            'Status': _clean_string(row.get('STATUS_147')),
            'AlternateBOM': _clean_string(row.get('ALTBOM_147')),
            'AlternateRouting': _clean_string(row.get('ALTRTG_147')),
            'OperatorID': None,
            'EquipmentID': None,
        }
        logger.info(
            'Shop order validated from MAX: %s -> %s qty=%s',
            details['ShopOrder'],
            details['PartID'],
            details['OrderQTY'],
        )
        return details
    finally:
        engine.dispose()


def validate_shop_order(shop_order: str) -> Optional[Dict[str, Any]]:
    """
    Validate a shop order and return work order details.

    Checks custom_work_orders from config first; if not found, queries MAX ShopOrder.

    Args:
        shop_order: The shop order number to validate.

    Returns:
        Dictionary with work order details, or None if not found.
    """
    if not shop_order:
        return None

    shop_order_clean = shop_order.strip()

    # Check config-based custom work orders (e.g. stinger228)
    try:
        config = _load_runtime_config()
        custom = config.get("custom_work_orders") or {}
        if isinstance(custom, dict) and shop_order_clean in custom:
            c = custom[shop_order_clean]
            if isinstance(c, dict):
                part_id = str(c.get("part_id", "")).strip()
                sequence_id = str(c.get("sequence_id", "")).strip()
                order_qty = c.get("order_qty", 1)
                if part_id and sequence_id:
                    details = {
                        "ShopOrder": shop_order_clean,
                        "PartID": part_id,
                        "SequenceID": sequence_id,
                        "OrderQTY": order_qty,
                        "OrderQty": order_qty,
                        "OperatorID": None,
                        "EquipmentID": None,
                    }
                    logger.info(f"Custom work order validated: {shop_order} -> {part_id}/{sequence_id}")
                    return details
    except Exception as e:
        logger.debug("Custom work order check failed: %s", e)

    max_lookup_failed = False
    try:
        details = lookup_shop_order(shop_order_clean)
    except Exception as e:
        max_lookup_failed = True
        details = None
        logger.error(f"MAX database error validating shop order: {e}")

    if details:
        part_id = _clean_string(details.get('PartID'))
        sequence_id = ''
        master = lookup_work_order_master(shop_order_clean, part_id)
        if master:
            sequence_id = _clean_string(master.get('SequenceID'))
        if not sequence_id:
            sequence_id = _lookup_detail_sequence_online(shop_order_clean, part_id)
        if sequence_id:
            details['SequenceID'] = sequence_id
            details['LastSequenceCalibrated'] = sequence_id
        local_cache.upsert_master(details, source='sql')
        logger.info(
            'Work order validated from MAX with calibration context: %s -> %s/%s',
            details.get('ShopOrder'),
            details.get('PartID'),
            details.get('SequenceID'),
        )
        return details

    if max_lookup_failed:
        master = lookup_work_order_master(shop_order_clean)
        if master:
            logger.info(
                'Work order validated from OrderCalibrationMaster fallback: %s -> %s/%s',
                master.get('ShopOrder'),
                master.get('PartID'),
                master.get('SequenceID'),
            )
            return master

        cached = local_cache.get_master(shop_order_clean)
        if cached:
            logger.info(
                'Work order validated from local cache: %s -> %s/%s',
                cached.get('ShopOrder'),
                cached.get('PartID'),
                cached.get('SequenceID'),
            )
            return cached

    return None


def load_test_parameters(part_id: str, sequence_id: str) -> Dict[str, str]:
    """
    Load test parameters for a part/sequence combination.
    
    Args:
        part_id: Part ID from work order.
        sequence_id: Sequence ID from work order.
        
    Returns:
        Dictionary mapping ParameterName -> ParameterValue.
    """
    if not part_id or not sequence_id:
        return {}
    
    try:
        with session_scope() as session:
            # Normalize sequence ID (may be stored with or without zero-padding)
            seq_normalized = str(int(sequence_id.strip()))
            
            # Query PTP
            results = session.query(ProductTestParameters).filter(
                ProductTestParameters.PartID == part_id.strip(),
                func.cast(func.rtrim(ProductTestParameters.SequenceID), sqlalchemy.Integer)
                == int(seq_normalized),
            ).all()
            
            # Convert to dictionary, stripping padding
            params = {}
            for row in results:
                name = row.ParameterName.strip() if row.ParameterName else None
                value = row.ParameterValue.strip() if row.ParameterValue else None
                if name:
                    params[name] = value
            
            logger.info(f"Loaded {len(params)} PTP parameters for {part_id}/{sequence_id}")
            if params:
                local_cache.upsert_ptp(part_id.strip(), seq_normalized, params, source='sql')
            return params
            
    except SQLAlchemyError as e:
        logger.error(f"Database error loading PTP: {e}")
        cached = local_cache.load_ptp(part_id, sequence_id)
        if cached:
            logger.info(
                'Loaded %d PTP parameters for %s/%s from local cache',
                len(cached),
                part_id,
                sequence_id,
            )
        return cached
    except Exception as e:
        logger.error(f"Unexpected error loading PTP: {e}")
        cached = local_cache.load_ptp(part_id, sequence_id)
        if cached:
            logger.info(
                'Loaded %d PTP parameters for %s/%s from local cache',
                len(cached),
                part_id,
                sequence_id,
            )
        return cached



def insert_test_parameters(part_id: str, sequence_id: str, params: Dict[str, str]) -> bool:
    """
    Insert or replace PTP parameters for a part/sequence.

    Deletes any existing rows for (part_id, sequence_id), then inserts the new params.
    Returns True on success.

    Args:
        part_id: Part ID.
        sequence_id: Sequence ID.
        params: Dictionary mapping ParameterName -> ParameterValue (strings).

    Returns:
        True if successful, False on error.
    """
    if not part_id or not sequence_id or not params:
        logger.warning("insert_test_parameters: part_id, sequence_id, and params required")
        return False

    try:
        part_id_clean = part_id.strip()
        seq_normalized = str(int(sequence_id.strip()))

        with session_scope() as session:
            # Delete existing rows for this part/sequence
            deleted = session.query(ProductTestParameters).filter(
                ProductTestParameters.PartID == part_id_clean,
                func.cast(func.rtrim(ProductTestParameters.SequenceID), sqlalchemy.Integer)
                == int(seq_normalized),
            ).delete(synchronize_session=False)

            if deleted:
                logger.info(f"Deleted {deleted} existing PTP rows for {part_id_clean}/{seq_normalized}")

            # Insert new rows
            for param_name, param_value in params.items():
                if not param_name or param_name.strip() == "":
                    continue
                value_str = str(param_value).strip() if param_value is not None else ""
                record = ProductTestParameters(
                    PartID=part_id_clean,
                    SequenceID=seq_normalized,
                    ParameterName=param_name.strip(),
                    ParameterValue=value_str,
                )
                session.add(record)

        logger.info(f"Inserted {len(params)} PTP parameters for {part_id_clean}/{seq_normalized}")
        return True

    except SQLAlchemyError as e:
        logger.error(f"Database error inserting PTP: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error inserting PTP: {e}")
        return False


def _template_master_for_insert(
    session,
    part_id: str,
    sequence_id: str,
) -> Optional[OrderCalibrationMaster]:
    part_id_clean = _clean_string(part_id)
    sequence_values = _sequence_id_lookup_values(sequence_id)
    if part_id_clean and sequence_values:
        template = (
            session.query(OrderCalibrationMaster)
            .filter(
                OrderCalibrationMaster.PartID == part_id_clean,
                OrderCalibrationMaster.LastSequenceCalibrated.in_(sequence_values),
            )
            .order_by(OrderCalibrationMaster.CalibrationDate.desc())
            .first()
        )
        if template:
            return template

    if part_id_clean:
        template = (
            session.query(OrderCalibrationMaster)
            .filter(OrderCalibrationMaster.PartID == part_id_clean)
            .order_by(OrderCalibrationMaster.CalibrationDate.desc())
            .first()
        )
        if template:
            return template

    return (
        session.query(OrderCalibrationMaster)
        .filter(OrderCalibrationMaster.PartID.isnot(None))
        .order_by(OrderCalibrationMaster.CalibrationDate.desc())
        .first()
    )


def _numeric_target_bounds(
    activation_target: Any,
    template: Optional[OrderCalibrationMaster],
) -> tuple[float, float]:
    if template is not None:
        min_limit = _as_optional_float(template.ActivationMinAllowable)
        max_limit = _as_optional_float(template.ActivationMaxAllowable)
        if min_limit is not None and max_limit is not None:
            return min_limit, max_limit

    target = _as_optional_float(activation_target)
    if target is None:
        return 0.0, 0.0
    return target - 1.0, target + 1.0


def _ensure_work_order_master_in_session(
    session,
    shop_order: str,
    part_id: str,
    sequence_id: str,
    order_qty: int = 1,
    activation_target: Optional[Any] = None,
    operator_id: str = '',
    equipment_id: str = '',
    temperature_c: Optional[float] = None,
) -> str:
    """Ensure a master/header row exists and return its sequence value."""
    if not shop_order or not part_id or not sequence_id:
        raise ValueError('shop_order, part_id, and sequence_id are required')

    shop_order_clean = _clean_string(shop_order)
    part_id_clean = _clean_string(part_id)
    operator_clean = _clean_string(operator_id) or 'Sys'
    equipment_clean = _clean_string(equipment_id) or 'STINGER_01'
    created_by = equipment_clean[:20] or 'STINGER'
    modified_by = equipment_clean[:20] or 'STINGER'
    sequence_write = _resolve_sequence_id_for_write(session, part_id_clean, sequence_id)
    target_text = _clean_string(activation_target) or 'TBD'

    for value, field_name, max_length in (
        (shop_order_clean, 'shop_order', ORDER_CAL_MASTER_LIMITS['shop_order']),
        (part_id_clean, 'part_id', ORDER_CAL_MASTER_LIMITS['part_id']),
        (operator_clean, 'operator_id', ORDER_CAL_MASTER_LIMITS['operator_id']),
        (equipment_clean, 'equipment_id', ORDER_CAL_MASTER_LIMITS['equipment_id']),
        (created_by, 'created_by', ORDER_CAL_MASTER_LIMITS['created_by']),
        (modified_by, 'modified_by', ORDER_CAL_MASTER_LIMITS['modified_by']),
        (target_text, 'activation_target', ORDER_CAL_MASTER_LIMITS['activation_target']),
    ):
        if not _validate_fixed_width(value, field_name=field_name, max_length=max_length):
            raise ValueError(f'{field_name} exceeds database width')

    existing = (
        session.query(OrderCalibrationMaster)
        .filter_by(ShopOrder=shop_order_clean, PartID=part_id_clean)
        .first()
    )
    now = datetime.now()
    template = _template_master_for_insert(session, part_id_clean, sequence_write)
    min_limit, max_limit = _numeric_target_bounds(activation_target, template)
    temperature = (
        _as_float(temperature_c)
        if temperature_c is not None
        else _as_float(getattr(template, 'TemperatureC', None), 25.0)
    )

    if existing:
        existing.LastSequenceCalibrated = sequence_write
        existing.OrderQTY = _parse_order_qty(order_qty)
        existing.OperatorID = operator_clean
        existing.EquipmentID = equipment_clean
        existing.ModificationDate = now
        existing.ModifiedBy = modified_by
        existing.TemperatureC = temperature
        existing.ActivationTarget = target_text
        existing.ActivationMinAllowable = min_limit
        existing.ActivationMaxAllowable = max_limit
        logger.info('Updated work order master %s -> %s/%s', shop_order_clean, part_id_clean, sequence_write)
        return sequence_write

    record = OrderCalibrationMaster(
        ShopOrder=shop_order_clean,
        PartID=part_id_clean,
        WascoDescription=_clean_string(getattr(template, 'WascoDescription', '')),
        LastSequenceCalibrated=sequence_write,
        OrderQTY=_parse_order_qty(order_qty),
        OperatorID=operator_clean,
        EquipmentID=equipment_clean,
        StartTime=now,
        FinishTime=now,
        CalibrationDate=now,
        ModificationDate=now,
        TemperatureC=temperature,
        ActivationTarget=target_text,
        ActivationMaxAllowable=max_limit,
        ActivationMinAllowable=min_limit,
        CreatedBy=created_by,
        CreationDate=now,
        ModifiedBy=modified_by,
    )
    session.add(record)
    logger.info('Inserted work order master %s -> %s/%s', shop_order_clean, part_id_clean, sequence_write)
    return sequence_write


def ensure_work_order_master(
    shop_order: str,
    part_id: str,
    sequence_id: str,
    order_qty: int = 1,
    activation_target: Optional[Any] = None,
    operator_id: str = '',
    equipment_id: str = '',
    temperature_c: Optional[float] = None,
) -> bool:
    """Insert or update a live master row; queue local metadata if SQL is offline."""
    if not shop_order or not part_id or not sequence_id:
        logger.warning('ensure_work_order_master: shop_order, part_id, sequence_id required')
        return False

    try:
        with session_scope() as session:
            sequence_write = _ensure_work_order_master_in_session(
                session,
                shop_order,
                part_id,
                sequence_id,
                order_qty,
                activation_target,
                operator_id,
                equipment_id,
                temperature_c,
            )
            row = (
                session.query(OrderCalibrationMaster)
                .filter_by(ShopOrder=_clean_string(shop_order), PartID=_clean_string(part_id))
                .one_or_none()
            )
            if row is not None:
                local_cache.upsert_master(_row_to_master_details(row), source='sql')
            else:
                local_cache.upsert_master(
                    {
                        'ShopOrder': shop_order,
                        'PartID': part_id,
                        'SequenceID': sequence_write,
                        'OrderQTY': order_qty,
                        'OperatorID': operator_id,
                        'EquipmentID': equipment_id,
                        'TemperatureC': temperature_c,
                        'ActivationTarget': activation_target,
                    },
                    source='sql',
                )
        return True
    except Exception as exc:
        logger.error('Database error ensuring work order master: %s', exc)
        local_cache.upsert_master(
            {
                'ShopOrder': shop_order,
                'PartID': part_id,
                'SequenceID': _sequence_id_default_storage_value(sequence_id),
                'OrderQTY': order_qty,
                'OperatorID': operator_id,
                'EquipmentID': equipment_id,
                'TemperatureC': temperature_c,
                'ActivationTarget': activation_target,
            },
            source='queued',
        )
        return True


def insert_work_order_master(*args: Any, **kwargs: Any) -> bool:
    """Backward-compatible alias for ensure_work_order_master."""
    return ensure_work_order_master(*args, **kwargs)


def get_tested_serials(shop_order: str, part_id: str, sequence_id: str) -> Set[int]:
    """
    Get set of serial numbers already tested for a work order.
    
    Args:
        shop_order: Shop order number.
        part_id: Part ID.
        sequence_id: Sequence ID.
        
    Returns:
        Set of serial numbers that have been tested.
    """
    try:
        with session_scope() as session:
            sequence_values = _sequence_id_lookup_values(sequence_id)
            
            results = session.query(OrderCalibrationDetail.SerialNumber).filter(
                OrderCalibrationDetail.ShopOrder == shop_order.strip(),
                OrderCalibrationDetail.PartID == part_id.strip(),
                OrderCalibrationDetail.SequenceID.in_(sequence_values),
            ).distinct().all()
            
            return {row[0] for row in results}
            
    except SQLAlchemyError as e:
        logger.error(f"Database error getting tested serials: {e}")
        return local_cache.get_tested_serials(shop_order, part_id, sequence_id)
    except Exception as e:
        logger.error(f"Unexpected error getting tested serials: {e}")
        return local_cache.get_tested_serials(shop_order, part_id, sequence_id)


def get_next_serial_number(
    shop_order: str, 
    part_id: str, 
    sequence_id: str,
    in_progress_serials: Set[int] = None,
    start_from: int = 1
) -> int:
    """
    Get the next available serial number for a work order.
    
    Args:
        shop_order: Shop order number.
        part_id: Part ID.
        sequence_id: Sequence ID.
        in_progress_serials: Set of serials currently being tested on other ports.
        start_from: Minimum serial number to consider.
        
    Returns:
        Next available serial number.
    """
    if in_progress_serials is None:
        in_progress_serials = set()
    
    # Get already-tested serials from database
    tested = get_tested_serials(shop_order, part_id, sequence_id)
    
    # Find next available
    serial = start_from
    while serial in tested or serial in in_progress_serials:
        serial += 1
    
    return serial


def _build_detail_payload(
    *,
    shop_order: str,
    part_id: str,
    sequence_id: str,
    serial_number: int,
    activation_id: int,
    increasing_activation: Optional[float],
    decreasing_deactivation: Optional[float],
    in_spec: bool,
    temperature_c: float,
    units_of_measure: str,
    operator_id: str,
    equipment_id: str,
    max_pressure_achieved: Optional[float],
    gage_reference_diff: Optional[float],
    inspection_date: Optional[datetime] = None,
) -> Dict[str, Any]:
    return {
        'ShopOrder': _clean_string(shop_order),
        'SequenceID': _clean_string(sequence_id),
        'PartID': _clean_string(part_id),
        'SerialNumber': int(serial_number),
        'ActivationID': int(activation_id),
        'IncreasingActivation': increasing_activation,
        'DecreasingDeactivation': decreasing_deactivation,
        'TemperatureC': temperature_c,
        'IncreasingGap': 0,
        'DecreasingGap': 0,
        'MaxPressureAchieved': max_pressure_achieved,
        'InSpec': in_spec,
        'UnitsOfMeasure': _clean_string(units_of_measure) or 'PSI',
        'GageReferenceDiff': gage_reference_diff,
        'InspectionDate': inspection_date or datetime.now(),
        'OperatorID': _clean_string(operator_id),
        'EquipmentID': _clean_string(equipment_id),
    }


def _save_detail_payload_to_session(session, detail: Dict[str, Any]) -> str:
    existing = session.query(OrderCalibrationDetail).filter_by(
        ShopOrder=detail['ShopOrder'],
        SequenceID=detail['SequenceID'],
        PartID=detail['PartID'],
        SerialNumber=detail['SerialNumber'],
        ActivationID=detail['ActivationID'],
    ).one_or_none()

    if existing:
        existing.IncreasingActivation = detail['IncreasingActivation']
        existing.DecreasingDeactivation = detail['DecreasingDeactivation']
        existing.TemperatureC = detail['TemperatureC']
        existing.IncreasingGap = detail['IncreasingGap']
        existing.DecreasingGap = detail['DecreasingGap']
        existing.MaxPressureAchieved = detail['MaxPressureAchieved']
        existing.InSpec = detail['InSpec']
        existing.UnitsOfMeasure = detail['UnitsOfMeasure']
        existing.GageReferenceDiff = detail['GageReferenceDiff']
        existing.InspectionDate = detail['InspectionDate']
        existing.OperatorID = detail['OperatorID']
        existing.EquipmentID = detail['EquipmentID']
        return 'Updated'

    session.add(
        OrderCalibrationDetail(
            ShopOrder=detail['ShopOrder'],
            SequenceID=detail['SequenceID'],
            PartID=detail['PartID'],
            SerialNumber=detail['SerialNumber'],
            ActivationID=detail['ActivationID'],
            IncreasingActivation=detail['IncreasingActivation'],
            DecreasingDeactivation=detail['DecreasingDeactivation'],
            TemperatureC=detail['TemperatureC'],
            IncreasingGap=detail['IncreasingGap'],
            DecreasingGap=detail['DecreasingGap'],
            MaxPressureAchieved=detail['MaxPressureAchieved'],
            InSpec=detail['InSpec'],
            UnitsOfMeasure=detail['UnitsOfMeasure'],
            GageReferenceDiff=detail['GageReferenceDiff'],
            InspectionDate=detail['InspectionDate'],
            OperatorID=detail['OperatorID'],
            EquipmentID=detail['EquipmentID'],
        )
    )
    return 'Inserted'


def save_test_result(
    shop_order: str,
    part_id: str,
    sequence_id: str,
    serial_number: int,
    increasing_activation: Optional[float],
    decreasing_deactivation: Optional[float],
    in_spec: bool,
    temperature_c: float,
    units_of_measure: str,
    operator_id: str,
    equipment_id: str,
    master_equipment_id: Optional[str] = None,
    activation_id: int = 1,
    max_pressure_achieved: Optional[float] = None,
    gage_reference_diff: Optional[float] = None,
    order_qty: int = 1,
    activation_target: Optional[Any] = None,
) -> bool:
    """
    Save or update a test result.
    
    Implements UPDATE behavior for retests - updates existing row if present.
    
    Args:
        shop_order: Shop order number.
        part_id: Part ID.
        sequence_id: Sequence ID.
        serial_number: Unit serial number.
        increasing_activation: Measured activation pressure (increasing direction).
        decreasing_deactivation: Measured deactivation pressure (decreasing direction).
        in_spec: True if passed, False if failed.
        temperature_c: Ambient test temperature in Celsius.
        units_of_measure: Units string for display.
        operator_id: Operator who performed the test.
        equipment_id: Detail-row equipment identifier.
        master_equipment_id: Optional shared work-order master EquipmentID.
        activation_id: Attempt identifier (usually 1).
        
    Returns:
        True if save successful.
    """
    action = 'Saved'
    try:
        shop_order_clean = _clean_string(shop_order)
        part_id_clean = _clean_string(part_id)
        operator_id_clean = _clean_string(operator_id)
        equipment_id_clean = _clean_string(equipment_id)
        master_equipment_id_clean = _clean_string(master_equipment_id) or equipment_id_clean
        units_clean = _clean_string(units_of_measure) or 'PSI'
        sequence_values = _sequence_id_lookup_values(sequence_id)

        if not _validate_fixed_width(
            shop_order_clean,
            field_name='shop_order',
            max_length=ORDER_CAL_DETAIL_LIMITS['shop_order'],
        ):
            return False
        if not _validate_fixed_width(
            _sequence_id_default_storage_value(sequence_id),
            field_name='sequence_id',
            max_length=ORDER_CAL_DETAIL_LIMITS['sequence_id'],
        ):
            return False
        if not _validate_fixed_width(
            part_id_clean,
            field_name='part_id',
            max_length=ORDER_CAL_DETAIL_LIMITS['part_id'],
        ):
            return False
        if not _validate_fixed_width(
            operator_id_clean,
            field_name='operator_id',
            max_length=ORDER_CAL_DETAIL_LIMITS['operator_id'],
        ):
            return False
        if not _validate_fixed_width(
            equipment_id_clean,
            field_name='equipment_id',
            max_length=ORDER_CAL_DETAIL_LIMITS['equipment_id'],
        ):
            return False
        if not _validate_fixed_width(
            units_clean,
            field_name='units_of_measure',
            max_length=ORDER_CAL_DETAIL_LIMITS['units_of_measure'],
        ):
            return False

        with session_scope() as session:
            existing_any_sequence = session.query(OrderCalibrationDetail).filter(
                OrderCalibrationDetail.ShopOrder == shop_order_clean,
                OrderCalibrationDetail.PartID == part_id_clean,
                OrderCalibrationDetail.SequenceID.in_(sequence_values),
                OrderCalibrationDetail.SerialNumber == serial_number,
                OrderCalibrationDetail.ActivationID == activation_id,
            ).one_or_none()

            seq_for_write = (
                _clean_string(existing_any_sequence.SequenceID)
                if existing_any_sequence is not None
                else _resolve_sequence_id_for_write(session, part_id_clean, sequence_id)
            )
            if not _validate_fixed_width(
                seq_for_write,
                field_name='sequence_id',
                max_length=ORDER_CAL_DETAIL_LIMITS['sequence_id'],
            ):
                return False

            _ensure_work_order_master_in_session(
                session,
                shop_order_clean,
                part_id_clean,
                seq_for_write,
                order_qty,
                activation_target,
                operator_id_clean,
                master_equipment_id_clean,
                temperature_c,
            )
            detail = _build_detail_payload(
                shop_order=shop_order_clean,
                part_id=part_id_clean,
                sequence_id=seq_for_write,
                serial_number=serial_number,
                activation_id=activation_id,
                increasing_activation=increasing_activation,
                decreasing_deactivation=decreasing_deactivation,
                in_spec=in_spec,
                temperature_c=temperature_c,
                units_of_measure=units_clean,
                operator_id=operator_id_clean,
                equipment_id=equipment_id_clean,
                max_pressure_achieved=max_pressure_achieved,
                gage_reference_diff=gage_reference_diff,
            )
            action = _save_detail_payload_to_session(session, detail)

        local_cache.upsert_master(
            {
                'ShopOrder': shop_order_clean,
                'PartID': part_id_clean,
                'SequenceID': detail['SequenceID'],
                'OrderQTY': order_qty,
                'OperatorID': operator_id_clean,
                'EquipmentID': master_equipment_id_clean,
                'TemperatureC': temperature_c,
                'ActivationTarget': activation_target,
            },
            source='sql',
        )
        local_cache.upsert_detail(detail, sync_status=local_cache.SYNC_SYNCED)
        logger.info(
            '%s test result: SN=%s, InSpec=%s, MaxPressureAchieved=%s, GageReferenceDiff=%s',
            action,
            serial_number,
            in_spec,
            max_pressure_achieved,
            gage_reference_diff,
        )
        return True
            
    except SQLAlchemyError as e:
        logger.error(f"Database error saving test result: {e}")
        return _queue_local_test_result(
            shop_order=shop_order,
            part_id=part_id,
            sequence_id=sequence_id,
            serial_number=serial_number,
            activation_id=activation_id,
            increasing_activation=increasing_activation,
            decreasing_deactivation=decreasing_deactivation,
            in_spec=in_spec,
            temperature_c=temperature_c,
            units_of_measure=units_of_measure,
            operator_id=operator_id,
            equipment_id=equipment_id,
            master_equipment_id=master_equipment_id,
            max_pressure_achieved=max_pressure_achieved,
            gage_reference_diff=gage_reference_diff,
            order_qty=order_qty,
            activation_target=activation_target,
        )
    except Exception as e:
        logger.error(f"Unexpected error saving test result: {e}")
        return _queue_local_test_result(
            shop_order=shop_order,
            part_id=part_id,
            sequence_id=sequence_id,
            serial_number=serial_number,
            activation_id=activation_id,
            increasing_activation=increasing_activation,
            decreasing_deactivation=decreasing_deactivation,
            in_spec=in_spec,
            temperature_c=temperature_c,
            units_of_measure=units_of_measure,
            operator_id=operator_id,
            equipment_id=equipment_id,
            master_equipment_id=master_equipment_id,
            max_pressure_achieved=max_pressure_achieved,
            gage_reference_diff=gage_reference_diff,
            order_qty=order_qty,
            activation_target=activation_target,
        )


def _queue_local_test_result(
    *,
    shop_order: str,
    part_id: str,
    sequence_id: str,
    serial_number: int,
    activation_id: int,
    increasing_activation: Optional[float],
    decreasing_deactivation: Optional[float],
    in_spec: bool,
    temperature_c: float,
    units_of_measure: str,
    operator_id: str,
    equipment_id: str,
    master_equipment_id: Optional[str],
    max_pressure_achieved: Optional[float],
    gage_reference_diff: Optional[float],
    order_qty: int,
    activation_target: Optional[Any],
) -> bool:
    """Persist a result to the local queue when SQL Server is unavailable."""
    try:
        sequence_write = _sequence_id_default_storage_value(sequence_id)
        detail = _build_detail_payload(
            shop_order=shop_order,
            part_id=part_id,
            sequence_id=sequence_write,
            serial_number=serial_number,
            activation_id=activation_id,
            increasing_activation=increasing_activation,
            decreasing_deactivation=decreasing_deactivation,
            in_spec=in_spec,
            temperature_c=temperature_c,
            units_of_measure=units_of_measure,
            operator_id=operator_id,
            equipment_id=equipment_id,
            max_pressure_achieved=max_pressure_achieved,
            gage_reference_diff=gage_reference_diff,
        )
        local_cache.upsert_master(
            {
                'ShopOrder': shop_order,
                'PartID': part_id,
                'SequenceID': sequence_write,
                'OrderQTY': order_qty,
                'OperatorID': operator_id,
                'EquipmentID': _clean_string(master_equipment_id) or equipment_id,
                'TemperatureC': temperature_c,
                'ActivationTarget': activation_target,
            },
            source='queued',
        )
        local_cache.upsert_detail(detail, sync_status=local_cache.SYNC_QUEUED)
        logger.warning(
            'Queued test result locally: SO=%s Part=%s Seq=%s SN=%s ActID=%s',
            detail['ShopOrder'],
            detail['PartID'],
            detail['SequenceID'],
            detail['SerialNumber'],
            detail['ActivationID'],
        )
        return True
    except Exception as exc:
        logger.error('Failed to queue test result locally: %s', exc)
        return False


def get_work_order_progress(shop_order: str, part_id: str, sequence_id: str) -> Dict[str, int]:
    """
    Get progress for a work order.
    
    Args:
        shop_order: Shop order number.
        part_id: Part ID.
        sequence_id: Sequence ID.
        
    Returns:
        Dictionary with 'completed', 'passed', 'failed' counts.
    """
    try:
        with session_scope() as session:
            sequence_values = _sequence_id_lookup_values(sequence_id)
            shop_order_clean = shop_order.strip()
            part_id_clean = part_id.strip()
            
            # Use window function to get the latest ActivationID per serial number
            # This is much more efficient than fetching all rows and grouping in Python
            # Subquery: get max ActivationID for each SerialNumber
            latest_per_serial = session.query(
                OrderCalibrationDetail.SerialNumber,
                func.max(OrderCalibrationDetail.ActivationID).label('max_activation_id')
            ).filter(
                OrderCalibrationDetail.ShopOrder == shop_order_clean,
                OrderCalibrationDetail.PartID == part_id_clean,
                OrderCalibrationDetail.SequenceID.in_(sequence_values),
            ).group_by(
                OrderCalibrationDetail.SerialNumber
            ).subquery()
            
            # Join to get the InSpec status for the latest attempt of each serial
            latest_results = session.query(
                OrderCalibrationDetail.InSpec
            ).join(
                latest_per_serial,
                (OrderCalibrationDetail.SerialNumber == latest_per_serial.c.SerialNumber) &
                (OrderCalibrationDetail.ActivationID == latest_per_serial.c.max_activation_id)
            ).filter(
                OrderCalibrationDetail.ShopOrder == shop_order_clean,
                OrderCalibrationDetail.PartID == part_id_clean,
                OrderCalibrationDetail.SequenceID.in_(sequence_values),
            ).all()
            
            # Count results
            completed = len(latest_results)
            passed = sum(1 for r in latest_results if r.InSpec)
            failed = completed - passed
            
            return {
                'completed': completed,
                'passed': passed,
                'failed': failed,
            }
            
    except SQLAlchemyError as e:
        logger.error(f"Database error getting progress: {e}")
        return local_cache.get_progress(shop_order, part_id, sequence_id)
    except Exception as e:
        logger.error(f"Unexpected error getting progress: {e}")
        return local_cache.get_progress(shop_order, part_id, sequence_id)


def get_local_queue_count() -> int:
    """Return queued/error local result rows waiting for sync."""
    try:
        return local_cache.queued_count()
    except Exception as exc:
        logger.error('Failed reading local queue count: %s', exc)
        return 0


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if value:
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            pass
    return datetime.now()


def _details_equivalent(remote: OrderCalibrationDetail, detail: Dict[str, Any]) -> bool:
    """Return True when a remote row already represents the queued local row."""
    if _clean_string(remote.EquipmentID) != _clean_string(detail.get('EquipmentID')):
        return False
    comparisons = (
        ('IncreasingActivation', remote.IncreasingActivation),
        ('DecreasingDeactivation', remote.DecreasingDeactivation),
        ('MaxPressureAchieved', remote.MaxPressureAchieved),
        ('GageReferenceDiff', remote.GageReferenceDiff),
    )
    for field, remote_value in comparisons:
        local_value = _as_optional_float(detail.get(field))
        remote_float = _as_optional_float(remote_value)
        if local_value is None and remote_float is None:
            continue
        if local_value is None or remote_float is None:
            return False
        if abs(local_value - remote_float) > 0.001:
            return False
    return bool(remote.InSpec) == bool(detail.get('InSpec'))


def sync_local_cache(max_rows: int = 100) -> Dict[str, int]:
    """Upload queued local detail rows to SQL Server."""
    queued = local_cache.list_queued_details(limit=max_rows)
    result = {
        'queued': len(queued),
        'synced': 0,
        'conflicts': 0,
        'errors': 0,
    }
    if not queued:
        return result

    try:
        for local_detail in queued:
            detail = local_cache.normalize_detail(local_detail)
            try:
                with session_scope() as session:
                    sequence_values = _sequence_id_lookup_values(detail['SequenceID'])
                    remote_any_sequence = (
                        session.query(OrderCalibrationDetail)
                        .filter(
                            OrderCalibrationDetail.ShopOrder == detail['ShopOrder'],
                            OrderCalibrationDetail.PartID == detail['PartID'],
                            OrderCalibrationDetail.SequenceID.in_(sequence_values),
                            OrderCalibrationDetail.SerialNumber == detail['SerialNumber'],
                            OrderCalibrationDetail.ActivationID == detail['ActivationID'],
                        )
                        .one_or_none()
                    )

                    if remote_any_sequence is not None:
                        remote_equipment = _clean_string(remote_any_sequence.EquipmentID)
                        sequence_differs = (
                            _clean_string(remote_any_sequence.SequenceID) != detail['SequenceID']
                        )
                        equipment_differs = remote_equipment != detail['EquipmentID']
                        if sequence_differs or equipment_differs:
                            local_cache.mark_detail_status(
                                detail,
                                local_cache.SYNC_CONFLICT,
                                (
                                    'Remote row exists for this unit/attempt with '
                                    f'sequence={_clean_string(remote_any_sequence.SequenceID)!r} '
                                    f'equipment={remote_equipment!r}'
                                ),
                            )
                            result['conflicts'] += 1
                            continue

                    master = local_cache.get_master(detail['ShopOrder']) or {}
                    _ensure_work_order_master_in_session(
                        session,
                        detail['ShopOrder'],
                        detail['PartID'],
                        detail['SequenceID'],
                        _parse_order_qty(master.get('OrderQTY') or master.get('OrderQty') or 1),
                        master.get('ActivationTarget'),
                        detail.get('OperatorID') or master.get('OperatorID') or '',
                        master.get('EquipmentID') or detail.get('EquipmentID') or '',
                        _as_optional_float(detail.get('TemperatureC')),
                    )
                    detail['InspectionDate'] = _parse_datetime(detail.get('InspectionDate'))
                    _save_detail_payload_to_session(session, detail)
                local_cache.mark_detail_status(detail, local_cache.SYNC_SYNCED)
                result['synced'] += 1
            except Exception as exc:
                logger.error(
                    'Failed syncing local result %s/%s/%s SN=%s ActID=%s: %s',
                    local_detail.get('ShopOrder'),
                    local_detail.get('PartID'),
                    local_detail.get('SequenceID'),
                    local_detail.get('SerialNumber'),
                    local_detail.get('ActivationID'),
                    exc,
                )
                local_cache.mark_detail_status(detail, local_cache.SYNC_ERROR, str(exc)[:500])
                result['errors'] += 1
    except Exception as exc:
        logger.error('Local cache sync could not connect to SQL Server: %s', exc)
        result['errors'] += len(queued)

    return result
