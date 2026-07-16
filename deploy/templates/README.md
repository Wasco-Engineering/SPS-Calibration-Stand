# Config templates

Copy to machine-local stand directory via:

```powershell
.\scripts\deploy_init_stand.ps1 -StandId CA-SPS-02
```

Source files for the copy are repo-root `stinger_config.yaml` and `quality_cal_config.yaml` at init time. Edit the **local** copies under `%LOCALAPPDATA%\Stinger\<STAND_ID>\` (or `C:\Stinger\` on deploy PCs).

The repo `stinger_config.yaml` is a template: no fitted `transducer_error_model` blocks and no DB password. Apply calibration via Quality Cal on the stand PC.

Do not store production DB passwords in Z: shared folders.

Script capture CSVs and optimizer output belong under `scripts/data/` (gitignored), not in this templates tree.
