"""Main QWizard for the standalone quality calibration workflow."""

from __future__ import annotations

import logging
from typing import Any, Optional

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QMessageBox, QWizard

from quality_cal.ui.styles import APP_STYLESHEET

from app.hardware.port import PortManager
from quality_cal.config import QualitySettings
from quality_cal.core.hardware_checks import evaluate_labjack_port_check
from quality_cal.core.hardware_discovery import (
    discover_alicat_assignments,
    discover_labjack_target,
    discover_mensor_port,
)
from quality_cal.core.mensor_reader import MensorReader
from quality_cal.session import QualityCalibrationSession
from quality_cal.ui.pages.calibration_run_page import CalibrationRunPage
from quality_cal.ui.pages.confirm_port_page import ConfirmPortPage
from quality_cal.ui.pages.leak_check_page import LeakCheckPage
from quality_cal.ui.pages.login_hardware_page import LoginHardwarePage
from quality_cal.ui.pages.report_page import ReportPage

logger = logging.getLogger(__name__)


class _SetupAndHardwarePage(LoginHardwarePage):
    def nextId(self) -> int:
        wizard = self.wizard()
        if wizard is None:
            return -1
        if wizard.session.include_leak_check:
            return wizard.PAGE_LEAK_LEFT
        return wizard.PAGE_CONFIRM_LEFT


class _LeakCheckLeftPage(LeakCheckPage):
    def nextId(self) -> int:
        return self.wizard().PAGE_LEAK_RIGHT


class _LeakCheckRightPage(LeakCheckPage):
    def nextId(self) -> int:
        return self.wizard().PAGE_CONFIRM_LEFT


class _ConfirmLeftPage(ConfirmPortPage):
    def nextId(self) -> int:
        return self.wizard().PAGE_CAL_LEFT


class _CalLeftPage(CalibrationRunPage):
    def nextId(self) -> int:
        return self.wizard().PAGE_CONFIRM_RIGHT


class _ConfirmRightPage(ConfirmPortPage):
    def nextId(self) -> int:
        return self.wizard().PAGE_CAL_RIGHT


class _CalRightPage(CalibrationRunPage):
    def nextId(self) -> int:
        return self.wizard().PAGE_REPORT


class QualityCalibrationWizard(QWizard):
    PAGE_SETUP_AND_HARDWARE = 0
    PAGE_LEAK_LEFT = 1
    PAGE_LEAK_RIGHT = 2
    PAGE_CONFIRM_LEFT = 3
    PAGE_CAL_LEFT = 4
    PAGE_CONFIRM_RIGHT = 5
    PAGE_CAL_RIGHT = 6
    PAGE_REPORT = 7

    def __init__(self, *, config: dict, settings: QualitySettings, parent=None) -> None:
        super().__init__(parent)
        self.config = config
        self.settings = settings
        self.session = QualityCalibrationSession()
        self.port_manager: Optional[PortManager] = None
        self.mensor_reader: Optional[MensorReader] = None
        self._labjack_probe_detail = "LabJack discovery not yet run."
        self._discovery_applied = False

        self.setWindowTitle("Quality Calibration")
        self.setMinimumSize(1100, 800)
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setStyleSheet(APP_STYLESHEET)

        self._create_pages()
        self.finished.connect(self._on_finished)
        QTimer.singleShot(0, self._style_wizard_buttons)

    def _style_wizard_buttons(self) -> None:
        """Apply primary style to Next/Finish so they match the design system."""
        for role in (QWizard.WizardButton.NextButton, QWizard.WizardButton.FinishButton):
            btn = self.button(role)
            if btn is not None:
                btn.setObjectName("primaryButton")

    def _create_pages(self) -> None:
        self.setPage(self.PAGE_SETUP_AND_HARDWARE, _SetupAndHardwarePage(self))
        self.setPage(
            self.PAGE_LEAK_LEFT,
            _LeakCheckLeftPage(port_id="port_a", title="Leak Check - Left Port", parent=self),
        )
        self.setPage(
            self.PAGE_LEAK_RIGHT,
            _LeakCheckRightPage(port_id="port_b", title="Leak Check - Right Port", parent=self),
        )
        self.setPage(
            self.PAGE_CONFIRM_LEFT,
            _ConfirmLeftPage(
                title="Confirm Mensor on Left Port",
                message="Move the Mensor to the left port, verify the connection is secure, then continue.",
                parent=self,
            ),
        )
        self.setPage(
            self.PAGE_CAL_LEFT,
            _CalLeftPage(port_id="port_a", title="Left Port Calibration", parent=self),
        )
        self.setPage(
            self.PAGE_CONFIRM_RIGHT,
            _ConfirmRightPage(
                title="Confirm Mensor on Right Port",
                message="Move the Mensor to the right port, verify the connection is secure, then continue.",
                parent=self,
            ),
        )
        self.setPage(
            self.PAGE_CAL_RIGHT,
            _CalRightPage(port_id="port_b", title="Right Port Calibration", parent=self),
        )
        self.setPage(self.PAGE_REPORT, ReportPage(self))

    def hardware_check_poll_interval_ms(self) -> int:
        quality_cfg = self.config.get("quality", {})
        try:
            return max(500, int(quality_cfg.get("hardware_check_poll_interval_ms", 2000)))
        except (TypeError, ValueError):
            return 2000

    def serial_auto_discovery_enabled(self) -> bool:
        quality_cfg = self.config.get("quality", {})
        discovery_cfg = quality_cfg.get("hardware_discovery", {}) or {}
        return bool(discovery_cfg.get("enable_serial_auto_discovery", True))

    def _apply_discovered_hardware_assignments(self) -> None:
        if self._discovery_applied:
            return

        hardware_cfg = self.config.setdefault("hardware", {})
        labjack_cfg = hardware_cfg.setdefault("labjack", {})
        alicat_cfg = hardware_cfg.setdefault("alicat", {})
        port_a_cfg = alicat_cfg.setdefault("port_a", {})
        port_b_cfg = alicat_cfg.setdefault("port_b", {})
        mensor_cfg = hardware_cfg.setdefault("mensor", {})
        changed = False

        labjack_probe = discover_labjack_target(self.config)
        self._labjack_probe_detail = str(labjack_probe.get("detail", "LabJack discovery unavailable."))
        if bool(labjack_probe.get("found", False)):
            desired_device = str(labjack_probe.get("device_type", labjack_cfg.get("device_type", "T7")))
            desired_connection = str(
                labjack_probe.get("connection_type", labjack_cfg.get("connection_type", "USB"))
            )
            desired_identifier = str(labjack_probe.get("identifier", labjack_cfg.get("identifier", "ANY")))
            if str(labjack_cfg.get("device_type", "")).strip() != desired_device:
                labjack_cfg["device_type"] = desired_device
                changed = True
            if str(labjack_cfg.get("connection_type", "")).strip() != desired_connection:
                labjack_cfg["connection_type"] = desired_connection
                changed = True
            if str(labjack_cfg.get("identifier", "")).strip() != desired_identifier:
                labjack_cfg["identifier"] = desired_identifier
                changed = True
        if self.serial_auto_discovery_enabled():
            discovered_alicats = discover_alicat_assignments(self.config)
            for logical_port, discovered_port in discovered_alicats.items():
                target_cfg = port_a_cfg if logical_port == "port_a" else port_b_cfg
                if str(target_cfg.get("com_port", "")).strip() != discovered_port:
                    logger.info(
                        "Updating %s Alicat COM port from %s to %s",
                        logical_port,
                        target_cfg.get("com_port"),
                        discovered_port,
                    )
                    target_cfg["com_port"] = discovered_port
                    changed = True

            discovered_mensor = discover_mensor_port(
                self.config,
                exclude_ports={
                    str(port_a_cfg.get("com_port", "")).strip(),
                    str(port_b_cfg.get("com_port", "")).strip(),
                },
            )
            if discovered_mensor and str(mensor_cfg.get("port", "")).strip() != discovered_mensor:
                logger.info(
                    "Updating Mensor COM port from %s to %s",
                    mensor_cfg.get("port"),
                    discovered_mensor,
                )
                mensor_cfg["port"] = discovered_mensor
                changed = True

        if changed:
            self.cleanup_hardware()
        self._discovery_applied = True

    def _ensure_mensor_reader(self) -> None:
        mensor_cfg = self.config.get("hardware", {}).get("mensor", {})
        if self.mensor_reader is None:
            self.mensor_reader = MensorReader(mensor_cfg)
            self.mensor_reader.connect()
            return
        if self.mensor_reader.status in {"Connected", "Connected (simulated)"}:
            return
        self.mensor_reader.close()
        self.mensor_reader = MensorReader(mensor_cfg)
        self.mensor_reader.connect()

    def get_hardware_snapshot(self) -> dict[str, Any]:
        self._apply_discovered_hardware_assignments()

        if self.port_manager is None:
            self.port_manager = PortManager(self.config)
            self.port_manager.initialize_ports()
            self.port_manager.connect_all()

        self._ensure_mensor_reader()

        entries: list[dict[str, Any]] = []
        overall_ok = True
        for port_id in ("port_a", "port_b"):
            port = self.port_manager.get_port(port_id)
            if port is None:
                overall_ok = False
                entries.append(
                    {
                        "name": f"{port_id} hardware",
                        "ok": False,
                        "detail": "Port is not configured.",
                    }
                )
                continue

            lj_status = port.daq.get_status()
            probe_detail = (
                f"Target={lj_status.get('device_type')}/"
                f"{lj_status.get('connection_type')}/"
                f"{lj_status.get('identifier')} | {self._labjack_probe_detail}"
            )
            labjack_result = evaluate_labjack_port_check(
                port_id=port_id,
                port=port,
                config=self.config,
                probe_detail=probe_detail,
            )
            if not labjack_result.ok:
                overall_ok = False
            entries.append(
                {
                    "name": f"{port_id} LabJack",
                    "ok": labjack_result.ok,
                    "detail": labjack_result.detail,
                }
            )

            alicat_status = port.alicat.get_status()
            alicat_reading = port.alicat.read_status()
            if alicat_reading is None and not bool(alicat_status.get("connected", False)):
                port.alicat.connect()
                alicat_status = port.alicat.get_status()
                alicat_reading = port.alicat.read_status()
            alicat_ok = alicat_reading is not None
            if not alicat_ok:
                overall_ok = False
            entries.append(
                {
                    "name": f"{port_id} Alicat",
                    "ok": alicat_ok,
                    "detail": (
                        f"{alicat_status.get('status', 'Unknown')} | "
                        f"Port={alicat_status.get('port')} Address={alicat_status.get('address')}"
                        if alicat_reading is None
                        else f"{alicat_status.get('status', 'Unknown')} | "
                        f"Port={alicat_status.get('port')} Address={alicat_status.get('address')} | "
                        f"Pressure={alicat_reading.pressure:.3f} psia "
                        f"Setpoint={alicat_reading.setpoint:.3f}"
                    ),
                }
            )

        mensor_ok = False
        mensor_detail = self.mensor_reader.status if self.mensor_reader is not None else "Not initialized"
        if self.mensor_reader is not None:
            if self.mensor_reader.status in {"Connected", "Connected (simulated)"}:
                try:
                    mensor = self.mensor_reader.read_pressure()
                    mensor_ok = True
                    mensor_detail = f"{self.mensor_reader.status} | Pressure={mensor.pressure_psia:.3f} psia"
                    # Flag obviously out-of-range readings so operator can verify sensor
                    if not (0.0 <= mensor.pressure_psia <= 300.0):
                        mensor_detail += " (unusual reading — verify sensor)"
                except Exception as exc:
                    mensor_detail = f"{self.mensor_reader.status} | Read failed: {exc}"
        if not mensor_ok:
            overall_ok = False
        entries.append(
            {
                "name": "Mensor",
                "ok": mensor_ok,
                "detail": mensor_detail,
            }
        )

        ready_count = sum(1 for entry in entries if entry["ok"])
        return {
            "overall_ok": overall_ok,
            "summary": f"{ready_count}/{len(entries)} hardware checks passing.",
            "discovery_note": self._labjack_probe_detail,
            "entries": entries,
        }

    def cleanup_hardware(self) -> None:
        if self.mensor_reader is not None:
            self.mensor_reader.close()
            self.mensor_reader = None
        if self.port_manager is not None:
            self.port_manager.disconnect_all()
            self.port_manager = None
        self._discovery_applied = False

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self.currentId() != self.PAGE_REPORT:
            reply = QMessageBox.question(
                self,
                "Exit Quality Calibration",
                "Are you sure you want to exit?\n\nAny unsaved progress will be lost.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.cleanup_hardware()
        event.accept()

    def _on_finished(self, _result: int) -> None:
        logger.info("Quality calibration wizard finished")
        self.cleanup_hardware()
