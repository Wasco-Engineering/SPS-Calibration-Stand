# Per-computer Stinger / Quality Cal configs
#
# Layout:
#   configs/<hostname>/stinger_config.yaml
#   configs/<hostname>/quality_cal_config.yaml
#
# At runtime `app.core.paths` selects the folder from this machine's hostname
# (override with STINGER_HOSTNAME or STINGER_CONFIG_DIR). Hostnames are listed in
# `deploy/DEPLOYMENT_REGISTRY.yaml`.
#
# Each stand keeps its own Alicat / transducer / Mensor error models and
# equipment_id here. Quality Cal Apply writes back into *this* host's file.
#
# Do not put rotating logs in these folders; logs go to C:\Stinger\logs.

