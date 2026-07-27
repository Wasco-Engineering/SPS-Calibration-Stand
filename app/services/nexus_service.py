"""Nexus client helpers for Stinger (badge login + optional WO resolve + Postgres PTP)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Per-stand Nexus keys (committed; not treated as secrets).
STAND_KEYS: Dict[str, str] = {
    "CA-SPS-01": "wsk_a12NoQgKjVsu4TRX59P91_5kJv6wPwcXf7DY4sNMyY8",
    "CA-SPS-02": "wsk_zwOGJ4jsyprRc3DSX4DLYZ8rbKwJO4cWwkuhb0uUMT4",
    "ID-SPS-01": "wsk_hGsNOhl4UCkf1TfHP26E7R_6M6qVbhZcdyXNjYoOi6I",
    "ID-SPS-02": "wsk_uHjPgBNlaPCbggAs6moST5jr8HQQKAkkMb_iZercP58",
}

# Legacy STINGER_* ids still accepted when resolving keys / defaults.
LEGACY_STAND_IDS: Dict[str, str] = {
    "STINGER_01": "CA-SPS-01",
    "STINGER_02": "CA-SPS-02",
    "STINGER_03": "ID-SPS-01",
    "STINGER_04": "ID-SPS-02",
}

DEFAULT_EQUIPMENT_ID = "CA-SPS-01"


def normalize_equipment_id(raw: Any, default: str = DEFAULT_EQUIPMENT_ID) -> str:
    """Map legacy STINGER_* labels to CA/ID-SPS stand ids used for DB writes."""
    value = str(raw or "").strip()
    if not value:
        return default
    return LEGACY_STAND_IDS.get(value, value)


def resolve_stand_key(config: Dict[str, Any]) -> Optional[str]:
    """Pick the Nexus stand key for this PC from config / equipment id."""
    nexus_cfg = dict(config.get("nexus") or {})
    explicit = nexus_cfg.get("stand_key")
    if explicit:
        return str(explicit).strip() or None

    equipment_id = normalize_equipment_id(
        (config.get("test_parameters") or {}).get("equipment_id"),
    )
    keys = dict(nexus_cfg.get("stand_keys") or STAND_KEYS)
    key = keys.get(equipment_id)
    if key:
        return str(key).strip() or None
    return None


def build_nexus_client(config: Dict[str, Any]) -> Any | None:
    nexus_cfg = dict(config.get("nexus") or {})
    if not bool(nexus_cfg.get("enabled", False)):
        return None
    stand_key = resolve_stand_key(config)
    if not stand_key:
        logger.warning("nexus.enabled but stand_key missing — Nexus client not created")
        return None
    try:
        from wasco_instruments.nexus import NexusClient, NexusConfig
    except ImportError as exc:
        logger.error("wasco-instruments[nexus] not installed: %s", exc)
        return None

    cache_path = nexus_cfg.get("cache_path")
    if cache_path:
        path = Path(str(cache_path))
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[2] / path
        path.parent.mkdir(parents=True, exist_ok=True)
        cache_path = str(path)

    client = NexusClient(
        NexusConfig(
            base_url=str(nexus_cfg.get("base_url", "https://id-production.wasconexus.com/")),
            stand_key=str(stand_key),
            timeout_sec=float(nexus_cfg.get("timeout_sec", 10.0)),
            verify_tls=bool(nexus_cfg.get("verify_tls", True)),
            cache_path=cache_path,
            cache_ttl_sec=int(nexus_cfg.get("cache_ttl_sec", 43200)),
        )
    )
    try:
        client.connect()
    except Exception as exc:
        logger.warning("Nexus connect failed (client still usable for later retries): %s", exc)
    return client


def build_ptp_repository(config: Dict[str, Any]) -> Any | None:
    ptp_cfg = dict(config.get("ptp_database") or {})
    if not bool(ptp_cfg.get("enabled", False)):
        return None
    try:
        from wasco_instruments.db import PtpRepository
    except ImportError as exc:
        logger.error("wasco-instruments[postgres] not installed: %s", exc)
        return None

    primary = dict(ptp_cfg.get("primary") or {})
    if not primary.get("host"):
        logger.warning("ptp_database.enabled but primary.host missing")
        return None
    primary.setdefault("type", "postgres")
    secondary_raw = dict(ptp_cfg.get("secondary") or {})
    secondary = None
    if secondary_raw.get("host"):
        secondary = dict(secondary_raw)
        secondary.setdefault("type", "postgres")
    return PtpRepository(
        primary,
        secondary,
        retry_primary_sec=ptp_cfg.get("retry_primary_sec"),
        timeout_sec=ptp_cfg.get("timeout_sec"),
    )


def badge_login(nexus_client: Any, badge_token: str) -> Dict[str, Any]:
    """Return operator fields from a Nexus badge login."""
    result = nexus_client.login(badge_token)
    operator = result.operator
    return {
        "operator_id": str(operator.netsuite_id or operator.id),
        "operator_name": operator.full_name or "",
        "from_cache": bool(result.from_cache),
        "open_runs": [
            {
                "work_order_number": run.work_order_number,
                "operation_sequence": run.operation_sequence,
                "item_name": run.item_name,
                "status": run.status,
            }
            for run in (result.open_runs or [])
        ],
    }


def resolve_work_order_via_nexus(
    nexus_client: Any,
    shop_order: str,
    *,
    sequence: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve part / qty / sequences for a scanned work order via Nexus."""
    ptp = nexus_client.request_ptp(shop_order.strip(), sequence=sequence or None)
    sequence_ids = list(ptp.sequence_ids or [])
    chosen = sequence or (sequence_ids[0] if sequence_ids else "")
    return {
        "ShopOrder": str(ptp.shop_order or shop_order).strip(),
        "PartID": str(ptp.part_id or "").strip(),
        "SequenceID": str(chosen or "").strip(),
        "OrderQTY": int(ptp.order_qty or 0),
        "OrderQty": int(ptp.order_qty or 0),
        "SequenceIDs": sequence_ids,
    }
