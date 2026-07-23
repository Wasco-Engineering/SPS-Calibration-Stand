"""Repeat headless SPS01438-02/300 runs and report mean/stdev of precision edges."""

from __future__ import annotations

import logging
import math
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import load_config, setup_logging
from app.services.ptp_service import convert_pressure
from scripts.run_executor_headless import run_headless_executor


def _mmhg(psi: float) -> float:
    return float(convert_pressure(psi, 'PSI', 'mmHg @ 0 C'))


def _stats(values: list[float]) -> dict[str, float]:
    return {
        'n': float(len(values)),
        'mean': statistics.mean(values),
        'stdev': statistics.stdev(values) if len(values) > 1 else 0.0,
        'min': min(values),
        'max': max(values),
    }


def _edges_from_summary(summary_path: Path) -> tuple[float | None, float | None]:
    import json

    data = json.loads(summary_path.read_text(encoding='utf-8'))
    for event in reversed(data.get('events') or []):
        if event.get('event') != 'edges_captured':
            continue
        payload = event.get('payload') or {}
        act = payload.get('activation_psi')
        deact = payload.get('deactivation_psi')
        if act is None:
            continue
        return float(act), float(deact) if deact is not None else None
    return None, None


def main() -> int:
    part = 'SPS01438-02'
    sequence = '300'
    repeats = 10
    num_cycles = 3
    ports = ('port_a', 'port_b')

    logging.basicConfig(
        level=logging.WARNING,
        format='%(asctime)s %(levelname)s %(name)s - %(message)s',
    )
    # Keep executor INFO so we can see lock/edges if needed; quiet root a bit.
    logging.getLogger('app').setLevel(logging.INFO)
    logging.getLogger('transitions').setLevel(logging.WARNING)

    config = load_config()
    setup_logging(config)
    config.setdefault('control', {}).setdefault('cycling', {})['num_cycles'] = num_cycles

    out_dir = Path('logs/headless_runs/stats_sps01438_300')
    out_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, list[tuple[float, float]]] = {p: [] for p in ports}
    failures: list[str] = []

    for port_id in ports:
        print(f'\n=== {port_id}: {repeats} runs x {num_cycles} cycles ===', flush=True)
        for i in range(1, repeats + 1):
            code = run_headless_executor(
                config=config,
                part=part,
                sequence=sequence,
                port_id=port_id,
                sample_interval_ms=20,
                alicat_refresh_interval_ms=40,
                max_duration_s=240.0,
                out_dir=str(out_dir),
                cycles_only=False,
            )
            # Newest summary for this port
            summaries = sorted(
                out_dir.glob(f'headless_{part}_{sequence}_{port_id}_*.json'),
                key=lambda p: p.stat().st_mtime,
            )
            if not summaries:
                failures.append(f'{port_id} run {i}: no summary')
                print(f'  [{i}/{repeats}] FAIL no summary (code={code})', flush=True)
                continue
            act, deact = _edges_from_summary(summaries[-1])
            if act is None or deact is None or code != 0:
                failures.append(f'{port_id} run {i}: act={act} deact={deact} code={code}')
                print(
                    f'  [{i}/{repeats}] FAIL act={act} deact={deact} code={code}',
                    flush=True,
                )
                continue
            results[port_id].append((act, deact))
            print(
                f'  [{i}/{repeats}] act={_mmhg(act):.2f} mmHg  deact={_mmhg(deact):.2f} mmHg',
                flush=True,
            )

    band_lo, band_hi = 73.0, 77.0
    print('\n' + '=' * 70)
    print(f'SPS01438-02 / 300  ({repeats} runs x {num_cycles} cycles, precision edges)')
    print(f'Activation band: {band_lo:.0f}–{band_hi:.0f} mmHg @ 0 C')
    print('=' * 70)

    exit_code = 0 if not failures else 1
    for port_id in ports:
        rows = results[port_id]
        if not rows:
            print(f'\n{port_id}: NO SUCCESSFUL RUNS')
            exit_code = 1
            continue
        acts = [_mmhg(a) for a, _ in rows]
        deacts = [_mmhg(d) for _, d in rows]
        as_ = _stats(acts)
        ds_ = _stats(deacts)
        in_band = sum(1 for v in acts if band_lo <= v <= band_hi)
        print(f'\n{port_id}  n={len(rows)}  in-band activation={in_band}/{len(rows)}')
        print(
            f'  activation   mean={as_["mean"]:.2f}  stdev={as_["stdev"]:.2f}  '
            f'min={as_["min"]:.2f}  max={as_["max"]:.2f}  mmHg'
        )
        print(
            f'  deactivation mean={ds_["mean"]:.2f}  stdev={ds_["stdev"]:.2f}  '
            f'min={ds_["min"]:.2f}  max={ds_["max"]:.2f}  mmHg'
        )
        if as_['stdev'] > 2.0 or in_band < len(rows):
            exit_code = 1

    if failures:
        print('\nFailures:')
        for line in failures:
            print(f'  - {line}')

    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
