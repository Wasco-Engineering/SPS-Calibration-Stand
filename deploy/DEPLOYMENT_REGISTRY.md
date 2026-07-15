# Stinger deployment registry

Track **every PC** that runs Stinger or Quality Cal, where its **local** configs live, and where **built EXEs** are installed. Do not store DB passwords in this file — only paths and non-secret identifiers.

**Machine-readable copy:** `deploy/DEPLOYMENT_REGISTRY.yaml` (same data; keep in sync).

---

## How config resolution works

| Priority | Source |
|----------|--------|
| 1 | `STINGER_CONFIG_DIR` → `%LOCALAPPDATA%\Stinger\<STAND_ID>\` |
| 2 | `STINGER_CONFIG` / `STINGER_QUALITY_CONFIG` (full file paths) |
| 3 | Next to `SPS Calibration Stand.exe` (fallback only) |
| 4 | Repo root (development fallback) |

Set env vars once per PC (`scripts/deploy_set_stand_env.ps1`).

**Shared Z: (code / release artifacts only):** `Z:\Engineering\Program Builds\Python Builds\Stinger\`

---

## Deployed computers

### CA-MAN-SPS-02 — Stand 1 (primary / reference)

| Field | Value |
|-------|--------|
| **Hostname** | `CA-MAN-SPS-02` |
| **Stand ID** | `STINGER_01` |
| **Equipment ID** | `STINGER_01` |
| **Role** | Reference stand — transducers + Mensor on port_b tee |
| **Local config dir** | `%LOCALAPPDATA%\Stinger\STINGER_01\` |
| **Stinger config** | `%LOCALAPPDATA%\Stinger\STINGER_01\stinger_config.yaml` |
| **Quality Cal config** | `%LOCALAPPDATA%\Stinger\STINGER_01\quality_cal_config.yaml` |
| **Logs** | `%LOCALAPPDATA%\Stinger\STINGER_01\logs\` |
| **Repo dev copy** | `C:\Stinger` |
| **Desktop EXEs (Engineer)** | `%USERPROFILE%\Desktop\Stinger\` |
| **Desktop EXEs (CalibrationUser)** | `C:\Users\CalibrationUser\Desktop\Stinger\` (install via elevated `deploy_install_desktop.ps1`) |
| **Z: release bin** | `Z:\Engineering\Program Builds\Python Builds\Stinger\bin\` |
| **Machine env (optional)** | `deploy_set_machine_env.ps1` — shared `STINGER_CONFIG_DIR` for all users |

**Hardware (this stand):**

| Item | Setting |
|------|---------|
| LabJack T7 | USB `ANY` |
| Alicat | `COM3` @ 115200 — Port A = address **A**, Port B = address **B** |
| Mensor | `COM4` @ 57600 (port_b tee) |
| Transducers | **Installed** — Port A AIN2/3, Port B AIN0/1, 0–30 PSIA abs |
| Solenoids | Port A **DIO19**, Port B **DIO18** |
| Measurement | `auto`, `transducer_only_below_psi: 20`, Mensor-based `transducer_error_model` on both ports |

**Env (User):**

```powershell
STINGER_STAND_ID=STINGER_01
STINGER_CONFIG_DIR=%LOCALAPPDATA%\Stinger\STINGER_01
```

---

### Stand 2 — (second bench; fill in hostname when known)

| Field | Value |
|-------|--------|
| **Hostname** | `_TBD_` (e.g. second shop PC name) |
| **Stand ID** | `STINGER_02` |
| **Equipment ID** | `STINGER_02` |
| **Role** | Identical plumbing; **transducers not installed** (capped) until sensors arrive |
| **Local config dir** | `%LOCALAPPDATA%\Stinger\STINGER_02\` |
| **Desktop EXEs** | `%USERPROFILE%\Desktop\Stinger\` |
| **Z: release bin** | Same shared `bin\` folder (EXE is shared; config is not) |

**Hardware differences:**

| Item | Setting |
|------|---------|
| Transducers | `transducer_installed: false` until installed |
| Measurement | `preferred_source: alicat` until transducers + Mensor cal |

**Env (User):**

```powershell
STINGER_STAND_ID=STINGER_02
STINGER_CONFIG_DIR=%LOCALAPPDATA%\Stinger\STINGER_02
```

---

### ID-MAN-CR3-01 - Stand 4 (SPA stand)

| Field | Value |
|-------|--------|
| **Hostname** | `ID-MAN-CR3-01` |
| **Stand ID** | `STINGER_04` |
| **Equipment ID** | `STINGER_04` |
| **Role** | SPA stand |
| **Local config dir** | `C:\Stinger` |
| **Stinger config** | `C:\Stinger\stinger_config.yaml` |
| **Quality Cal config** | `C:\Stinger\quality_cal_config.yaml` |
| **Logs** | `C:\Stinger\logs` |
| **Repo dev copy** | `C:\Stinger` |
| **Z: release bin** | `Z:\Engineering\Program Builds\Python Builds\Stinger\bin\` |

**Hardware (verified 2026-06-24):**

| Item | Setting |
|------|---------|
| LabJack T7 | USB `ANY`; transducers not installed |
| Alicat | `COM3` @ 115200 - Port A = address **A**, Port B = address **B** |
| Mensor | Configured `COM4` @ 57600, but only `COM3` was detected during setup |
| Transducers | **Not installed** — Port A AIN2/3, Port B AIN0/1 reserved for future 0-30 PSIA abs sensors |
| Solenoids | Port A **DIO19**, Port B **DIO18** |
| Measurement | `alicat` |

**Env (User set; Machine env requires elevated PowerShell):**

```powershell
STINGER_STAND_ID=STINGER_04
STINGER_CONFIG_DIR=C:\Stinger
```

---

## EXE artifacts (shared build output)

Built from `C:\Stinger` via `.\scripts\deploy_build_and_install.ps1`:

| EXE | Purpose |
|-----|---------|
| `SPS Calibration Stand.exe` | Main calibration UI (`run.py`) |
| `QualityCal.exe` | Full pressure sweep workflow + QF87 certificate export (`run_quality_cal.py`) |

Copy targets:

1. `%USERPROFILE%\Desktop\Stinger\` — per-PC shortcut folder (no YAML required next to exe if env is set)
2. `Z:\Engineering\Program Builds\Python Builds\Stinger\bin\` — shared binaries for IT / other PCs

---

## Maintenance

- **New PC:** Add a section here + row in `DEPLOYMENT_REGISTRY.yaml`, run `deploy_init_stand.ps1`, set env, run parity guide on Z:.
- **Config change:** Edit **local** YAML only; note date in git commit message or changelog if templates change.
- **New EXE build:** Run `deploy_build_and_install.ps1`; record build timestamp from `Desktop\Stinger\build_manifest.json`.
