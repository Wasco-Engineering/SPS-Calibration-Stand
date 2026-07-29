# Stinger — Build, Deploy, and Local Configuration

## Principle

| Location | What lives there | Scope |
|----------|------------------|--------|
| **Git repo** | Source, plus **per-computer** `configs/<hostname>/*.yaml` (COM, equipment_id, Mensor/Alicat/transducer models) | Shared history; each host edits only its folder |
| **C:\Stinger\logs** | Rotating logs | This PC only |

Runtime selects `configs/<COMPUTERNAME>/` automatically (`app.core.paths`). Leave `STINGER_CONFIG_DIR` unset (or pointed at `C:\Stinger`, which is treated as legacy and remapped to the hostname folder).

Repo-root `stinger_config.yaml` is a deprecated fallback only.

---

## Recommended directory layout

### Shared (computer-agnostic)

```
Z:\Engineering\Program Builds\Python Builds\Stinger\
  STINGER_STAND_PARITY_SETUP.md     # Bring-up checklist
  releases\                         # Optional: versioned PyInstaller / zip builds
  source\                           # Optional: git mirror or release tag checkouts
  templates\                        # Optional: copy of deploy\templates from repo
```

Set for documentation and release scripts:

```powershell
$env:STINGER_RELEASE_ROOT = 'Z:\Engineering\Program Builds\Python Builds\Stinger'
```

### Per-PC install (computer-specific) — **C:\Stinger**

Standard layout on every stand / calibration PC (repo clone or installed tree):

```
C:\Stinger\
  SPS Calibration Stand.exe
  QualityCal.exe
  configs\
    <hostname>\
      stinger_config.yaml      # COM ports, equipment_id, error models, DB
      quality_cal_config.yaml  # Mensor COM, calibration profiles
  logs\                        # sweeps, quality cal, mensor checks
  deploy\
    DEPLOYMENT_REGISTRY.yaml
    templates\qf87\            # QF87 Word template (copied on install)
```

Set machine environment (all users, including CalibrationUser):

```powershell
STINGER_STAND_ID=CA-SPS-02
# Prefer leaving STINGER_CONFIG_DIR unset so configs/<hostname>/ is used.
```

Legacy layout (still supported if YAML exists there):

```
%LOCALAPPDATA%\Stinger\<STAND_ID>\
```

---

## Environment variables

| Variable | Purpose |
|----------|---------|
| `STINGER_STAND_ID` | Stand / equipment label; maps via registry if hostname folder missing |
| `STINGER_HOSTNAME` | Override hostname used for `configs/<hostname>/` |
| `STINGER_HOME` | Override local root (default `%LOCALAPPDATA%\Stinger`) |
| `STINGER_CONFIG_DIR` | Explicit config directory override (tests/dev). If set to `C:\Stinger`, remaps to hostname folder when present |
| `STINGER_CONFIG` | Full path to `stinger_config.yaml` only |
| `STINGER_QUALITY_CONFIG` | Full path to `quality_cal_config.yaml` only |
| `STINGER_RELEASE_ROOT` | Shared Z: root for docs/releases |

**Config resolution order** (implemented in `app/core/paths.py`):

1. `STINGER_CONFIG` / `STINGER_QUALITY_CONFIG` if set  
2. `STINGER_CONFIG_DIR\stinger_config.yaml` (and quality file) if present  
3. Frozen executable directory (PyInstaller)  
4. Repo root (developer fallback)

---

## One-time stand setup (PowerShell)

From repo root (`C:\Stinger` on the build machine):

```powershell
cd C:\Stinger
# If scripts are blocked by execution policy, use the .bat wrapper or Bypass (one session):
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\deploy_install_to_c_stinger.ps1 -Build -InstallPyInstaller -StandId CA-SPS-01 -SetMachineEnv -DesktopShortcuts
```

Or from **elevated** Command Prompt (no execution-policy change):

```cmd
cd /d C:\Stinger
scripts\deploy_install_to_c_stinger.bat CA-SPS-01
```

Or build + install in one step:

```powershell
.\scripts\deploy_build_and_install.ps1 -StandId CA-SPS-01 -SetMachineEnv -InstallPyInstaller
```

Edit per-PC settings (COM ports, DB, Mensor) in:

```
C:\Stinger\stinger_config.yaml
C:\Stinger\quality_cal_config.yaml
```

Re-run `deploy_install_to_c_stinger.ps1` without `-ForceConfig` to update EXEs only; existing YAML is preserved.

Restart or sign out/in after `-SetMachineEnv` so all users pick up `STINGER_CONFIG_DIR`.

---

## Running applications

```powershell
cd C:\Stinger   # or Z:\...\source\Stinger
.\.venv\Scripts\Activate.ps1

# Verify which config is loaded
python -c "from app.core.paths import get_config_dir, get_stinger_config_path, get_quality_cal_config_path; print(get_config_dir()); print(get_stinger_config_path()); print(get_quality_cal_config_path())"

python run.py
python run_quality_cal.py
```

### Shortcuts for CalibrationUser

Use `-DesktopShortcuts` on `deploy_install_to_c_stinger.ps1` (run **elevated** if `TargetUser` is not you).

Executables live under `C:\Stinger\`; shortcuts point there with working directory `C:\Stinger`.

---

## Mensor-relative offsets

Fit `transducer_error_model` / `alicat_error_model` in the **local** `stinger_config.yaml` using Quality Cal (Mensor is the reference). The repo-root template omits fitted models and DB credentials.

Vacuum leak (both ports):

```powershell
python scripts/vacuum_leak_test.py hold --duration 3600 --interval 60
```

---

## Parity between stands

Use the same **commands** on Stand 1 and Stand 2; only **local YAML** and `equipment_id` differ. See `Z:\Engineering\Program Builds\Python Builds\Stinger\STINGER_STAND_PARITY_SETUP.md`.

---

## Deployment registry

Every deployed PC is listed in:

- `deploy/DEPLOYMENT_REGISTRY.md` (human-readable)
- `deploy/DEPLOYMENT_REGISTRY.yaml` (machine-readable)

Update these when adding a stand or changing `STINGER_CONFIG_DIR`.

## Related

- `deploy/templates/` — example configs (copy via `deploy_init_stand.ps1`)
- `docs/INITIAL_SETUP.md` — hardware verification
- `AGENTS.md` — dev test commands
