# Config templates

Per-computer configs are tracked in git under:

```text
configs/<hostname>/stinger_config.yaml
configs/<hostname>/quality_cal_config.yaml
```

Runtime selection is automatic from the machine hostname (`app.core.paths`).
Hostnames are listed in `deploy/DEPLOYMENT_REGISTRY.yaml`.

To seed a **new** PC folder:

```powershell
.\scripts\deploy_init_stand.ps1 -StandId CA-SPS-01 -EquipmentId CA-SPS-01
```

Quality Cal Apply writes error models into the active host’s
`configs/<hostname>/stinger_config.yaml`.

Repo-root `stinger_config.yaml` / `quality_cal_config.yaml` are deprecated
fallbacks only — do not put production offsets there.

Do not store rotating logs under `configs/`; logs go to `C:\Stinger\logs`.
