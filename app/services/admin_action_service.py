"""Admin action dispatch extracted from WorkOrderController."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)


class AdminActionService:
    """Handle admin actions and delegate side effects through callbacks."""

    def __init__(
        self,
        *,
        on_set_main_measurement_source: Callable[[Dict[str, Any]], None],
        on_refresh_hardware: Callable[[], None],
        on_refresh_database: Callable[[], None],
        on_reconnect_hardware: Callable[[], None],
        on_reconnect_database: Callable[[], None],
        on_open_logs: Callable[[], None],
        on_open_bug_reports: Callable[[], None],
        on_export_logs: Callable[[], None],
        on_export_history: Callable[[], None],
        on_safety_override: Callable[[Dict[str, Any]], None],
    ) -> None:
        self._on_set_main_measurement_source = on_set_main_measurement_source
        self._on_refresh_hardware = on_refresh_hardware
        self._on_refresh_database = on_refresh_database
        self._on_reconnect_hardware = on_reconnect_hardware
        self._on_reconnect_database = on_reconnect_database
        self._on_open_logs = on_open_logs
        self._on_open_bug_reports = on_open_bug_reports
        self._on_export_logs = on_export_logs
        self._on_export_history = on_export_history
        self._on_safety_override = on_safety_override

    def handle(self, action: str, payload: Dict[str, Any]) -> None:
        if action == 'set_main_measurement_source':
            self._on_set_main_measurement_source(payload)
            return
        if action == 'refresh_hardware':
            self._on_refresh_hardware()
            return
        if action == 'refresh_db':
            self._on_refresh_database()
            return
        if action == 'reconnect_hardware':
            self._on_reconnect_hardware()
            return
        if action == 'reconnect_db':
            self._on_reconnect_database()
            return
        if action == 'open_logs':
            self._on_open_logs()
            return
        if action == 'open_bug_reports':
            self._on_open_bug_reports()
            return
        if action == 'export_logs':
            self._on_export_logs()
            return
        if action == 'export_history':
            self._on_export_history()
            return
        if action == 'safety_override':
            self._on_safety_override(payload)
            return
        logger.warning('Unknown admin action: %s payload=%s', action, payload)
