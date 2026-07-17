"""Entry point for the standalone quality calibration application."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QMessageBox

from quality_cal.config import load_config, parse_quality_settings, setup_logging
from quality_cal.ui.styles import APP_STYLESHEET
from quality_cal.ui import QualityCalibrationWindow

logger = logging.getLogger(__name__)


def main() -> int:
    try:
        config = load_config()
        settings = parse_quality_settings(config)
    except Exception as exc:
        print(f"ERROR loading quality calibration config: {exc}")
        return 1

    setup_logging(config)
    logger.info("Starting standalone quality calibration application")

    app = QApplication(sys.argv)
    app.setApplicationName(str(config.get("app", {}).get("name", "Quality Calibration")))
    app.setApplicationVersion(str(config.get("app", {}).get("version", "0.1.0")))
    app.setStyleSheet(APP_STYLESHEET)
    if hasattr(Qt.ApplicationAttribute, "AA_UseHighDpiPixmaps"):
        app.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)

    try:
        window = QualityCalibrationWindow(config=config, settings=settings)
        app.aboutToQuit.connect(window.cleanup_hardware)
        window.showMaximized()
        exit_code = app.exec()
        window.cleanup_hardware()
        return exit_code
    except Exception as exc:
        logger.exception("Fatal startup error")
        QMessageBox.critical(None, "Startup Error", str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
