# Quality Calibration — manual checklist

Run from repo root:

```powershell
.\.venv\Scripts\python.exe run_quality_cal.py
```

## Session flow

1. **Setup** — Default profile **CAL 10 WCS02075** (or dense 0–115 / Mensor 0–30), enter technician ID/asset, **Refresh Hardware**, **Begin Session**.
2. **Left port calibration** — **Start Run**; wait for full sweep. Review fit dialog; **Apply** to write models to `stinger_config.yaml` on this PC.
3. **Move Mensor** — Physically move reference to right port; confirm.
4. **Right port calibration** — Repeat sweep and apply.
5. **Report** — Export PDF/CSV as needed.

## Profiles

| Profile | Setpoints | Mensor |
|---------|-----------|--------|
| **CAL 10 WCS02075** (default) | Per CAL 10 Rev 000 §5.3.2: 115, 75, 25, 15, 10, 5, 1, 0.5, 0.2, 0.05 PSIA (high→low) | Mensor for points ≤30 PSIA |
| Mensor 0–30 PSIA | 0–30 @ 1 PSI | All points |
| Dense 0–115 PSIA | 0–30 @ 1 PSI, 35–115 @ 5 PSI | Disconnect prompt above 30 PSIA |

Work instruction (manual procedure): `I:\Level 4 Work Instructions\CAL 10 WCS02075 Calibration Rev 000.pdf`  
Quality Cal automates the same setpoints on **left then right** port with solenoid vacuum/atmosphere control.

## After apply

Launch Stinger (`python run.py`) and verify corrected transducer (0–20 PSIA) and corrected Alicat readings on both ports.

Sweep CSV logs: `logs/quality_cal_sweep_port_*_*.csv`

### Offset history (versioned models)

Each finalize (applied or not) appends an immutable archive under `logs/offset_history/`:

| Path | Contents |
|------|----------|
| `entries/{YYYYMMDD_HHMMSS}_{port}.json` | Full record: previous + new `*_error_model`, fit p99s, sweep path, profile |
| `index.jsonl` | One line per event (timestamp, port, applied, p99s, entry path) |
| `latest_{port}.json` | Most recent entry for that port |

`stinger_config.yaml` remains the live source of truth. Use the history folder to audit how offsets drifted over time (no UI browser yet).
