# Stinger (Scorpion Calibration Stand)

Stinger is a **dual-port pressure/vacuum switch test stand** used in a **cleanroom** with **high purity N₂** to test the Scorpion product line.

Each port runs independently with its own control + measurement hardware, and operator workflows are driven by **Shop Order context** and **database test parameters**.

## Quick Start

1. Review **`docs/INITIAL_SETUP.md`** for new PC / stand bring-up
2. Review documentation in `docs/README.md`
3. Configure hardware channels in `configs/<hostname>/stinger_config.yaml` (see `configs/README.md`)
4. Confirm open questions in `docs/OPEN_QUESTIONS.md`

## Documentation

All specs live in `docs/`:

| Document | Purpose |
|----------|---------|
| `docs/README.md` | Documentation index |
| `docs/INITIAL_SETUP.md` | New PC / stand bring-up and hardware verification |
| `docs/SYSTEM_SPEC.md` | Authoritative system description |
| `docs/WORKFLOWS.md` | QAL 15/16/17 operator workflows |
| `docs/UI_SPEC.md` | Touch-first UI design |
| `docs/HARDWARE_SPEC.md` | Hardware topology + channels |
| `docs/DATABASE_CONTRACT.md` | DB read/write contract |
| `docs/STATE_MACHINE.md` | Per-port state machine |
| `docs/TESTING.md` | Unit, coverage, and hardware test commands |
| `docs/COVERAGE_BASELINE.md` | Latest coverage snapshot and priorities |
| `docs/OPEN_QUESTIONS.md` | Remaining unknowns |

## Configuration

Per-computer YAML lives under `configs/<hostname>/` (`stinger_config.yaml` and
`quality_cal_config.yaml`). Runtime selects the folder from this PC's hostname
(see `app.core.paths` and `deploy/DEPLOYMENT_REGISTRY.yaml`). Repo-root YAML
files are deprecated fallbacks only.

Desktop shortcuts target locally built EXEs in the install root (typically
`C:\Stinger\`). Rebuild on each PC with `.\scripts\deploy_build_and_install.ps1`.

## Hardware Summary

| Component | Port A (Left) | Port B (Right) |
|-----------|---------------|----------------|
| DAQ | LeftDAQ | RightDAQ |
| Alicat | COM3 / Address B | COM3 / Address A |
| Transducer | 0.5-4.5V = 0-115 PSI | Same |

## Database

Stinger integrates with the existing SQL Server schema:

- **Reads**:
  - `OrderCalibrationMaster` (work order context)
  - `ProductTestParameters` (test parameters per PartID/SequenceID)
- **Writes**:
  - `OrderCalibrationDetail` (per-unit results; retest inserts new `ActivationID`)

See `docs/DATABASE_CONTRACT.md` for full contract.

## Build and Publish (Windows EXE)

Stinger can be packaged as a single-file EXE with PyInstaller and published to the shared drive:
`Z:\Engineering\Program Builds\Python Builds`.

### 1) Build onefile EXE

From repo root in PowerShell:

`.\scripts\build_stinger.ps1 -InstallPyInstaller`

Outputs:
- `dist\SPS Calibration Stand\SPS Calibration Stand.exe`
- `dist\SPS Calibration Stand\stinger_config.yaml`
- `dist\SPS Calibration Stand\build_manifest.json`

### 2) Publish to shared drive

`.\scripts\publish_stinger_build.ps1`

Publishes to:
- `Z:\Engineering\Program Builds\Python Builds\Stinger\<timestamp>\`
- updates:
  - `Z:\Engineering\Program Builds\Python Builds\Stinger\latest.json`
  - `Z:\Engineering\Program Builds\Python Builds\Stinger\latest.txt`

### Rollback

To roll back, use a previous timestamped folder in
`Z:\Engineering\Program Builds\Python Builds\Stinger\`.
Set consumers to that folder, or set `latest.txt`/`latest.json` back to the previous build.

### Notes

- The build is a onefile EXE, so there is no required `_internal` folder beside it.
- Onefile EXE startup can be slightly slower because it self-extracts at launch.
- Machine-specific settings belong in `configs/<hostname>/`, not beside the EXE.
- EXEs are not tracked in git; build and install locally (or via deploy scripts).

