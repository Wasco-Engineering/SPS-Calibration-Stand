"""Auto-discovery helpers for hardware used by quality calibration."""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.hardware.alicat import AlicatController
from app.hardware import labjack as labjack_module
from quality_cal.core.mensor_reader import MensorReader

logger = logging.getLogger(__name__)


def build_candidate_ports(*port_groups: list[str]) -> list[str]:
    """Merge preferred and detected port lists while preserving order."""
    merged: list[str] = []
    seen: set[str] = set()
    for group in port_groups:
        for port in group:
            normalized = str(port or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            merged.append(normalized)
    return merged


def available_serial_ports() -> list[str]:
    return [entry["device"] for entry in AlicatController.list_available_ports()]


def available_mensor_ports() -> list[str]:
    return MensorReader.list_available_ports()


def _ip_to_string(ip_value: Any) -> Optional[str]:
    ljm = getattr(labjack_module, "ljm", None)
    if not labjack_module.LJM_AVAILABLE or ljm is None:
        return None
    try:
        return str(ljm.numberToIP(ip_value))
    except Exception:
        return None


def _format_labjack_handle_info(info: tuple[Any, ...] | list[Any]) -> str:
    if len(info) < 6:
        return "Connected"
    device_type, connection_type, serial_number, ip_value, port, _ = info[:6]
    connection_name = {
        1: "USB",
        3: "ETHERNET",
        4: "WIFI",
    }.get(int(connection_type), str(connection_type))
    ip_display = _ip_to_string(ip_value) or str(ip_value)
    return (
        f"device_type={device_type} connection={connection_name} "
        f"serial={serial_number} ip={ip_display} port={port}"
    )


def _normalize_labjack_device_type(value: Any, fallback: str = "T7") -> str:
    text = str(value)
    return {
        "7": "T7",
        "T7": "T7",
    }.get(text.upper(), fallback)


def discover_labjack_target(config: dict[str, Any]) -> dict[str, Any]:
    configured = config.get("hardware", {}).get("labjack", {}) or {}
    configured_device = str(configured.get("device_type", "T7"))
    configured_connection = str(configured.get("connection_type", "USB"))
    configured_identifier = str(configured.get("identifier", "ANY"))

    if not labjack_module.LJM_AVAILABLE:
        return {
            "found": False,
            "detail": str(
                getattr(
                    labjack_module,
                    "LJM_IMPORT_ERROR",
                    "LabJack LJM driver unavailable in this environment.",
                )
            ),
        }

    ljm = getattr(labjack_module, "ljm", None)
    if ljm is None:
        return {
            "found": False,
            "detail": "LabJack module imported without active LJM bindings.",
        }

    discovery_errors: list[str] = []
    try:
        dt_any = getattr(ljm.constants, "dtANY", -1)
        ct_any = getattr(ljm.constants, "ctANY", -1)
        discovery_attempts = [
            ("listAll", (dt_any, ct_any)),
            ("listAllS", ("ANY", "ANY")),
            ("listAllS", ("T7", "ANY")),
        ]
        for func_name, args in discovery_attempts:
            func = getattr(ljm, func_name, None)
            if func is None:
                continue
            try:
                result = func(*args)
                if len(result) >= 5 and int(result[0]) > 0:
                    device_type = result[1][0]
                    connection_type = result[2][0]
                    serial_number = result[3][0]
                    ip_value = result[4][0]
                    connection_name = {
                        1: "USB",
                        3: "ETHERNET",
                        4: "WIFI",
                    }.get(int(connection_type), configured_connection)
                    return {
                        "found": True,
                        "device_type": _normalize_labjack_device_type(device_type, configured_device),
                        "connection_type": connection_name,
                        "identifier": str(serial_number),
                        "detail": (
                            f"Discovered serial={serial_number} connection={connection_name} "
                            f"ip={_ip_to_string(ip_value) or ip_value}"
                        ),
                    }
            except Exception as exc:
                discovery_errors.append(f"{func_name}{args}: {exc}")
    except Exception as exc:
        discovery_errors.append(f"discovery setup failed: {exc}")

    probe_attempts = [
        (configured_device, configured_connection, configured_identifier),
        ("T7", "USB", "ANY"),
        ("T7", "ANY", "ANY"),
        ("ANY", "ANY", "ANY"),
    ]
    seen: set[tuple[str, str, str]] = set()
    probe_errors: list[str] = []
    for device_type, connection_type, identifier in probe_attempts:
        candidate = (str(device_type), str(connection_type), str(identifier))
        if candidate in seen:
            continue
        seen.add(candidate)
        handle = None
        try:
            handle = ljm.openS(candidate[0], candidate[1], candidate[2])
            info = ljm.getHandleInfo(handle)
            connection_name = {
                1: "USB",
                3: "ETHERNET",
                4: "WIFI",
            }.get(int(info[1]), candidate[1])
            return {
                "found": True,
                "device_type": _normalize_labjack_device_type(info[0], candidate[0]),
                "connection_type": connection_name,
                "identifier": str(info[2]),
                "detail": f"Connected via probe: {_format_labjack_handle_info(info)}",
            }
        except Exception as exc:
            probe_errors.append(f"{candidate[0]}/{candidate[1]}/{candidate[2]}: {exc}")
        finally:
            if handle is not None:
                try:
                    ljm.close(handle)
                except Exception:
                    pass

    detail_parts = [
        f"Configured target={configured_device}/{configured_connection}/{configured_identifier}",
    ]
    if discovery_errors:
        detail_parts.append(f"Discovery={discovery_errors[-1]}")
    if probe_errors:
        detail_parts.append(f"Open={probe_errors[-1]}")
    return {
        "found": False,
        "detail": " | ".join(detail_parts),
    }


def discover_alicat_assignments(config: dict[str, Any]) -> dict[str, str]:
    hardware_cfg = config.get("hardware", {})
    alicat_cfg = hardware_cfg.get("alicat", {})
    port_a_cfg = alicat_cfg.get("port_a", {}) or {}
    port_b_cfg = alicat_cfg.get("port_b", {}) or {}
    quality_cfg = config.get("quality", {})
    discovery_cfg = quality_cfg.get("hardware_discovery", {}) or {}

    configured_ports = [
        str(port_a_cfg.get("com_port", "")).strip(),
        str(port_b_cfg.get("com_port", "")).strip(),
    ]
    preferred_ports = [
        str(port).strip() for port in discovery_cfg.get("preferred_serial_ports", []) or []
    ]
    candidate_ports = build_candidate_ports(
        configured_ports,
        preferred_ports,
        available_serial_ports(),
    )

    address_map = {
        "port_a": str(port_a_cfg.get("address", "A")).upper(),
        "port_b": str(port_b_cfg.get("address", "B")).upper(),
    }
    assignments: dict[str, str] = {}

    for candidate_port in candidate_ports:
        for logical_port, address in address_map.items():
            if logical_port in assignments:
                continue
            probe_cfg = {
                **alicat_cfg,
                **(port_a_cfg if logical_port == "port_a" else port_b_cfg),
                "com_port": candidate_port,
                "address": address,
                "auto_configure": False,
                "auto_tare_on_connect": False,
                "command_retries": 0,
                "response_read_attempts": 2,
            }
            controller = AlicatController(probe_cfg)
            try:
                if not controller.connect(max_retries=1):
                    continue
                reading = controller.read_status()
                if reading is None:
                    continue
                assignments[logical_port] = candidate_port
                logger.info(
                    "Auto-discovered %s Alicat on %s (address=%s)",
                    logical_port,
                    candidate_port,
                    address,
                )
            except Exception as exc:
                logger.debug(
                    "Alicat discovery probe failed on %s address=%s: %s",
                    candidate_port,
                    address,
                    exc,
                )
            finally:
                try:
                    controller.disconnect()
                except Exception:
                    pass
    return assignments


def discover_mensor_port(
    config: dict[str, Any],
    *,
    exclude_ports: Optional[set[str]] = None,
) -> Optional[str]:
    hardware_cfg = config.get("hardware", {})
    mensor_cfg = hardware_cfg.get("mensor", {}) or {}
    quality_cfg = config.get("quality", {})
    discovery_cfg = quality_cfg.get("hardware_discovery", {}) or {}
    excluded = {str(port).strip() for port in (exclude_ports or set()) if str(port).strip()}

    configured_port = str(mensor_cfg.get("port", "")).strip()
    preferred_ports = [
        str(port).strip() for port in discovery_cfg.get("preferred_serial_ports", []) or []
    ]
    candidate_ports = build_candidate_ports(
        [configured_port],
        preferred_ports,
        available_mensor_ports(),
    )

    for candidate_port in candidate_ports:
        if candidate_port in excluded:
            continue
        probe_cfg = dict(mensor_cfg)
        probe_cfg["port"] = candidate_port
        reader = MensorReader(probe_cfg)
        try:
            if not reader.connect():
                continue
            reader.read_pressure()
            logger.info("Auto-discovered Mensor on %s", candidate_port)
            return candidate_port
        except Exception as exc:
            logger.debug("Mensor discovery probe failed on %s: %s", candidate_port, exc)
        finally:
            reader.close()
    return None
