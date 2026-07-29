#!/usr/bin/env python3
"""Small dual-port setpoint UI with live pressure logging.

Use for manual Mensor/Alicat/transducer checks when Quality Cal is not running.

  python scripts/manual_setpoint_logger_gui.py
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from app.core.config import load_config, setup_logging
from app.core.paths import get_logs_dir
from app.hardware.port import PortId, PortManager, PortReading
from app.services.measurement_source import (
    _alicat_pressure_abs_psi,
    _transducer_pressure_abs_psi,
)
from app.services.pressure_domain import infer_barometric_pressure, is_plausible_barometric_psi
from app.services.ptp_service import convert_pressure


def _fmt(value: Optional[float], decimals: int = 3, suffix: str = '') -> str:
    if value is None:
        return '--'
    return f'{value:.{decimals}f}{suffix}'


def _torr(psi: Optional[float]) -> Optional[float]:
    if psi is None:
        return None
    return float(convert_pressure(float(psi), 'PSI', 'Torr'))


class PortControlPanel(QGroupBox):
    """Setpoint controls + live readout for one port."""

    def __init__(self, port_id: PortId, parent: Optional[QWidget] = None) -> None:
        super().__init__(port_id.value.replace('_', ' ').upper(), parent)
        self.port_id = port_id

        layout = QGridLayout(self)
        row = 0

        layout.addWidget(QLabel('Setpoint (PSIA)'), row, 0)
        self.setpoint_spin = QDoubleSpinBox()
        self.setpoint_spin.setRange(0.0, 120.0)
        self.setpoint_spin.setDecimals(3)
        self.setpoint_spin.setSingleStep(0.1)
        self.setpoint_spin.setValue(14.7)
        layout.addWidget(self.setpoint_spin, row, 1)
        row += 1

        btn_row = QHBoxLayout()
        self.apply_btn = QPushButton('Apply SP')
        self.atm_btn = QPushButton('Atmosphere / EXH')
        self.vac_btn = QPushButton('Vacuum route')
        btn_row.addWidget(self.apply_btn)
        btn_row.addWidget(self.atm_btn)
        btn_row.addWidget(self.vac_btn)
        layout.addLayout(btn_row, row, 0, 1, 2)
        row += 1

        self.labels: dict[str, QLabel] = {}
        for title, key in (
            ('Alicat PSIA', 'alicat_psia'),
            ('Alicat Torr', 'alicat_torr'),
            ('Transducer PSIA', 'xducer_psia'),
            ('Transducer Torr', 'xducer_torr'),
            ('A−X (Torr)', 'delta_torr'),
            ('Cmd SP', 'cmd_sp'),
            ('Route', 'route'),
        ):
            layout.addWidget(QLabel(f'{title}:'), row, 0)
            value = QLabel('--')
            value.setStyleSheet('font-family: Consolas, monospace; font-size: 14px;')
            layout.addWidget(value, row, 1)
            self.labels[key] = value
            row += 1

        self._route_text = '—'

    @property
    def route_text(self) -> str:
        return self._route_text

    def set_route(self, text: str) -> None:
        self._route_text = text
        self.labels['route'].setText(text)

    def update_reading(
        self,
        *,
        alicat_psia: Optional[float],
        xducer_psia: Optional[float],
        cmd_sp: Optional[float],
    ) -> None:
        delta = None
        if alicat_psia is not None and xducer_psia is not None:
            delta = _torr(alicat_psia) - _torr(xducer_psia)
        self.labels['alicat_psia'].setText(_fmt(alicat_psia, 4, ' PSIA'))
        self.labels['alicat_torr'].setText(_fmt(_torr(alicat_psia), 1, ' Torr'))
        self.labels['xducer_psia'].setText(_fmt(xducer_psia, 3, ' PSIA'))
        self.labels['xducer_torr'].setText(_fmt(_torr(xducer_psia), 1, ' Torr'))
        self.labels['delta_torr'].setText(_fmt(delta, 1, ' Torr'))
        self.labels['cmd_sp'].setText(_fmt(cmd_sp, 4, ' PSIA'))
        self.labels['route'].setText(self._route_text)


class ManualSetpointLoggerWindow(QMainWindow):
    """Connect both ports, command setpoints, and CSV-log live pressures."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle('Stinger Manual Setpoint Logger')
        self.resize(920, 480)

        self._config = load_config()
        setup_logging(self._config)
        self._pm: Optional[PortManager] = None
        self._log_path: Optional[Path] = None
        self._log_file = None
        self._log_writer: Optional[csv.DictWriter] = None
        self._baro_psi = 14.7

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_once)

        central = QWidget()
        root = QVBoxLayout(central)

        top = QHBoxLayout()
        self.connect_btn = QPushButton('Connect')
        self.connect_btn.clicked.connect(self._connect)
        self.disconnect_btn = QPushButton('Disconnect')
        self.disconnect_btn.clicked.connect(self._disconnect)
        self.disconnect_btn.setEnabled(False)
        self.log_enable = QCheckBox('Log CSV')
        self.log_enable.setChecked(True)
        self.choose_log_btn = QPushButton('Log file…')
        self.choose_log_btn.clicked.connect(self._choose_log_file)
        top.addWidget(self.connect_btn)
        top.addWidget(self.disconnect_btn)
        top.addWidget(self.log_enable)
        top.addWidget(self.choose_log_btn)
        top.addStretch(1)
        root.addLayout(top)

        panels = QHBoxLayout()
        self.panel_a = PortControlPanel(PortId.PORT_A)
        self.panel_b = PortControlPanel(PortId.PORT_B)
        panels.addWidget(self.panel_a)
        panels.addWidget(self.panel_b)
        root.addLayout(panels)

        self.panel_a.apply_btn.clicked.connect(lambda: self._apply_setpoint(PortId.PORT_A))
        self.panel_b.apply_btn.clicked.connect(lambda: self._apply_setpoint(PortId.PORT_B))
        self.panel_a.atm_btn.clicked.connect(lambda: self._atmosphere(PortId.PORT_A))
        self.panel_b.atm_btn.clicked.connect(lambda: self._atmosphere(PortId.PORT_B))
        self.panel_a.vac_btn.clicked.connect(lambda: self._vacuum_route(PortId.PORT_A))
        self.panel_b.vac_btn.clicked.connect(lambda: self._vacuum_route(PortId.PORT_B))

        self.setCentralWidget(central)
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._set_status('Ready — Connect to start')

        default_log = get_logs_dir() / f'manual_setpoint_{datetime.now():%Y%m%d_%H%M%S}.csv'
        default_log.parent.mkdir(parents=True, exist_ok=True)
        self._log_path = default_log

    def _set_status(self, text: str) -> None:
        self._status.showMessage(text)

    def _choose_log_file(self) -> None:
        start = str(self._log_path or (get_logs_dir() / 'manual_setpoint.csv'))
        path, _ = QFileDialog.getSaveFileName(
            self,
            'CSV log file',
            start,
            'CSV (*.csv)',
        )
        if path:
            self._log_path = Path(path)
            self._set_status(f'Log file: {self._log_path}')

    def _connect(self) -> None:
        if self._pm is not None:
            return
        try:
            pm = PortManager(self._config)
            if not pm.initialize_ports():
                raise RuntimeError('PortManager.initialize_ports failed')
            if not pm.connect_all(safe_idle_on_connect=False):
                raise RuntimeError('PortManager.connect_all failed')
            for port in pm.ports.values():
                # Instant moves for manual verification (no Alicat ramp limiting).
                port.set_ramp_rate(0.0)
            self._pm = pm
            self._open_log_if_needed()
            self._timer.start(250)
            self.connect_btn.setEnabled(False)
            self.disconnect_btn.setEnabled(True)
            self._set_status(
                'Connected — ramp=0 (instant). Use Vacuum route before sub-atm SPs'
            )
            self._poll_once()
        except Exception as exc:
            self._cleanup()
            QMessageBox.critical(self, 'Connect failed', str(exc))
            self._set_status(f'Connect failed: {exc}')

    def _disconnect(self) -> None:
        self._cleanup()
        self.connect_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)
        self._set_status('Disconnected')

    def _cleanup(self) -> None:
        self._timer.stop()
        if self._pm is not None:
            try:
                self._pm.disconnect_all()
            except Exception:
                pass
            self._pm = None
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None
            self._log_writer = None

    def _open_log_if_needed(self) -> None:
        if not self.log_enable.isChecked() or self._log_path is None:
            return
        if self._log_file is not None:
            return
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self._log_path.exists()
        self._log_file = self._log_path.open('a', newline='', encoding='utf-8')
        self._log_writer = csv.DictWriter(
            self._log_file,
            fieldnames=[
                'timestamp',
                'port',
                'cmd_sp_psia',
                'alicat_psia',
                'alicat_torr',
                'transducer_psia',
                'transducer_torr',
                'delta_torr_alicat_minus_xducer',
                'route',
            ],
        )
        if new_file:
            self._log_writer.writeheader()
            self._log_file.flush()
        self._set_status(f'Logging to {self._log_path}')

    def _port(self, port_id: PortId):
        if self._pm is None:
            raise RuntimeError('Not connected')
        port = self._pm.ports.get(port_id)
        if port is None:
            raise RuntimeError(f'{port_id.value} not available')
        return port

    def _panel(self, port_id: PortId) -> PortControlPanel:
        return self.panel_a if port_id == PortId.PORT_A else self.panel_b

    def _apply_setpoint(self, port_id: PortId) -> None:
        try:
            port = self._port(port_id)
            sp = float(self._panel(port_id).setpoint_spin.value())
            port.set_ramp_rate(0.0)
            ok = port.set_pressure(sp)
            if not ok:
                raise RuntimeError('set_pressure returned False')
            self._set_status(f'{port_id.value}: setpoint {sp:.4f} PSIA (ramp=0)')
        except Exception as exc:
            QMessageBox.warning(self, 'Setpoint failed', str(exc))
            self._set_status(f'{port_id.value}: setpoint failed: {exc}')

    def _atmosphere(self, port_id: PortId) -> None:
        try:
            port = self._port(port_id)
            port.vent_to_atmosphere()
            self._panel(port_id).set_route('atmosphere/EXH')
            self._set_status(f'{port_id.value}: atmosphere / EXH')
        except Exception as exc:
            QMessageBox.warning(self, 'Atmosphere failed', str(exc))

    def _vacuum_route(self, port_id: PortId) -> None:
        try:
            port = self._port(port_id)
            ok = port.prepare_vacuum_route_for_test(self._baro_psi)
            if not ok:
                raise RuntimeError('prepare_vacuum_route_for_test failed')
            self._panel(port_id).set_route('vacuum/test')
            self._set_status(f'{port_id.value}: vacuum/test route')
        except Exception as exc:
            QMessageBox.warning(self, 'Vacuum route failed', str(exc))

    def _extract(self, reading: PortReading) -> tuple[Optional[float], Optional[float], Optional[float]]:
        baro = infer_barometric_pressure(reading)
        if baro is not None and is_plausible_barometric_psi(baro):
            self._baro_psi = float(baro)
        else:
            baro = self._baro_psi
        alicat = _alicat_pressure_abs_psi(reading, baro)
        xducer = _transducer_pressure_abs_psi(reading, baro)
        cmd = None
        if reading.alicat is not None and reading.alicat.setpoint is not None:
            cmd = float(reading.alicat.setpoint)
        return alicat, xducer, cmd

    def _poll_once(self) -> None:
        if self._pm is None:
            return
        try:
            self._open_log_if_needed()
            now = datetime.now().isoformat(timespec='seconds')
            for port_id in (PortId.PORT_A, PortId.PORT_B):
                port = self._pm.ports.get(port_id)
                if port is None:
                    continue
                port.refresh_alicat()
                reading = port.read_fast()
                alicat, xducer, cmd = self._extract(reading)
                panel = self._panel(port_id)
                panel.update_reading(alicat_psia=alicat, xducer_psia=xducer, cmd_sp=cmd)
                if self.log_enable.isChecked() and self._log_writer is not None:
                    a_torr = _torr(alicat)
                    x_torr = _torr(xducer)
                    delta = None if a_torr is None or x_torr is None else a_torr - x_torr
                    self._log_writer.writerow(
                        {
                            'timestamp': now,
                            'port': port_id.value,
                            'cmd_sp_psia': '' if cmd is None else f'{cmd:.4f}',
                            'alicat_psia': '' if alicat is None else f'{alicat:.4f}',
                            'alicat_torr': '' if a_torr is None else f'{a_torr:.2f}',
                            'transducer_psia': '' if xducer is None else f'{xducer:.4f}',
                            'transducer_torr': '' if x_torr is None else f'{x_torr:.2f}',
                            'delta_torr_alicat_minus_xducer': (
                                '' if delta is None else f'{delta:.2f}'
                            ),
                            'route': panel.route_text,
                        }
                    )
            if self._log_file is not None:
                self._log_file.flush()
        except Exception as exc:
            self._set_status(f'Poll error: {exc}')

    def closeEvent(self, event) -> None:  # noqa: N802
        self._cleanup()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    window = ManualSetpointLoggerWindow()
    window.show()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
