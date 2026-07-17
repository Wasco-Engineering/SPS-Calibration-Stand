"""Calibration run page for a single port."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QThread
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QWizardPage,
)

from app.services import run_async
from quality_cal.core.calibration_runner import CalibrationRunner
from quality_cal.core.port_calibrator import FinalizeCalibrationResult, finalize_port_calibration
from quality_cal.ui.styles import COLORS, STYLES, TYPOGRAPHY


class CalibrationRunPage(QWizardPage):
    def __init__(self, *, port_id: str, title: str, parent=None) -> None:
        super().__init__(parent)
        self.port_id = port_id
        self.setTitle(title)
        self.setSubTitle("Automatic static pressure calibration with progress updates.")
        self._completed = False
        self._running = False
        self._thread: QThread | None = None
        self._runner: CalibrationRunner | None = None
        self._points_completed = 0
        self._retest_thread: QThread | None = None
        self._retest_runner: CalibrationRunner | None = None
        self._sweep_csv_path: str | None = None
        self._finalize_pending = False

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(20, 12, 20, 20)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        scroll_center = QWidget()
        scroll_center_layout = QHBoxLayout(scroll_center)
        scroll_center_layout.setContentsMargins(0, 0, 0, 0)
        container = QWidget()
        container.setMinimumWidth(640)
        layout = QVBoxLayout(container)
        layout.setSpacing(18)

        # PASS/FAIL banner (hidden until calibration completes)
        self.result_banner = QFrame(container)
        self.result_banner.setProperty("card", True)
        self.result_banner.setVisible(False)
        banner_layout = QVBoxLayout(self.result_banner)
        banner_layout.setContentsMargins(24, 20, 24, 20)
        self.result_banner_label = QLabel()
        self.result_banner_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.result_banner_label.setStyleSheet(
            f"font-weight: bold; {TYPOGRAPHY['headline']}"
        )
        banner_layout.addWidget(self.result_banner_label)
        layout.addWidget(self.result_banner)

        card = QFrame(container)
        card.setProperty("card", True)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(28, 28, 28, 28)
        card_layout.setSpacing(16)

        self.status_label = QLabel("Calibration not started.")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(
            f"color: {COLORS['text_primary']}; {TYPOGRAPHY['subtitle']}"
        )
        card_layout.addWidget(self.status_label)

        # Live pressure readout (updates during hold/settle)
        self.live_readout_label = QLabel("—")
        self.live_readout_label.setStyleSheet(
            f"color: {COLORS['accent_blue']}; {TYPOGRAPHY['subtitle']} font-weight: bold;"
        )
        card_layout.addWidget(self.live_readout_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setStyleSheet(STYLES["progress_bar"])
        card_layout.addWidget(self.progress_bar)

        # Results table: Point, Target, Mensor, Alicat, Transducer, Deviation, Result
        self.results_table = QTableWidget()
        self.results_table.setColumnCount(7)
        self.results_table.setHorizontalHeaderLabels([
            "Point", "Target (psia)", "Mensor", "Alicat", "Transducer",
            "Deviation", "Result",
        ])
        self.results_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.results_table.verticalHeader().setDefaultSectionSize(36)
        self.results_table.setMinimumHeight(380)
        self.results_table.setStyleSheet(STYLES["table_widget"])
        self.results_table.setAlternatingRowColors(True)
        card_layout.addWidget(self.results_table, 1)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; {TYPOGRAPHY['body']}"
        )
        card_layout.addWidget(self.summary_label)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self.start_button = QPushButton("Start Calibration")
        self.start_button.setMinimumSize(280, 58)
        self.start_button.clicked.connect(self._start_run)
        button_row.addWidget(self.start_button)
        button_row.addStretch(1)
        card_layout.addLayout(button_row)

        retest_row = QHBoxLayout()
        retest_row.addStretch(1)
        retest_label = QLabel("Retest point:")
        retest_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; {TYPOGRAPHY['body']}"
        )
        retest_row.addWidget(retest_label)
        self.retest_spin = QSpinBox()
        self.retest_spin.setMinimum(1)
        self.retest_spin.setMaximum(1)
        self.retest_spin.setValue(1)
        self.retest_spin.setMinimumWidth(72)
        retest_row.addWidget(self.retest_spin)
        self.retest_button = QPushButton("Retest point")
        self.retest_button.setMinimumSize(140, 44)
        self.retest_button.clicked.connect(self._retest_point)
        retest_row.addWidget(self.retest_button)
        retest_row.addStretch(1)
        card_layout.addLayout(retest_row)

        layout.addWidget(card, 1)
        scroll_center_layout.addWidget(container, 1)
        scroll.setWidget(scroll_center)
        outer_layout.addWidget(scroll)

    def initializePage(self) -> None:
        wizard = self.wizard()
        if wizard is not None:
            n = len(wizard.settings.pressure_points_psia)
            self.retest_spin.setMaximum(max(1, n))
        if not self._running and not self._completed:
            self._start_run()

    def isComplete(self) -> bool:
        return self._completed and not self._finalize_pending

    def cleanupPage(self) -> None:
        if self._runner is not None:
            self._runner.request_cancel()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(2000)
            self._thread = None
        if self._retest_runner is not None:
            self._retest_runner.request_cancel()
        if self._retest_thread is not None:
            self._retest_thread.quit()
            self._retest_thread.wait(2000)
            self._retest_thread = None

    def _start_run(self) -> None:
        wizard = self.wizard()
        if wizard is None or self._running:
            return
        # Ensure hardware is initialized (e.g. user navigated here before first hardware poll)
        if wizard.port_manager is None or wizard.mensor_reader is None:
            wizard.get_hardware_snapshot()
        if wizard.port_manager is None:
            self._on_failed(
                "Hardware not ready. Go back to Hardware Check and wait for verification."
            )
            return
        if wizard.mensor_reader is None:
            self._on_failed("Mensor is not connected.")
            return

        port = wizard.port_manager.get_port(self.port_id)
        if port is None:
            self._on_failed(f"Port not available: {self.port_id}")
            return

        self._completed = False
        self._running = True
        self._finalize_pending = False
        self._sweep_csv_path = None
        self._points_completed = 0
        self.result_banner.setVisible(False)
        self.progress_bar.setValue(0)
        self.status_label.setText("Starting calibration...")
        self.live_readout_label.setText("—")
        self.summary_label.setText("")
        self.results_table.setRowCount(0)
        self.start_button.setEnabled(False)
        self.retest_button.setEnabled(False)
        self.retest_spin.setEnabled(False)

        self._thread = QThread(self)
        self._runner = CalibrationRunner(
            port_id=self.port_id,
            port=port,
            mensor=wizard.mensor_reader,
            settings=wizard.settings,
        )
        self._runner.moveToThread(self._thread)
        self._thread.started.connect(self._runner.run)
        self._runner.progressChanged.connect(self._on_progress)
        self._runner.liveReadingsUpdated.connect(self._on_live_readings)
        self._runner.pointMeasured.connect(self._on_point_measured)
        self._runner.sweepCsvReady.connect(self._on_sweep_csv_ready)
        self._runner.finished.connect(self._on_finished)
        self._runner.failed.connect(self._on_failed)
        self._runner.cancelled.connect(self._on_cancelled)
        self._runner.finished.connect(self._thread.quit)
        self._runner.failed.connect(self._thread.quit)
        self._runner.cancelled.connect(self._thread.quit)
        self._thread.start()

    def _on_progress(self, percent: int, message: str) -> None:
        self.progress_bar.setValue(percent)
        self.status_label.setText(message)

    def _on_live_readings(
        self,
        mensor_psia: object,
        alicat_psia: object,
        transducer_psia: object,
        target_psia: object = None,
    ) -> None:
        wizard = self.wizard()
        mensor_max = 30.0
        if wizard is not None:
            mensor_max = float(wizard.settings.mensor_max_psia)

        def _fmt_mensor() -> str:
            if mensor_psia is not None:
                try:
                    return f"{float(mensor_psia):.3f}"
                except (TypeError, ValueError):
                    return "—"
            if target_psia is not None:
                try:
                    if float(target_psia) > mensor_max + 1e-6:
                        return f"N/A (>{mensor_max:.0f})"
                except (TypeError, ValueError):
                    pass
            return "—"

        def _fmt(v: object) -> str:
            if v is None:
                return "—"
            try:
                return f"{float(v):.3f}"
            except (TypeError, ValueError):
                return "—"
        parts = [
            f"Mensor: {_fmt_mensor()}",
            f"Alicat: {_fmt(alicat_psia)}",
            f"Transducer: {_fmt(transducer_psia)}",
        ]
        self.live_readout_label.setText("  |  ".join(parts) + "  psia")

    def _on_point_measured(self, result) -> None:
        self._points_completed = result.point_index
        row = self.results_table.rowCount()
        self.results_table.insertRow(row)
        self.results_table.setItem(row, 0, QTableWidgetItem(str(result.point_index)))
        self.results_table.setItem(
            row, 1, QTableWidgetItem(f"{result.target_psia:.2f}")
        )
        mensor_str = f"{result.mensor_psia:.3f}" if result.mensor_psia is not None else "—"
        self.results_table.setItem(row, 2, QTableWidgetItem(mensor_str))
        alicat_str = f"{result.alicat_psia:.3f}" if result.alicat_psia is not None else "—"
        self.results_table.setItem(row, 3, QTableWidgetItem(alicat_str))
        trans_str = (
            f"{result.transducer_psia:.3f}" if result.transducer_psia is not None else "—"
        )
        self.results_table.setItem(row, 4, QTableWidgetItem(trans_str))
        dev_str = (
            f"{result.deviation_psia:+.3f}" if result.deviation_psia is not None else "—"
        )
        self.results_table.setItem(row, 5, QTableWidgetItem(dev_str))
        result_item = QTableWidgetItem("PASS" if result.passed else "FAIL")
        result_item.setForeground(
            QBrush(QColor(COLORS["success"] if result.passed else COLORS["danger"]))
        )
        self.results_table.setItem(row, 6, result_item)
        self.results_table.scrollToBottom()
        self.summary_label.setText(
            f"Points completed: {result.point_index}/{result.point_total}"
        )

    def _on_sweep_csv_ready(self, path: str) -> None:
        self._sweep_csv_path = path

    def _on_finished(self, results) -> None:
        wizard = self.wizard()
        if wizard is not None:
            wizard.session.port_result(self.port_id).points = list(results)
        self._running = False
        self.start_button.setEnabled(True)
        self.retest_button.setEnabled(True)
        self.retest_spin.setEnabled(True)
        self.live_readout_label.setText("Calibration complete.")

        csv_path = self._sweep_csv_path
        if csv_path and wizard is not None:
            self._finalize_pending = True
            self.status_label.setText("Fitting correlation models and rescoring points...")
            self.progress_bar.setValue(95)

            def _finalize() -> object:
                port = wizard.port_manager.get_port(self.port_id) if wizard.port_manager else None
                return finalize_port_calibration(
                    Path(csv_path),
                    self.port_id,
                    list(results),
                    wizard.settings,
                    port=port,
                    apply_to_stinger=True,
                )

            def _on_finalize_done(bundle: object, error: Exception | None) -> None:
                self._finalize_pending = False
                if error is not None:
                    self.summary_label.setText(f"Finalize failed: {error}")
                    self._show_final_banner(results)
                    self._completed = True
                    self.completeChanged.emit()
                    return
                assert isinstance(bundle, FinalizeCalibrationResult)
                wizard.session.port_result(self.port_id).points = bundle.points
                wizard.session.port_result(self.port_id).fit_summary = bundle.fit_summary
                self._refresh_results_table(bundle.points)
                self._show_final_banner(bundle.points)
                self.summary_label.setText(bundle.message)
                self._completed = True
                self.completeChanged.emit()

            run_async(_finalize, _on_finalize_done)
            return

        self._show_final_banner(results)
        self.summary_label.setText(
            f"Calibration complete. Points recorded: {len(results)}. "
            f"Overall: {'PASS' if all(r.passed for r in results) else 'FAIL'}."
        )
        self._completed = True
        self.completeChanged.emit()

    def _refresh_results_table(self, results) -> None:
        self.results_table.setRowCount(0)
        for result in results:
            self._on_point_measured(result)

    def _show_final_banner(self, results) -> None:
        passed = all(r.passed for r in results) if results else False
        self.result_banner.setVisible(True)
        self.result_banner_label.setText("PASS" if passed else "FAIL")
        if passed:
            self.result_banner.setStyleSheet(
                f"QFrame {{ background: {COLORS['success_muted']}; "
                f"border: 1px solid {COLORS['success']}; "
                f"border-radius: 12px; }}"
            )
            self.result_banner_label.setStyleSheet(
                f"color: {COLORS['success']}; font-weight: bold; {TYPOGRAPHY['headline']}"
            )
        else:
            self.result_banner.setStyleSheet(
                f"QFrame {{ background: {COLORS['danger_muted']}; "
                f"border: 1px solid {COLORS['danger']}; "
                f"border-radius: 12px; }}"
            )
            self.result_banner_label.setStyleSheet(
                f"color: {COLORS['danger']}; font-weight: bold; {TYPOGRAPHY['headline']}"
            )

    def _on_failed(self, message: str) -> None:
        self._running = False
        self.start_button.setEnabled(True)
        self.retest_button.setEnabled(True)
        self.retest_spin.setEnabled(True)
        self.status_label.setText("Calibration failed.")
        self.live_readout_label.setText("—")
        self.summary_label.setText(message)
        self.summary_label.setStyleSheet(
            f"color: {COLORS['danger']}; font-weight: bold; {TYPOGRAPHY['body']}"
        )
        self.completeChanged.emit()

    def _on_cancelled(self) -> None:
        self._running = False
        self.start_button.setEnabled(True)
        self.retest_button.setEnabled(True)
        self.retest_spin.setEnabled(True)
        self.status_label.setText("Calibration cancelled.")
        self.live_readout_label.setText("—")
        self.summary_label.setText("")

    def _retest_point(self) -> None:
        wizard = self.wizard()
        if wizard is None or self._running or self._retest_runner is not None:
            return
        if wizard.port_manager is None or wizard.mensor_reader is None:
            wizard.get_hardware_snapshot()
        if wizard.port_manager is None or wizard.mensor_reader is None:
            return
        port = wizard.port_manager.get_port(self.port_id)
        if port is None:
            return
        point_index = self.retest_spin.value()
        n = len(wizard.settings.pressure_points_psia)
        if point_index < 1 or point_index > n:
            return
        self.retest_button.setEnabled(False)
        self.status_label.setText(f"Retesting point {point_index}…")
        self._retest_thread = QThread(self)
        self._retest_runner = CalibrationRunner(
            port_id=self.port_id,
            port=port,
            mensor=wizard.mensor_reader,
            settings=wizard.settings,
        )
        self._retest_runner.moveToThread(self._retest_thread)
        self._retest_thread.started.connect(
            lambda: self._retest_runner.run_single_point(point_index)
        )
        self._retest_runner.progressChanged.connect(self._on_progress)
        self._retest_runner.liveReadingsUpdated.connect(self._on_live_readings)
        self._retest_runner.singlePointDone.connect(self._on_single_point_done)
        self._retest_runner.failed.connect(self._on_retest_failed)
        self._retest_runner.singlePointDone.connect(self._retest_thread.quit)
        self._retest_runner.failed.connect(self._retest_thread.quit)
        self._retest_thread.start()

    def _on_single_point_done(self, result) -> None:
        self._retest_thread = None
        self._retest_runner = None
        self.retest_button.setEnabled(True)
        self.status_label.setText("Calibration not started.")
        wizard = self.wizard()
        if wizard is None:
            return
        port_result = wizard.session.port_result(self.port_id)
        idx = result.point_index - 1
        if 0 <= idx < len(port_result.points):
            port_result.points[idx] = result
        if idx < self.results_table.rowCount():
            self.results_table.setItem(idx, 0, QTableWidgetItem(str(result.point_index)))
            self.results_table.setItem(idx, 1, QTableWidgetItem(f"{result.target_psia:.2f}"))
            mensor_str = f"{result.mensor_psia:.3f}" if result.mensor_psia is not None else "—"
            self.results_table.setItem(idx, 2, QTableWidgetItem(mensor_str))
            alicat_str = f"{result.alicat_psia:.3f}" if result.alicat_psia is not None else "—"
            self.results_table.setItem(idx, 3, QTableWidgetItem(alicat_str))
            trans_str = f"{result.transducer_psia:.3f}" if result.transducer_psia is not None else "—"
            self.results_table.setItem(idx, 4, QTableWidgetItem(trans_str))
            dev_str = f"{result.deviation_psia:+.3f}" if result.deviation_psia is not None else "—"
            self.results_table.setItem(idx, 5, QTableWidgetItem(dev_str))
            result_item = QTableWidgetItem("PASS" if result.passed else "FAIL")
            result_item.setForeground(
                QBrush(QColor(COLORS["success"] if result.passed else COLORS["danger"]))
            )
            self.results_table.setItem(idx, 6, result_item)
        self.summary_label.setText(f"Point {result.point_index} retested: {'PASS' if result.passed else 'FAIL'}.")

    def _on_retest_failed(self, message: str) -> None:
        self._retest_thread = None
        self._retest_runner = None
        self.retest_button.setEnabled(True)
        self.status_label.setText("Calibration not started.")
        self.summary_label.setText(f"Retest failed: {message}")
