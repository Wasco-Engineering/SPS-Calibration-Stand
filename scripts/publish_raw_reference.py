"""Publish an existing Quality Cal sweep CSV into the latest raw-reference slot.

Usage:
  python scripts/publish_raw_reference.py --port port_b --csv path/to/sweep.csv
  python scripts/publish_raw_reference.py --port port_b --latest-dated
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quality_cal.core.raw_reference_store import (
    get_quality_cal_logs_dir,
    publish_latest_raw_reference,
)


def _latest_dated_sweep(port_id: str) -> Path:
    log_dir = get_quality_cal_logs_dir()
    matches = sorted(log_dir.glob(f'quality_cal_sweep_{port_id}_*.csv'), key=lambda p: p.stat().st_mtime)
    if not matches:
        raise FileNotFoundError(f'No dated sweep CSVs for {port_id} under {log_dir}')
    return matches[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True, choices=('port_a', 'port_b'))
    parser.add_argument('--csv', type=Path, default=None, help='Sweep CSV to publish')
    parser.add_argument(
        '--latest-dated',
        action='store_true',
        help='Use the newest quality_cal_sweep_<port>_*.csv in the QC logs dir',
    )
    parser.add_argument('--profile-id', default=None)
    args = parser.parse_args()

    if args.csv is None and not args.latest_dated:
        parser.error('Provide --csv or --latest-dated')
    source = args.csv if args.csv is not None else _latest_dated_sweep(args.port)
    manifest = publish_latest_raw_reference(
        source,
        port_id=args.port,
        profile_id=args.profile_id,
    )
    print(f"Published {source}")
    print(f"Latest CSV: {manifest['latest_csv']}")
    print(
        f"rows={manifest['row_count']} mensor={manifest['has_mensor']} "
        f"targets={manifest['static_targets_psia']}"
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
