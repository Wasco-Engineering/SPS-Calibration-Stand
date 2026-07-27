"""Inspect and optionally repair Stinger rows that are hidden from legacy reports.

Dry-run is the default. Apply mode only:
- creates missing OrderCalibrationMaster rows via the app's ensure function
- normalizes mixed SequenceID rows when the target key does not already exist

It never fabricates historical MaxPressureAchieved or GageReferenceDiff values.
"""
from __future__ import annotations

import argparse
import logging
from typing import Any, Iterable

from sqlalchemy import text

from app.core.config import load_config, setup_logging
from app.database.operations import (
    _create_mssql_engine,
    ensure_work_order_master,
)

logger = logging.getLogger(__name__)


def _clean(value: Any) -> str:
    return '' if value is None else str(value).strip()


def _row_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _sequence_values(sequence_id: str) -> list[str]:
    raw = _clean(sequence_id)
    values = [raw] if raw else []
    try:
        number = int(raw)
    except (TypeError, ValueError):
        candidates = [raw]
    else:
        candidates = [str(number), f'{number:04d}']
    for candidate in candidates:
        if candidate and candidate not in values:
            values.append(candidate)
    return values


def _where_shop_order(shop_order: str | None) -> str:
    return "AND LTRIM(RTRIM(d.ShopOrder)) = :shop_order" if shop_order else ""


def inspect_rows(engine, *, equipment_id: str, shop_order: str | None = None) -> dict[str, list[dict[str, Any]]]:
    params = {'equipment_id': equipment_id}
    if shop_order:
        params['shop_order'] = shop_order

    with engine.connect() as conn:
        missing_master = conn.execute(
            text(
                f"""
                SELECT
                    LTRIM(RTRIM(d.ShopOrder)) AS ShopOrder,
                    LTRIM(RTRIM(d.PartID)) AS PartID,
                    LTRIM(RTRIM(d.SequenceID)) AS SequenceID,
                    COUNT(*) AS DetailRows,
                    MAX(d.InspectionDate) AS LastInspection
                FROM dbo.OrderCalibrationDetail d
                LEFT JOIN dbo.OrderCalibrationMaster m
                  ON LTRIM(RTRIM(m.ShopOrder)) = LTRIM(RTRIM(d.ShopOrder))
                 AND LTRIM(RTRIM(m.PartID)) = LTRIM(RTRIM(d.PartID))
                WHERE LTRIM(RTRIM(d.EquipmentID)) = :equipment_id
                  {_where_shop_order(shop_order)}
                  AND m.ShopOrder IS NULL
                GROUP BY d.ShopOrder, d.PartID, d.SequenceID
                ORDER BY LastInspection DESC
                """
            ),
            params,
        ).mappings().all()

        mixed_sequences = conn.execute(
            text(
                f"""
                SELECT
                    LTRIM(RTRIM(d.ShopOrder)) AS ShopOrder,
                    LTRIM(RTRIM(d.PartID)) AS PartID,
                    COUNT(DISTINCT LTRIM(RTRIM(d.SequenceID))) AS SequenceCount,
                    COUNT(*) AS DetailRows,
                    MAX(d.InspectionDate) AS LastInspection
                FROM dbo.OrderCalibrationDetail d
                WHERE LTRIM(RTRIM(d.EquipmentID)) = :equipment_id
                  {_where_shop_order(shop_order)}
                GROUP BY d.ShopOrder, d.PartID
                HAVING COUNT(DISTINCT LTRIM(RTRIM(d.SequenceID))) > 1
                ORDER BY LastInspection DESC
                """
            ),
            params,
        ).mappings().all()

        null_report_fields = conn.execute(
            text(
                f"""
                SELECT
                    LTRIM(RTRIM(d.ShopOrder)) AS ShopOrder,
                    LTRIM(RTRIM(d.PartID)) AS PartID,
                    LTRIM(RTRIM(d.SequenceID)) AS SequenceID,
                    COUNT(*) AS DetailRows,
                    SUM(CASE WHEN d.MaxPressureAchieved IS NULL THEN 1 ELSE 0 END) AS MaxPressureNulls,
                    SUM(CASE WHEN d.GageReferenceDiff IS NULL THEN 1 ELSE 0 END) AS GageReferenceNulls,
                    MAX(d.InspectionDate) AS LastInspection
                FROM dbo.OrderCalibrationDetail d
                WHERE LTRIM(RTRIM(d.EquipmentID)) = :equipment_id
                  {_where_shop_order(shop_order)}
                GROUP BY d.ShopOrder, d.PartID, d.SequenceID
                HAVING
                    SUM(CASE WHEN d.MaxPressureAchieved IS NULL THEN 1 ELSE 0 END) > 0
                    OR SUM(CASE WHEN d.GageReferenceDiff IS NULL THEN 1 ELSE 0 END) > 0
                ORDER BY LastInspection DESC
                """
            ),
            params,
        ).mappings().all()

    return {
        'missing_master': [dict(row) for row in missing_master],
        'mixed_sequences': [dict(row) for row in mixed_sequences],
        'null_report_fields': [dict(row) for row in null_report_fields],
    }


def _canonical_sequence(conn, shop_order: str, part_id: str, observed_sequences: Iterable[str]) -> str:
    master = conn.execute(
        text(
            """
            SELECT TOP 1 LTRIM(RTRIM(LastSequenceCalibrated)) AS SequenceID
            FROM dbo.OrderCalibrationMaster
            WHERE LTRIM(RTRIM(ShopOrder)) = :shop_order
              AND LTRIM(RTRIM(PartID)) = :part_id
            """
        ),
        {'shop_order': shop_order, 'part_id': part_id},
    ).mappings().first()
    if master and _clean(master['SequenceID']):
        return _clean(master['SequenceID'])

    counts = conn.execute(
        text(
            """
            SELECT
                LTRIM(RTRIM(SequenceID)) AS SequenceID,
                COUNT(*) AS RowCount,
                MAX(InspectionDate) AS LastInspection
            FROM dbo.OrderCalibrationDetail
            WHERE LTRIM(RTRIM(ShopOrder)) = :shop_order
              AND LTRIM(RTRIM(PartID)) = :part_id
            GROUP BY SequenceID
            ORDER BY RowCount DESC, LastInspection DESC
            """
        ),
        {'shop_order': shop_order, 'part_id': part_id},
    ).mappings().first()
    if counts and _clean(counts['SequenceID']):
        return _clean(counts['SequenceID'])
    return next(iter(observed_sequences))


def apply_safe_repairs(engine, report: dict[str, list[dict[str, Any]]], *, equipment_id: str) -> dict[str, int]:
    applied = {
        'masters_created': 0,
        'sequence_rows_updated': 0,
        'sequence_rows_skipped_conflict': 0,
    }

    for row in report['missing_master']:
        if ensure_work_order_master(
            shop_order=_clean(row['ShopOrder']),
            part_id=_clean(row['PartID']),
            sequence_id=_clean(row['SequenceID']),
            order_qty=int(row.get('DetailRows') or 1),
            equipment_id=equipment_id,
        ):
            applied['masters_created'] += 1

    with engine.begin() as conn:
        for group in report['mixed_sequences']:
            shop_order = _clean(group['ShopOrder'])
            part_id = _clean(group['PartID'])
            sequence_rows = conn.execute(
                text(
                    """
                    SELECT DISTINCT LTRIM(RTRIM(SequenceID)) AS SequenceID
                    FROM dbo.OrderCalibrationDetail
                    WHERE LTRIM(RTRIM(ShopOrder)) = :shop_order
                      AND LTRIM(RTRIM(PartID)) = :part_id
                      AND LTRIM(RTRIM(EquipmentID)) = :equipment_id
                    """
                ),
                {'shop_order': shop_order, 'part_id': part_id, 'equipment_id': equipment_id},
            ).mappings().all()
            observed = [_clean(row['SequenceID']) for row in sequence_rows if _clean(row['SequenceID'])]
            canonical = _canonical_sequence(conn, shop_order, part_id, observed)
            for sequence_id in observed:
                if sequence_id == canonical:
                    continue
                for alternate in _sequence_values(sequence_id):
                    if alternate == canonical:
                        continue
                rows = conn.execute(
                    text(
                        """
                        SELECT
                            SerialNumber,
                            ActivationID
                        FROM dbo.OrderCalibrationDetail
                        WHERE LTRIM(RTRIM(ShopOrder)) = :shop_order
                          AND LTRIM(RTRIM(PartID)) = :part_id
                          AND LTRIM(RTRIM(SequenceID)) = :sequence_id
                          AND LTRIM(RTRIM(EquipmentID)) = :equipment_id
                        """
                    ),
                    {
                        'shop_order': shop_order,
                        'part_id': part_id,
                        'sequence_id': sequence_id,
                        'equipment_id': equipment_id,
                    },
                ).mappings().all()
                for detail in rows:
                    conflict = conn.execute(
                        text(
                            """
                            SELECT 1
                            FROM dbo.OrderCalibrationDetail
                            WHERE LTRIM(RTRIM(ShopOrder)) = :shop_order
                              AND LTRIM(RTRIM(PartID)) = :part_id
                              AND LTRIM(RTRIM(SequenceID)) = :canonical
                              AND SerialNumber = :serial_number
                              AND ActivationID = :activation_id
                            """
                        ),
                        {
                            'shop_order': shop_order,
                            'part_id': part_id,
                            'canonical': canonical,
                            'serial_number': detail['SerialNumber'],
                            'activation_id': detail['ActivationID'],
                        },
                    ).first()
                    if conflict:
                        applied['sequence_rows_skipped_conflict'] += 1
                        continue
                    result = conn.execute(
                        text(
                            """
                            UPDATE dbo.OrderCalibrationDetail
                            SET SequenceID = :canonical
                            WHERE LTRIM(RTRIM(ShopOrder)) = :shop_order
                              AND LTRIM(RTRIM(PartID)) = :part_id
                              AND LTRIM(RTRIM(SequenceID)) = :sequence_id
                              AND SerialNumber = :serial_number
                              AND ActivationID = :activation_id
                              AND LTRIM(RTRIM(EquipmentID)) = :equipment_id
                            """
                        ),
                        {
                            'canonical': canonical,
                            'shop_order': shop_order,
                            'part_id': part_id,
                            'sequence_id': sequence_id,
                            'serial_number': detail['SerialNumber'],
                            'activation_id': detail['ActivationID'],
                            'equipment_id': equipment_id,
                        },
                    )
                    applied['sequence_rows_updated'] += int(result.rowcount or 0)

    return applied


def print_report(report: dict[str, list[dict[str, Any]]]) -> None:
    for section, rows in report.items():
        print(f'\n{section}: {len(rows)}')
        for row in rows:
            print('  ' + ', '.join(f'{key}={_clean(value)}' for key, value in row.items()))


def main() -> int:
    parser = argparse.ArgumentParser(description='Inspect/repair STINGER report visibility rows.')
    parser.add_argument('--equipment-id', default='CA-SPS-01')
    parser.add_argument('--shop-order')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()

    config = load_config()
    setup_logging(config)
    engine = _create_mssql_engine(dict(config.get('database') or {}))
    try:
        report = inspect_rows(engine, equipment_id=args.equipment_id, shop_order=args.shop_order)
        print_report(report)
        if args.apply:
            applied = apply_safe_repairs(engine, report, equipment_id=args.equipment_id)
            print('\napplied:')
            for key, value in applied.items():
                print(f'  {key}={value}')
        else:
            print('\ndry-run only; pass --apply to create missing masters and safe sequence repairs')
    finally:
        engine.dispose()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
