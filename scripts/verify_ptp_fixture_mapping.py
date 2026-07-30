"""Gate a stand release on live PTP switch-fixture observability.

This is a software mapping audit. It proves that this stand's configured LabJack
inputs and drive output can be resolved for each live PTP application; it does
not replace a hardware transition check on an installed switch.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import load_config
from app.core.paths import get_logs_dir
from app.database.session import close_database, initialize_database
from app.services.ptp_fixture_verifier import (
    TOP_LEVEL_SEQUENCE,
    discover_sps_applications,
    discover_top_level_applications,
    has_blocked_verdict,
    verify_application_fixture,
)
from app.services.ptp_service import load_ptp_from_db

HEADERS = (
    'part_id',
    'sequence_id',
    'port_id',
    'status',
    'category',
    'message',
    'ptp_source',
    'derivation_mode',
    'drive_role',
    'drive_dio',
    'no_dio',
    'nc_dio',
    'sensed_db9_pins',
    'warnings',
)


def parse_application(value: str) -> tuple[str, str]:
    """Parse a PART:SEQUENCE argument."""
    if ':' not in value:
        raise argparse.ArgumentTypeError('Use PART:SEQUENCE, for example SPS01496-02:300')
    part_id, sequence_id = (piece.strip() for piece in value.split(':', 1))
    if not part_id or not sequence_id:
        raise argparse.ArgumentTypeError('Both part and sequence are required')
    return part_id, sequence_id


def write_report(path: Path, rows: list[dict[str, str]]) -> None:
    """Write the per-port live-verifier report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADERS, lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--all-sps', action='store_true', help='Audit SPS sequences 300 and 600.')
    parser.add_argument(
        '--all-top-level',
        action='store_true',
        help=f'Audit five-digit 17xxx applications at sequence {TOP_LEVEL_SEQUENCE}.',
    )
    parser.add_argument(
        '--application',
        action='append',
        type=parse_application,
        help='Audit one live PTP application as PART:SEQUENCE. May be repeated.',
    )
    parser.add_argument(
        '--output',
        type=Path,
        default=get_logs_dir() / 'ptp_fixture_mapping.csv',
        help='CSV report path.',
    )
    parser.add_argument(
        '--allow-blocked',
        action='store_true',
        help='Report blocked/missing mappings without failing the command.',
    )
    args = parser.parse_args(argv)
    if not (args.all_sps or args.all_top_level or args.application):
        parser.error('Select --all-sps, --all-top-level, or --application.')

    config = load_config()
    if not initialize_database(config.get('database', {})):
        parser.error('Live PTP fixture verification requires a database connection.')

    try:
        applications = set(args.application or [])
        if args.all_sps:
            applications.update(discover_sps_applications())
        if args.all_top_level:
            applications.update(discover_top_level_applications())

        rows: list[dict[str, str]] = []
        for part_id, sequence_id in sorted(applications):
            params = load_ptp_from_db(part_id, sequence_id)
            rows.extend(
                verify_application_fixture(
                    part_id=part_id,
                    sequence_id=sequence_id,
                    ptp_params=params,
                    config=config,
                )
            )
        write_report(args.output, rows)
    finally:
        close_database()

    counts = Counter(row['status'] for row in rows)
    print(f'Verified {len(rows) // 2} applications ({len(rows)} port mappings) -> {args.output}')
    print(', '.join(f'{status}={count}' for status, count in sorted(counts.items())))
    if has_blocked_verdict(rows):
        blocked = [
            f"{row['part_id']}/{row['sequence_id']} {row['port_id']}: {row['message']}"
            for row in rows
            if row['status'].startswith('BLOCKED') or row['status'] == 'MISSING_PTP'
        ]
        print('Blocked mappings:')
        for item in blocked[:20]:
            print(f'  {item}')
        return 0 if args.allow_blocked else 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
