"""Custom single-window shell for quality calibration."""

from __future__ import annotations

import logging
from typing import Any

from pathlib import Path

from PyQt6.QtCore import Qt, QThread
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.hardware.port import PortManager
from app.services import run_async
from quality_cal.config import QualitySettings, parse_quality_settings
from quality_cal.core.calibration_runner import CalibrationRunner
from quality_cal.core.port_calibrator import (
    FinalizeCalibrationResult,
    finalize_port_calibration,
)
from quality_cal.core.hardware_discovery import (
    discover_alicat_assignments,
    discover_labjack_target,
    discover_mensor_port,
)
from quality_cal.core.hardware_checks import evaluate_labjack_port_check
from quality_cal.core.leak_check_runner import LeakCheckRunner
from quality_cal.core.mensor_reader import MensorReader
from quality_cal.session import CalibrationPointResult, QualityCalibrationSession
from quality_cal.ui.models import HardwareSnapshot, HardwareStatusEntry, WorkflowStage
from quality_cal.ui.styles import APP_STYLESHEET, COLORS
from quality_cal.ui.views import ConfirmMensorPanel, ReportPanel, RunPanel, SetupPanel, WorkflowRail

logger = logging.getLogger(__name__)


class QualityCalibrationWindow(QMainWindow):
    """Full-window quality calibration shell."""

    def __init__(self, *, config: dict, settings: QualitySettings, preview_mode: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.config = config
        self.settings = settings
        self._preview_mode = preview_mode
        self.session = QualityCalibrationSession()
        self.port_manager: PortManager | None = None
        self.mensor_reader: MensorReader | None = None
        self._labjack_probe_detail = 'LabJack discovery not yet run.'
        self._discovery_applied = False
        self._hardware_snapshot: HardwareSnapshot | None = None
        self._stages: list[WorkflowStage] = []
        self._completed_stage_keys: set[str] = set()
        self._stage_widgets: dict[str, QWidget] = {}
        self._current_stage_index = 0
        self._thread: QThread | None = None
        self._runner: CalibrationRunner | LeakCheckRunner | None = None
        self._retest_thread: QThread | None = None
        self._retest_runner: CalibrationRunner | None = None
        self._pending_sweep_csv: dict[str, str] = {}

        self.setWindowTitle('Quality Calibration')
        self.setMinimumSize(1360, 860)
        self.setStyleSheet(APP_STYLESHEET)

        self._build_shell()
        self._set_stages(self._build_workflow_stages(include_leak_check=False))
        self._show_stage(0)
        setup = self._stage_widgets.get('setup')
        if isinstance(setup, SetupPanel):
            setup.ensure_hardware_refresh()

    def _build_shell(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        header = QFrame()
        header.setProperty('panelRole', 'chrome')
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 12, 16, 12)
        header_layout.setSpacing(12)

        title_col = QVBoxLayout()
        title_col.setContentsMargins(0, 0, 0, 0)
        title_col.setSpacing(6)
        eyebrow = QLabel('QUALITY CALIBRATION')
        eyebrow.setProperty('role', 'eyebrow')
        title_col.addWidget(eyebrow)

        self.title_label = QLabel('Session setup')
        self.title_label.setProperty('textRole', 'hero')
        self.title_label.setWordWrap(True)
        title_col.addWidget(self.title_label)

        self.description_label = QLabel('Prepare the station, verify hardware, and begin the session.')
        self.description_label.setProperty('textRole', 'body')
        self.description_label.setWordWrap(True)
        title_col.addWidget(self.description_label)
        header_layout.addLayout(title_col, 1)

        status_col = QVBoxLayout()
        status_col.setContentsMargins(0, 0, 0, 0)
        status_col.setSpacing(8)
        self.session_label = QLabel('Technician: --  |  Asset: --')
        self.session_label.setProperty('textRole', 'body')
        status_col.addWidget(self.session_label)
        self.status_summary_label = QLabel('Hardware not checked yet.')
        self.status_summary_label.setProperty('textRole', 'muted')
        self.status_summary_label.setWordWrap(True)
        status_col.addWidget(self.status_summary_label)
        header_layout.addLayout(status_col, 0)
        root.addWidget(header)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(8)
        root.addLayout(body, 1)

        rail_frame = QFrame()
        rail_frame.setProperty('panelRole', 'rail')
        rail_layout = QVBoxLayout(rail_frame)
        rail_layout.setContentsMargins(10, 10, 10, 10)
        rail_layout.setSpacing(8)
        rail_title = QLabel('Workflow')
        rail_title.setStyleSheet(
            'color: #f8fafc; font-size: 13px; font-weight: 700; background: transparent;'
        )
        rail_layout.addWidget(rail_title)
        self.workflow_rail = WorkflowRail()
        rail_layout.addWidget(self.workflow_rail, 1)
        rail_frame.setFixedWidth(248)
        body.addWidget(rail_frame, 0)

        self.stack = QStackedWidget()
        body.addWidget(self.stack, 1)

        footer = QFrame()
        footer.setProperty('panelRole', 'footer')
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(16, 10, 16, 10)
        footer_layout.setSpacing(8)
        self.back_button = QPushButton('Back')
        self.back_button.clicked.connect(self._go_back)
        footer_layout.addWidget(self.back_button)
        footer_layout.addStretch(1)
        self.next_button = QPushButton('Next')
        self.next_button.setObjectName('primaryButton')
        self.next_button.clicked.connect(self._go_next)
        footer_layout.addWidget(self.next_button)
        self.close_button = QPushButton('Close')
        self.close_button.clicked.connect(self.close)
        footer_layout.addWidget(self.close_button)
        root.addWidget(footer)

    def _build_workflow_stages(self, *, include_leak_check: bool) -> list[WorkflowStage]:
        stages = [
            WorkflowStage(
                key='setup',
                title='Setup and hardware verification',
                description='Enter the session details and verify all connected hardware.',
                kind='setup',
            ),
        ]
        if include_leak_check:
            stages.append(
                WorkflowStage(
                    key='left_leak',
                    title='Left port leak check',
                    description='Run the optional leak-check hold on the left port.',
                    kind='leak',
                    port_id='port_a',
                )
            )
        stages.append(
            WorkflowStage(
                key='confirm_left',
                title='Confirm Mensor — left port',
                description='Connect the Mensor to the left port before calibration.',
                kind='confirm',
            )
        )
        stages.append(
            WorkflowStage(
                key='left_calibration',
                title='Left port calibration',
                description='Run the left-port calibration sweep and review the point results.',
                kind='calibration',
                port_id='port_a',
            )
        )
        stages.append(
            WorkflowStage(
                key='confirm_right',
                title='Confirm Mensor — right port',
                description='Move the Mensor to the right port before calibration.',
                kind='confirm',
            )
        )
        if include_leak_check:
            stages.append(
                WorkflowStage(
                    key='right_leak',
                    title='Right port leak check',
                    description='Run the optional leak-check hold on the right port.',
                    kind='leak',
                    port_id='port_b',
                )
            )
        stages.append(
            WorkflowStage(
                key='right_calibration',
                title='Right port calibration',
                description='Run the right-port calibration sweep and review the point results.',
                kind='calibration',
                port_id='port_b',
            )
        )
        stages.append(
            WorkflowStage(
                key='report',
                title='Final report',
                description='Review the completed session and export the report package.',
                kind='report',
            )
        )
        return stages

    def _wrap_stage_widget(self, widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(widget)
        return scroll

    def _set_stages(self, stages: list[WorkflowStage]) -> None:
        self._stages = stages
        self._stage_widgets = {}
        while self.stack.count():
            current = self.stack.widget(0)
            self.stack.removeWidget(current)
            current.deleteLater()

        for stage in self._stages:
            widget = self._create_stage_widget(stage)
            self._stage_widgets[stage.key] = widget
            self.stack.addWidget(self._wrap_stage_widget(widget))

        self.workflow_rail.set_stages(self._stages, self._current_stage_index, self._completed_stage_keys)

    def _create_stage_widget(self, stage: WorkflowStage) -> QWidget:
        if stage.kind == 'setup':
            panel = SetupPanel(self, config=self.config, auto_refresh=not self._preview_mode)
            panel.set_session_values(
                self.session.technician_name,
                self.session.asset_id,
                self.session.include_leak_check,
            )
            panel.refresh_requested.connect(self.refresh_hardware_snapshot)
            panel.submit_requested.connect(self._handle_setup_submit)
            if self._hardware_snapshot is not None:
                panel.set_hardware_snapshot(self._hardware_snapshot)
            return panel

        if stage.kind in {'leak', 'calibration'}:
            panel = RunPanel(self)
            panel.configure(stage, mensor_max_psia=self.settings.mensor_max_psia)
            panel.set_fit_max_psia(self.settings.fit_max_psia)
            panel.set_alicat_fit_max_psia(self.settings.alicat_fit_max_psia)
            panel.start_requested.connect(lambda stage_key=stage.key: self._start_stage_run(stage_key))
            panel.retest_requested.connect(
                lambda point_index, stage_key=stage.key: self._start_retest(stage_key, point_index)
            )
            return panel

        if stage.kind == 'confirm':
            port_label = 'Left port' if 'left' in stage.key else 'Right port'
            panel = ConfirmMensorPanel(port_label=port_label, parent=self)
            panel.confirm_requested.connect(
                lambda _checked=False, key=stage.key: self._on_confirm_port(key),
            )
            return panel

        panel = ReportPanel(self)
        return panel

    def _show_stage(self, index: int) -> None:
        self._current_stage_index = max(0, min(index, len(self._stages) - 1))
        self.stack.setCurrentIndex(self._current_stage_index)
        stage = self._stages[self._current_stage_index]
        self.title_label.setText(stage.title)
        self.description_label.setText(stage.description)
        self.back_button.setEnabled(self._current_stage_index > 0)
        self.next_button.setVisible(stage.kind != 'report')
        self._update_next_button(stage)
        self.workflow_rail.set_stages(self._stages, self._current_stage_index, self._completed_stage_keys)
        self._refresh_header_summary()

        if stage.kind == 'confirm':
            self._completed_stage_keys.discard(stage.key)
            confirm_panel = self._stage_widgets[stage.key]
            assert isinstance(confirm_panel, ConfirmMensorPanel)
            confirm_panel.reset()
            port = str(self.config.get('hardware', {}).get('mensor', {}).get('port', '')).strip()
            confirm_panel.set_port_text(port)
            self._update_next_button(stage)
        elif stage.kind == 'setup':
            setup_panel = self._stage_widgets.get(stage.key)
            if isinstance(setup_panel, SetupPanel):
                setup_panel.ensure_hardware_refresh()
        elif stage.kind == 'report':
            if self.session.completed_at is None:
                self.session.complete()
            if not self._preview_mode:
                self._export_session_certificates()
            report_panel = self._stage_widgets[stage.key]
            assert isinstance(report_panel, ReportPanel)
            report_panel.render(self.session, self.settings)

    def _update_next_button(self, stage: WorkflowStage) -> None:
        if stage.kind == 'setup':
            self.next_button.setEnabled(False)
        elif stage.kind == 'confirm':
            self.next_button.setEnabled(stage.key in self._completed_stage_keys)
        else:
            self.next_button.setEnabled(stage.key in self._completed_stage_keys)

    def _go_back(self) -> None:
        if self._current_stage_index > 0:
            self._show_stage(self._current_stage_index - 1)

    def _go_next(self) -> None:
        if self._current_stage_index >= len(self._stages) - 1:
            return
        current_key = self._stages[self._current_stage_index].key
        if current_key not in self._completed_stage_keys:
            return
        self._show_stage(self._current_stage_index + 1)

    def _mark_stage_complete(self, stage_key: str) -> None:
        self._completed_stage_keys.add(stage_key)
        if self._stages[self._current_stage_index].key == stage_key:
            self._update_next_button(self._stages[self._current_stage_index])
        self.workflow_rail.set_stages(self._stages, self._current_stage_index, self._completed_stage_keys)

    def _export_session_certificates(self) -> None:
        try:
            from PyQt6.QtWidgets import QApplication

            from quality_cal.core.qf87_certificate import (
                export_certificate_bundle,
                load_equipment_id_from_stinger_config,
            )

            if QApplication.instance() is None:
                import sys

                QApplication(sys.argv)
            equipment_id = load_equipment_id_from_stinger_config()
            paths = export_certificate_bundle(
                self.session,
                self.settings,
                equipment_id=equipment_id,
            )
            self.session.last_certificate_docx = None
            self.session.last_certificate_pdf = paths.get('pdf')
            if paths.get('pdf'):
                self.session.last_report_path = paths['pdf']
        except Exception as exc:
            logger.exception('Certificate export failed')
            QMessageBox.warning(
                self,
                'Certificate export',
                f'Could not write QF87 certificate to Desktop:\n{exc}\n\n'
                'Use Save PDF on the report screen for the HTML technical report.',
            )

    def _refresh_header_summary(self) -> None:
        technician = self.session.technician_name or '--'
        asset = self.session.asset_id or '--'
        self.session_label.setText(f'Technician: {technician}  |  Asset: {asset}')
        if self._hardware_snapshot is None:
            self.status_summary_label.setText('Hardware not checked yet.')
            return
        self.status_summary_label.setText(self._hardware_snapshot.summary)

    def _handle_setup_submit(self, payload: dict[str, Any]) -> None:
        profile_id = str(payload.get('profile_id', ''))
        self.settings = parse_quality_settings(self.config, profile_id=profile_id or None)
        self.session = QualityCalibrationSession(
            technician_name=payload['technician_name'],
            asset_id=payload['asset_id'],
            include_leak_check=bool(payload['include_leak_check']),
            profile_id=self.settings.profile_id,
            profile_label=self.settings.profile_label,
        )
        self.session.begin()
        self._completed_stage_keys = {'setup'}
        self._set_stages(self._build_workflow_stages(include_leak_check=self.session.include_leak_check))
        self._show_stage(1)

    def _on_confirm_port(self, stage_key: str) -> None:
        self._mark_stage_complete(stage_key)

    def refresh_hardware_snapshot(self) -> None:
        # Allow serial re-discovery and Mensor reconnect after a transient failure.
        self._discovery_applied = False
        if self.mensor_reader is not None and self.mensor_reader.status not in {
            'Connected',
            'Connected (simulated)',
        }:
            self.mensor_reader.close()
            self.mensor_reader = None

        widget = self._stage_widgets.get('setup')
        if isinstance(widget, SetupPanel):
            widget.set_busy(True)

        def _load_snapshot() -> HardwareSnapshot:
            return self.get_hardware_snapshot()

        def _on_done(result: Any, error: Exception | None) -> None:
            if isinstance(widget, SetupPanel):
                widget.set_busy(False)
            if error is not None:
                QMessageBox.critical(self, 'Hardware Check Failed', str(error))
                return
            self._hardware_snapshot = result
            if isinstance(widget, SetupPanel):
                widget.set_hardware_snapshot(result)
            self._refresh_header_summary()

        run_async(_load_snapshot, _on_done)

    def _stage_by_key(self, stage_key: str) -> WorkflowStage:
        for stage in self._stages:
            if stage.key == stage_key:
                return stage
        raise KeyError(stage_key)

    def _run_panel_for(self, stage_key: str) -> RunPanel:
        panel = self._stage_widgets[stage_key]
        if not isinstance(panel, RunPanel):
            raise TypeError(f'Stage {stage_key} is not a run panel.')
        return panel

    def _start_stage_run(self, stage_key: str) -> None:
        if self._thread is not None or self._runner is not None:
            return
        stage = self._stage_by_key(stage_key)
        panel = self._run_panel_for(stage_key)
        if stage.port_id is None:
            return

        if self.port_manager is None or (stage.kind == 'calibration' and self.mensor_reader is None):
            self._hardware_snapshot = self.get_hardware_snapshot()
            setup_panel = self._stage_widgets.get('setup')
            if isinstance(setup_panel, SetupPanel):
                setup_panel.set_hardware_snapshot(self._hardware_snapshot)

        if self.port_manager is None:
            panel.show_error('Hardware is not ready. Return to setup and rerun hardware verification.')
            return
        if stage.kind == 'calibration' and self.mensor_reader is None:
            panel.show_error('Mensor is not connected.')
            return

        port = self.port_manager.get_port(stage.port_id)
        if port is None:
            panel.show_error(f'Port not available: {stage.port_id}')
            return

        panel.configure(stage, mensor_max_psia=self.settings.mensor_max_psia)
        panel.set_running(True)
        panel.set_progress(0, 'Preparing run...')
        panel.set_ready_message('Run started by operator. Holding status will update live below.')

        self._thread = QThread(self)
        if stage.kind == 'leak':
            runner = LeakCheckRunner(port_id=stage.port_id, port=port, settings=self.settings)
            self._runner = runner
            runner.moveToThread(self._thread)
            self._thread.started.connect(runner.run)
            runner.progressChanged.connect(panel.set_progress)
            runner.sampleData.connect(
                lambda elapsed_s, alicat_psia, transducer_psia, p=panel: p.set_live_readings(
                    elapsed_s=elapsed_s,
                    alicat_psia=alicat_psia if alicat_psia != 0 else None,
                    transducer_psia=transducer_psia if transducer_psia != 0 else None,
                )
            )
            runner.finished.connect(lambda result, key=stage_key: self._on_leak_finished(key, result))
            runner.failed.connect(lambda message, key=stage_key: self._on_stage_failed(key, message))
            runner.cancelled.connect(lambda key=stage_key: self._on_stage_cancelled(key))
            runner.finished.connect(self._thread.quit)
            runner.failed.connect(self._thread.quit)
            runner.cancelled.connect(self._thread.quit)
        else:
            runner = CalibrationRunner(
                port_id=stage.port_id,
                port=port,
                mensor=self.mensor_reader,
                settings=self.settings,
            )
            self._runner = runner
            runner.moveToThread(self._thread)
            self._thread.started.connect(runner.run)
            runner.progressChanged.connect(panel.set_progress)
            runner.liveReadingsUpdated.connect(
                lambda mensor_psia, alicat_psia, transducer_psia, target_psia, p=panel: p.set_live_readings(
                    mensor_psia=mensor_psia,
                    alicat_psia=alicat_psia,
                    transducer_psia=transducer_psia,
                    target_psia=target_psia,
                    mensor_max_psia=self.settings.mensor_max_psia,
                )
            )
            runner.pointMeasured.connect(panel.append_point_result)
            runner.sweepCsvReady.connect(
                lambda path, key=stage_key: self._pending_sweep_csv.__setitem__(key, path),
            )
            runner.mensorDisconnectRequired.connect(
                self._on_mensor_disconnect_required,
                Qt.ConnectionType.QueuedConnection,
            )
            runner.finished.connect(lambda results, key=stage_key: self._on_calibration_finished(key, results))
            runner.failed.connect(lambda message, key=stage_key: self._on_stage_failed(key, message))
            runner.cancelled.connect(lambda key=stage_key: self._on_stage_cancelled(key))
            runner.finished.connect(self._thread.quit)
            runner.failed.connect(self._thread.quit)
            runner.cancelled.connect(self._thread.quit)

        self._thread.finished.connect(self._clear_primary_worker)
        self._thread.start()

    def _start_retest(self, stage_key: str, point_index: int) -> None:
        stage = self._stage_by_key(stage_key)
        if stage.kind != 'calibration' or stage.port_id is None:
            return
        if self._thread is not None or self._runner is not None or self._retest_thread is not None:
            return
        if self.port_manager is None or self.mensor_reader is None:
            return
        port = self.port_manager.get_port(stage.port_id)
        if port is None:
            return
        panel = self._run_panel_for(stage_key)
        panel.set_running(True)
        panel.set_progress(0, f'Retesting point {point_index}...')

        self._retest_thread = QThread(self)
        runner = CalibrationRunner(
            port_id=stage.port_id,
            port=port,
            mensor=self.mensor_reader,
            settings=self.settings,
        )
        self._retest_runner = runner
        runner.moveToThread(self._retest_thread)
        self._retest_thread.started.connect(lambda: runner.run_single_point(point_index))
        runner.progressChanged.connect(panel.set_progress)
        runner.liveReadingsUpdated.connect(
            lambda mensor_psia, alicat_psia, transducer_psia, target_psia, p=panel: p.set_live_readings(
                mensor_psia=mensor_psia,
                alicat_psia=alicat_psia,
                transducer_psia=transducer_psia,
                target_psia=target_psia,
                mensor_max_psia=self.settings.mensor_max_psia,
            )
        )
        runner.singlePointDone.connect(lambda result, key=stage_key: self._on_retest_done(key, result))
        runner.failed.connect(lambda message, key=stage_key: self._on_retest_failed(key, message))
        runner.cancelled.connect(lambda key=stage_key: self._on_stage_cancelled(key))
        runner.singlePointDone.connect(self._retest_thread.quit)
        runner.failed.connect(self._retest_thread.quit)
        runner.cancelled.connect(self._retest_thread.quit)
        self._retest_thread.finished.connect(self._clear_retest_worker)
        self._retest_thread.start()

    def _on_mensor_disconnect_required(self, target_psia: float) -> None:
        QMessageBox.information(
            self,
            'Disconnect Mensor',
            (
                f'Target is {target_psia:.0f} PSIA.\n\n'
                'If your procedure requires disconnecting the Mensor at high pressure, '
                'do so now, then click OK to continue.'
            ),
        )
        runner = self._runner
        if isinstance(runner, CalibrationRunner):
            runner.acknowledge_mensor_disconnect()

    def _on_calibration_finished(self, stage_key: str, results: list[CalibrationPointResult]) -> None:
        stage = self._stage_by_key(stage_key)
        port_id = stage.port_id
        if port_id is not None:
            self.session.port_result(port_id).points = list(results)
        panel = self._run_panel_for(stage_key)

        csv_path = self._pending_sweep_csv.pop(stage_key, None)
        if not csv_path or port_id is None:
            panel.show_calibration_result(list(results))
            self._mark_stage_complete(stage_key)
            return

        panel.set_running(True)
        panel.set_progress(95, 'Fitting correlation models and rescoring points...')

        def _finalize() -> object:
            port = self.port_manager.get_port(port_id) if self.port_manager else None
            return finalize_port_calibration(
                csv_path,
                port_id,
                list(results),
                self.settings,
                port=port,
                apply_to_stinger=True,
            )

        def _on_finalize_done(bundle: object, error: Exception | None) -> None:
            panel.set_running(False)
            if error is not None:
                panel.show_error(f'Post-calibration finalize failed: {error}')
                panel.show_calibration_result(list(results))
                self._mark_stage_complete(stage_key)
                return

            assert isinstance(bundle, FinalizeCalibrationResult)
            self.session.port_result(port_id).points = bundle.points
            self.session.port_result(port_id).fit_summary = bundle.fit_summary
            panel.set_results_table(bundle.points)
            panel.show_calibration_result(bundle.points)
            panel.set_fit_summary(bundle.message, applied=bundle.fit_summary.applied_to_stinger_config)
            self._mark_stage_complete(stage_key)

        run_async(_finalize, _on_finalize_done)

    def _on_leak_finished(self, stage_key: str, result) -> None:
        stage = self._stage_by_key(stage_key)
        if stage.port_id is not None:
            self.session.port_result(stage.port_id).leak_check = result
        panel = self._run_panel_for(stage_key)
        panel.update_leak_summary(result)
        self._mark_stage_complete(stage_key)

    def _on_stage_failed(self, stage_key: str, message: str) -> None:
        self._run_panel_for(stage_key).show_error(message)

    def _on_stage_cancelled(self, stage_key: str) -> None:
        self._run_panel_for(stage_key).show_error('Run cancelled.')

    def _on_retest_done(self, stage_key: str, result: CalibrationPointResult) -> None:
        stage = self._stage_by_key(stage_key)
        if stage.port_id is not None:
            port_result = self.session.port_result(stage.port_id)
            index = max(0, result.point_index - 1)
            if index < len(port_result.points):
                port_result.points[index] = result
            else:
                port_result.points.append(result)
        panel = self._run_panel_for(stage_key)
        panel.replace_point_result(result)
        panel.set_running(False)

    def _on_retest_failed(self, stage_key: str, message: str) -> None:
        panel = self._run_panel_for(stage_key)
        panel.show_error(f'Retest failed: {message}')

    def _clear_primary_worker(self) -> None:
        self._thread = None
        self._runner = None

    def _clear_retest_worker(self) -> None:
        self._retest_thread = None
        self._retest_runner = None

    def serial_auto_discovery_enabled(self) -> bool:
        quality_cfg = self.config.get('quality', {})
        discovery_cfg = quality_cfg.get('hardware_discovery', {}) or {}
        return bool(discovery_cfg.get('enable_serial_auto_discovery', True))

    def _apply_discovered_hardware_assignments(self) -> None:
        if self._discovery_applied:
            return

        hardware_cfg = self.config.setdefault('hardware', {})
        labjack_cfg = hardware_cfg.setdefault('labjack', {})
        alicat_cfg = hardware_cfg.setdefault('alicat', {})
        port_a_cfg = alicat_cfg.setdefault('port_a', {})
        port_b_cfg = alicat_cfg.setdefault('port_b', {})
        mensor_cfg = hardware_cfg.setdefault('mensor', {})
        changed = False

        labjack_probe = discover_labjack_target(self.config)
        self._labjack_probe_detail = str(labjack_probe.get('detail', 'LabJack discovery unavailable.'))
        if bool(labjack_probe.get('found', False)):
            desired_device = str(labjack_probe.get('device_type', labjack_cfg.get('device_type', 'T7')))
            desired_connection = str(
                labjack_probe.get('connection_type', labjack_cfg.get('connection_type', 'USB'))
            )
            desired_identifier = str(labjack_probe.get('identifier', labjack_cfg.get('identifier', 'ANY')))
            if str(labjack_cfg.get('device_type', '')).strip() != desired_device:
                labjack_cfg['device_type'] = desired_device
                changed = True
            if str(labjack_cfg.get('connection_type', '')).strip() != desired_connection:
                labjack_cfg['connection_type'] = desired_connection
                changed = True
            if str(labjack_cfg.get('identifier', '')).strip() != desired_identifier:
                labjack_cfg['identifier'] = desired_identifier
                changed = True

        if self.serial_auto_discovery_enabled():
            discovered_alicats = discover_alicat_assignments(self.config)
            for logical_port, discovered_port in discovered_alicats.items():
                target_cfg = port_a_cfg if logical_port == 'port_a' else port_b_cfg
                if str(target_cfg.get('com_port', '')).strip() != discovered_port:
                    target_cfg['com_port'] = discovered_port
                    changed = True

            discovered_mensor = discover_mensor_port(
                self.config,
                exclude_ports={
                    str(port_a_cfg.get('com_port', '')).strip(),
                    str(port_b_cfg.get('com_port', '')).strip(),
                },
            )
            if discovered_mensor and str(mensor_cfg.get('port', '')).strip() != discovered_mensor:
                mensor_cfg['port'] = discovered_mensor
                changed = True

        if changed:
            self.cleanup_hardware()
        self._discovery_applied = True

    def _ensure_mensor_reader(self) -> None:
        mensor_cfg = self.config.get('hardware', {}).get('mensor', {})
        if self.mensor_reader is None:
            self.mensor_reader = MensorReader(mensor_cfg)
            self.mensor_reader.connect()
            return
        if self.mensor_reader.status in {'Connected', 'Connected (simulated)'}:
            return
        self.mensor_reader.close()
        self.mensor_reader = MensorReader(mensor_cfg)
        self.mensor_reader.connect()

    def get_hardware_snapshot(self) -> HardwareSnapshot:
        self._apply_discovered_hardware_assignments()

        if self.port_manager is None:
            self.port_manager = PortManager(self.config)
            self.port_manager.initialize_ports()
            self.port_manager.connect_all()

        self._ensure_mensor_reader()

        entries: list[HardwareStatusEntry] = []
        overall_ok = True

        for port_id, label in (('port_a', 'Left'), ('port_b', 'Right')):
            port = self.port_manager.get_port(port_id)
            if port is None:
                overall_ok = False
                entries.append(
                    HardwareStatusEntry(
                        name=f'{port_id}_hardware',
                        label=f'{label} hardware',
                        ok=False,
                        detail='Port is not configured.',
                    )
                )
                continue

            labjack_result = evaluate_labjack_port_check(
                port_id=port_id,
                port=port,
                config=self.config,
                probe_detail=self._labjack_probe_detail,
            )
            overall_ok = overall_ok and labjack_result.ok
            entries.append(
                HardwareStatusEntry(
                    name=f'{port_id}_labjack',
                    label=f'{label} LabJack',
                    ok=labjack_result.ok,
                    detail=labjack_result.detail,
                )
            )

            alicat_status = port.alicat.get_status()
            alicat_reading = port.alicat.read_status()
            if alicat_reading is None and not bool(alicat_status.get('connected', False)):
                port.alicat.connect()
                alicat_status = port.alicat.get_status()
                alicat_reading = port.alicat.read_status()
            alicat_ok = alicat_reading is not None
            overall_ok = overall_ok and alicat_ok
            entries.append(
                HardwareStatusEntry(
                    name=f'{port_id}_alicat',
                    label=f'{label} Alicat',
                    ok=alicat_ok,
                    detail=(
                        f"{alicat_status.get('status', 'Unknown')} | "
                        f"Port={alicat_status.get('port')} Address={alicat_status.get('address')}"
                        if alicat_reading is None
                        else f"{alicat_status.get('status', 'Unknown')} | "
                        f"Pressure={alicat_reading.pressure:.3f} psia Setpoint={alicat_reading.setpoint:.3f}"
                    ),
                )
            )

        mensor_ok = False
        mensor_detail = self.mensor_reader.status if self.mensor_reader is not None else 'Not initialized'
        if self.mensor_reader is not None:
            if self.mensor_reader.status in {'Connected', 'Connected (simulated)'}:
                try:
                    reading = self.mensor_reader.read_pressure()
                    mensor_ok = True
                    mensor_detail = f'{self.mensor_reader.status} | Pressure={reading.pressure_psia:.3f} psia'
                except Exception as exc:
                    mensor_detail = f'{self.mensor_reader.status} | Read failed: {exc}'
        overall_ok = overall_ok and mensor_ok
        entries.append(
            HardwareStatusEntry(
                name='mensor',
                label='Mensor',
                ok=mensor_ok,
                detail=mensor_detail,
            )
        )

        ready_count = sum(1 for entry in entries if entry.ok)
        return HardwareSnapshot(
            overall_ok=overall_ok,
            summary=f'{ready_count}/{len(entries)} hardware checks passing.',
            discovery_note=self._labjack_probe_detail,
            entries=tuple(entries),
        )

    def cleanup_hardware(self) -> None:
        if self.mensor_reader is not None:
            self.mensor_reader.close()
            self.mensor_reader = None
        if self.port_manager is not None:
            self.port_manager.disconnect_all()
            self.port_manager = None
        self._discovery_applied = False

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._thread is not None and self._runner is not None:
            self._runner.request_cancel()
            self._thread.quit()
            self._thread.wait(2000)
        if self._retest_thread is not None and self._retest_runner is not None:
            self._retest_runner.request_cancel()
            self._retest_thread.quit()
            self._retest_thread.wait(2000)

        if (
            not self._preview_mode
            and self.session.started_at is not None
            and self._stages[self._current_stage_index].kind != 'report'
        ):
            reply = QMessageBox.question(
                self,
                'Exit Quality Calibration',
                'Are you sure you want to exit?\n\nAny unsaved progress will be lost.',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

        self.cleanup_hardware()
        event.accept()

    def workflow_stage_keys(self) -> list[str]:
        return [stage.key for stage in self._stages]

    def current_stage_key(self) -> str:
        return self._stages[self._current_stage_index].key

    def select_stage(self, stage_key: str) -> None:
        for index, stage in enumerate(self._stages):
            if stage.key == stage_key:
                self._show_stage(index)
                return
        raise KeyError(stage_key)
