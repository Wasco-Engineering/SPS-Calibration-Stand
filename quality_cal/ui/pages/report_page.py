"""Final report page for the quality calibration wizard."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtPrintSupport import QPrintDialog, QPrinter
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
    QWizardPage,
)

from app.core.paths import get_stinger_config_path
from quality_cal.core.calibration_export import export_recommended_calibration_yaml
from quality_cal.core.report_generator import (
    build_report_html,
    build_text_document,
    default_csv_filename,
    default_report_filename,
    export_report_csv,
    export_report_pdf,
)
from quality_cal.ui.styles import COLORS, STYLES, TYPOGRAPHY


class ReportPage(QWizardPage):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setTitle("Final Report")
        self.setSubTitle("Review, print, and save the calibration report.")

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(20, 12, 20, 20)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(18)

        # PASS/FAIL banner with session metadata
        self.result_banner = QFrame(container)
        self.result_banner.setProperty("card", True)
        banner_layout = QVBoxLayout(self.result_banner)
        banner_layout.setContentsMargins(24, 20, 24, 20)
        banner_layout.setSpacing(8)
        self.banner_title = QLabel("PASS")
        self.banner_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.banner_title.setStyleSheet(
            f"font-weight: bold; {TYPOGRAPHY['headline']}"
        )
        banner_layout.addWidget(self.banner_title)
        self.banner_meta = QLabel("")
        self.banner_meta.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.banner_meta.setStyleSheet(
            f"color: {COLORS['text_secondary']}; {TYPOGRAPHY['body']}"
        )
        self.banner_meta.setWordWrap(True)
        banner_layout.addWidget(self.banner_meta)
        layout.addWidget(self.result_banner)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; {TYPOGRAPHY['body']}"
        )
        layout.addWidget(self.summary_label)

        self.browser = QTextBrowser()
        self.browser.setStyleSheet(STYLES.get("readonly_text_edit", ""))
        layout.addWidget(self.browser, 1)

        # Button bar inside a card
        button_card = QFrame(container)
        button_card.setProperty("card", True)
        button_card_layout = QVBoxLayout(button_card)
        button_card_layout.setContentsMargins(24, 20, 24, 20)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)
        self.save_button = QPushButton("Save PDF…")
        self.save_button.setStyleSheet(STYLES.get("action_button", ""))
        self.save_button.clicked.connect(self._save_pdf)
        btn_row.addWidget(self.save_button)
        self.export_csv_button = QPushButton("Export CSV…")
        self.export_csv_button.setStyleSheet(STYLES.get("action_button", ""))
        self.export_csv_button.clicked.connect(self._export_csv)
        btn_row.addWidget(self.export_csv_button)
        self.export_stinger_button = QPushButton("Export Stinger Calibration…")
        self.export_stinger_button.setStyleSheet(STYLES.get("action_button", ""))
        self.export_stinger_button.clicked.connect(self._export_stinger_calibration)
        btn_row.addWidget(self.export_stinger_button)
        self.print_button = QPushButton("Print")
        self.print_button.setStyleSheet(STYLES.get("action_button", ""))
        self.print_button.clicked.connect(self._print_report)
        btn_row.addWidget(self.print_button)
        self.open_folder_button = QPushButton("Open Output Folder")
        self.open_folder_button.setStyleSheet(STYLES.get("action_button", ""))
        self.open_folder_button.clicked.connect(self._open_output_folder)
        btn_row.addWidget(self.open_folder_button)
        btn_row.addStretch(1)
        button_card_layout.addLayout(btn_row)
        self.saved_label = QLabel("")
        self.saved_label.setWordWrap(True)
        self.saved_label.setStyleSheet(
            f"color: {COLORS['success']}; {TYPOGRAPHY['body']}; padding-top: 4px;"
        )
        self.saved_label.setVisible(False)
        button_card_layout.addWidget(self.saved_label)
        layout.addWidget(button_card)

        scroll.setWidget(container)
        outer_layout.addWidget(scroll)

    def initializePage(self) -> None:
        wizard = self.wizard()
        if wizard is None:
            return
        wizard.session.complete()
        self.browser.setHtml(build_report_html(wizard.session, wizard.settings))
        self.summary_label.setText(
            f"Output folder: {wizard.settings.report_output_dir}\n"
            f"Template: {wizard.settings.report_template_path}"
        )
        self.saved_label.setVisible(False)

        # Update PASS/FAIL banner and session metadata
        session = wizard.session
        passed = session.overall_passed
        self.banner_title.setText("PASS" if passed else "FAIL")
        if passed:
            self.result_banner.setStyleSheet(
                f"QFrame {{ background: {COLORS['success_muted']}; "
                f"border: 1px solid {COLORS['success']}; "
                f"border-radius: 12px; }}"
            )
            self.banner_title.setStyleSheet(
                f"color: {COLORS['success']}; font-weight: bold; {TYPOGRAPHY['headline']}"
            )
        else:
            self.result_banner.setStyleSheet(
                f"QFrame {{ background: {COLORS['danger_muted']}; "
                f"border: 1px solid {COLORS['danger']}; "
                f"border-radius: 12px; }}"
            )
            self.banner_title.setStyleSheet(
                f"color: {COLORS['danger']}; font-weight: bold; {TYPOGRAPHY['headline']}"
            )
        started = (
            session.started_at.strftime("%Y-%m-%d %H:%M")
            if session.started_at
            else "—"
        )
        completed = (
            session.completed_at.strftime("%Y-%m-%d %H:%M")
            if session.completed_at
            else "—"
        )
        self.banner_meta.setText(
            f"Technician: {session.technician_name or '—'}  |  "
            f"Asset: {session.asset_id or '—'}  |  "
            f"Started: {started}  |  Completed: {completed}"
        )

    def _save_pdf(self) -> None:
        wizard = self.wizard()
        if wizard is None:
            return
        self.saved_label.setVisible(False)
        start_path = wizard.settings.report_output_dir / default_report_filename(
            wizard.session, wizard.settings
        )
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save PDF Report",
            str(start_path),
            "PDF (*.pdf)",
        )
        if not path:
            return
        try:
            out_path = export_report_pdf(wizard.session, wizard.settings, Path(path))
            wizard.session.last_report_path = out_path
            self.saved_label.setText(f"Saved successfully:\n{out_path}")
            self.saved_label.setVisible(True)
        except Exception as exc:
            QMessageBox.critical(self, "Save Failed", str(exc))

    def _export_stinger_calibration(self) -> None:
        wizard = self.wizard()
        if wizard is None:
            return
        self.saved_label.setVisible(False)
        start_path = wizard.settings.report_output_dir / 'recommended_calibration.yaml'
        path, _ = QFileDialog.getSaveFileName(
            self,
            'Export Stinger Calibration YAML',
            str(start_path),
            'YAML (*.yaml *.yml)',
        )
        if not path:
            return
        merge = QMessageBox.question(
            self,
            'Merge into stinger_config.yaml?',
            f'Also merge models into {get_stinger_config_path()}?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        try:
            out_path = export_recommended_calibration_yaml(
                wizard.session,
                Path(path),
                merge_stinger_path=(
                    get_stinger_config_path()
                    if merge == QMessageBox.StandardButton.Yes
                    else None
                ),
            )
            self.saved_label.setText(f'Calibration exported:\n{out_path}')
            self.saved_label.setVisible(True)
        except Exception as exc:
            QMessageBox.critical(self, 'Export Failed', str(exc))

    def _export_csv(self) -> None:
        wizard = self.wizard()
        if wizard is None:
            return
        self.saved_label.setVisible(False)
        start_path = wizard.settings.report_output_dir / default_csv_filename(
            wizard.session, wizard.settings
        )
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Calibration Data (CSV)",
            str(start_path),
            "CSV (*.csv)",
        )
        if not path:
            return
        try:
            out_path = export_report_csv(wizard.session, wizard.settings, Path(path))
            self.saved_label.setText(f"Exported successfully:\n{out_path}")
            self.saved_label.setVisible(True)
        except Exception as exc:
            QMessageBox.critical(self, "Export Failed", str(exc))

    def _print_report(self) -> None:
        wizard = self.wizard()
        if wizard is None:
            return
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        dialog = QPrintDialog(printer, self)
        if dialog.exec():
            document = build_text_document(wizard.session, wizard.settings)
            document.print(printer)

    def _open_output_folder(self) -> None:
        wizard = self.wizard()
        if wizard is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(wizard.settings.report_output_dir)))
