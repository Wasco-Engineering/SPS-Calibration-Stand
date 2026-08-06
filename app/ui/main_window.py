"""
Main window for Stinger application.

Provides the primary operator interface with:
- Top bar (work order info, controls)
- Two port columns (Port A and Port B)
- Tab navigation (Main, Debug, Admin)
"""

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, TYPE_CHECKING, cast, Tuple

from PyQt6.QtCore import QBuffer, QIODevice, Qt, pyqtSlot, QTimer
from PyQt6.QtGui import QAction, QFont
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QLabel, QPushButton, QFrame,
    QMessageBox, QInputDialog, QLineEdit, QProgressBar,
    QToolButton, QMenu, QComboBox, QDialog,
    QCheckBox, QTableWidget, QTableWidgetItem, QTextEdit, QHeaderView,
    QGridLayout
)

if TYPE_CHECKING:
    from app.services.ui_bridge import UIBridge

from app.ui.login_dialog import LoginDialog
from app.ui.port_column import PortColumn
from app.core.config import is_port_installed
from app.services.bug_report_service import create_bug_report
from app.ui.styles import STYLES, STATUS_COLORS, status_badge_style, status_tool_button_style
from app.ui.debug_panel import DebugPortPanel
from app.ui.bug_report_dialog import BugReportDialog
from app.services.ptp_service import convert_pressure


logger = logging.getLogger(__name__)


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _map_db9_pin_to_dio(port_id: str, pin: int) -> Optional[int]:
    if pin < 1 or pin > 9:
        return None
    if port_id == "port_a":
        return pin - 1
    if port_id == "port_b":
        return pin + 8
    return None


class MainWindow(QMainWindow):
    """
    Main application window for Stinger.

    Contains the operator-facing interface with two port columns
    and navigation between Main, Debug, and Admin tabs.
    """

    _TAB_MAIN = 0
    _TAB_DEBUG = 1
    _TAB_ADMIN = 2

    def __init__(
        self,
        config: Dict[str, Any],
        ui_bridge: Optional['UIBridge'] = None,
        *,
        skip_login: bool = False,
    ):
        """
        Initialize the main window.
        
        Args:
            config: Application configuration dictionary.
        """
        super().__init__()
        
        self.config = config
        self._ui_bridge = ui_bridge
        self._admin_pin = config.get('ui', {}).get('admin_pin', '2245')

        self._status_data = {
            'system': 'Unknown',
            'database': 'Unknown',
            'hardware': 'Unknown',
            'hardware_port_a': 'Unknown',
            'hardware_port_b': 'Unknown',
            'last_error': 'None',
        }
        self._start_time = time.monotonic()
        self._uptime_timer: Optional[QTimer] = None
        self._status_level = 'unknown'
        self._status_actions: Dict[str, QAction | None] = {}
        self._status_button: Optional[QToolButton] = None
        self._status_menu: Optional[QMenu] = None


        self._debug_panels: Dict[str, DebugPortPanel] = {}  # Store DebugPortPanel widgets
        self._debug_logs_preview: Optional[QTextEdit] = None
        self._debug_units_combo: Optional[QComboBox] = None
        self._admin_measurement_source_combo: Optional[QComboBox] = None
        self._admin_labels: Dict[str, QLabel] = {}
        self._test_history_table: Optional[QTableWidget] = None
        self._logs_preview: Optional[QTextEdit] = None
        self._ptp_preview: Optional[QTextEdit] = None
        self._overlay_widget: Optional[QWidget] = None  # Grey overlay for login dialog
        self._admin_pin_verified = False  # Track if admin PIN has been verified
        self._debug_pin_verified = False  # Track if debug PIN has been verified

        self._setup_window()
        self._setup_ui()
        self._status_data["system"] = "Ready"
        self._refresh_status_level()
        self._setup_uptime_timer()

        if self._ui_bridge is not None:
            self._connect_ui_bridge()
            if not skip_login:
                QTimer.singleShot(0, self._show_login_dialog)


        
        logger.info("MainWindow initialized")
    
    def _setup_window(self) -> None:
        """Configure window properties."""
        self.setWindowTitle("Stinger - Scorpion Calibration Stand")
        self.setMinimumSize(1280, 800)
        
        # Set consistent light background
        self.setStyleSheet(STYLES["main_window_bg"])
        
        # Full screen for touch interface
        # self.showFullScreen()
    
    def _setup_ui(self) -> None:
        """Build the UI layout."""
        central = QWidget()
        self.setCentralWidget(central)
        
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        
        # Tab widget with custom styling
        self._tabs = QTabWidget()
        self._tabs.setTabPosition(QTabWidget.TabPosition.North)
        self._tabs.setStyleSheet(STYLES["tab_widget"])
        
        # Main tab (production)
        self._main_tab = self._create_main_tab()
        self._tabs.addTab(self._main_tab, "Main")
        
        # Debug tab (PIN protected)
        self._debug_tab = self._create_debug_tab()
        self._tabs.addTab(self._debug_tab, "Debug")
        
        # Admin tab (PIN protected)
        self._admin_tab = self._create_admin_tab()
        self._tabs.addTab(self._admin_tab, "Admin")
        
        # Connect tab change for PIN protection
        self._tabs.currentChanged.connect(self._on_tab_changed)
        self._last_tab_index = self._TAB_MAIN
        
        layout.addWidget(self._tabs, 1)

    def _setup_uptime_timer(self) -> None:
        """Start a timer to update system uptime label."""
        if self._uptime_timer is None:
            self._uptime_timer = QTimer(self)
            self._uptime_timer.setInterval(1000)
            self._uptime_timer.timeout.connect(self._update_uptime)
        self._uptime_timer.start()
        self._update_uptime()
    
    def _create_top_bar(self) -> QWidget:
        """Create the top bar with work order info and controls."""
        bar = QFrame()
        bar.setFrameStyle(QFrame.Shape.NoFrame)
        bar.setFixedHeight(72)
        
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(16)
        
        # End Work Order button
        self._btn_end_wo = QPushButton("End Work Order")
        self._btn_end_wo.setFixedSize(140, 48)
        self._btn_end_wo.setFont(QFont("Segoe UI, Inter, Arial", 12, QFont.Weight.Bold))
        self._btn_end_wo.clicked.connect(self._on_end_work_order)
        self._btn_end_wo.setStyleSheet(STYLES["end_wo_button"])
        layout.addWidget(self._btn_end_wo)
        
        # Work order info - simplified, no card borders
        self._lbl_operator = QLabel("---")
        self._lbl_shop_order = QLabel("---")
        self._lbl_part = QLabel("---")
        self._lbl_sequence = QLabel("---")
        self._lbl_process = QLabel("---")

        layout.addWidget(self._build_info_item("Operator", self._lbl_operator))
        layout.addWidget(self._build_info_item("Shop Order", self._lbl_shop_order))
        layout.addWidget(self._build_info_item("Part", self._lbl_part))
        layout.addWidget(self._build_info_item("Seq", self._lbl_sequence))
        layout.addWidget(self._build_info_item("Process", self._lbl_process))

        # PTP quick view (plain title + compact card)
        ptp_widget = QWidget()
        ptp_widget.setMinimumWidth(360)
        ptp_layout = QVBoxLayout(ptp_widget)
        ptp_layout.setContentsMargins(0, 0, 0, 0)
        ptp_layout.setSpacing(2)

        ptp_title = QLabel("PTP")
        ptp_title.setStyleSheet(STYLES["title_muted_11"])
        ptp_title.setFont(QFont("Segoe UI, Inter, Arial", 9))
        ptp_layout.addWidget(ptp_title)

        ptp_card = QFrame()
        ptp_card.setStyleSheet(STYLES["compact_panel_shell"])
        ptp_row = QHBoxLayout(ptp_card)
        ptp_row.setContentsMargins(8, 3, 8, 3)
        ptp_row.setSpacing(6)

        self._lbl_ptp_setpoint = QLabel("Setpoint: --")
        self._lbl_ptp_setpoint.setFont(QFont("Segoe UI, Inter, Arial", 9, QFont.Weight.Bold))
        self._lbl_ptp_setpoint.setStyleSheet(STYLES["topbar_meta_chip"])
        ptp_row.addWidget(self._lbl_ptp_setpoint)

        self._lbl_ptp_direction = QLabel("Direction: --")
        self._lbl_ptp_direction.setFont(QFont("Segoe UI, Inter, Arial", 9, QFont.Weight.Bold))
        self._lbl_ptp_direction.setStyleSheet(STYLES["topbar_meta_chip"])
        ptp_row.addWidget(self._lbl_ptp_direction)

        self._lbl_ptp_units = QLabel("Units: --")
        self._lbl_ptp_units.setFont(QFont("Segoe UI, Inter, Arial", 9, QFont.Weight.Bold))
        self._lbl_ptp_units.setStyleSheet(STYLES["topbar_meta_chip"])
        ptp_row.addWidget(self._lbl_ptp_units)

        ptp_row.addStretch()
        ptp_layout.addWidget(ptp_card)
        layout.addWidget(ptp_widget)

        # Progress with inline indicators
        progress_widget = QWidget()
        progress_widget.setMinimumWidth(230)
        progress_layout = QVBoxLayout(progress_widget)
        progress_layout.setContentsMargins(6, 2, 6, 2)
        progress_layout.setSpacing(3)

        progress_title = QLabel("Progress")
        progress_title.setStyleSheet(STYLES["title_muted_11"])
        progress_title.setFont(QFont("Segoe UI, Inter, Arial", 10))
        progress_layout.addWidget(progress_title)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setFixedHeight(18)
        self._progress_bar.setStyleSheet(STYLES["progress_bar"])
        self._progress_bar.setFont(QFont("Segoe UI, Inter, Arial", 10, QFont.Weight.Bold))
        progress_row = QHBoxLayout()
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.setSpacing(8)
        progress_row.addWidget(self._progress_bar, 1)

        self._lbl_progress_percent = QLabel("0%")
        self._lbl_progress_percent.setStyleSheet(STYLES["progress_label"])
        self._lbl_progress_percent.setFont(QFont("Segoe UI, Inter, Arial", 11, QFont.Weight.Bold))
        self._lbl_progress_percent.setFixedWidth(32)
        self._lbl_progress_percent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        progress_row.addWidget(self._lbl_progress_percent)

        self._lbl_progress = QLabel("0 / 0")
        self._lbl_progress.setStyleSheet(STYLES["progress_label"])
        self._lbl_progress.setFont(QFont("Segoe UI, Inter, Arial", 11, QFont.Weight.Bold))
        self._lbl_progress.setFixedWidth(46)
        self._lbl_progress.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        progress_row.addWidget(self._lbl_progress)

        progress_layout.addLayout(progress_row)

        layout.addWidget(progress_widget)
        layout.addStretch()

        self._status_button = self._build_status_button()
        layout.addWidget(self._status_button)

        self._btn_report_bug = QPushButton("Report Bug")
        self._btn_report_bug.setFixedSize(112, 40)
        self._btn_report_bug.setFont(QFont("Segoe UI, Inter, Arial", 10, QFont.Weight.Bold))
        self._btn_report_bug.setToolTip("Save a screenshot, logs, and report details")
        self._btn_report_bug.clicked.connect(self._on_report_bug)
        layout.addWidget(self._btn_report_bug)

        # Close button
        self._btn_close = QPushButton("Close Program")
        self._btn_close.setFixedSize(120, 48)
        self._btn_close.setFont(QFont("Segoe UI, Inter, Arial", 12, QFont.Weight.Bold))
        self._btn_close.clicked.connect(self._on_close_program)
        self._btn_close.setStyleSheet(STYLES["close_button"])
        layout.addWidget(self._btn_close)
        
        return bar

    def _build_status_button(self) -> QToolButton:
        """Create the status icon button with popdown menu and pulsing dot."""
        button = QToolButton()
        button.setFixedHeight(32)
        button.setToolTip("System Status")
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setAutoRaise(True)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)

        menu = QMenu(button)
        menu.setStyleSheet(STYLES["menu"])
        self._status_menu = menu
        self._status_actions = {
            "system": cast(QAction, menu.addAction("System: Unknown")),
            "database": cast(QAction, menu.addAction("Database: Unknown")),
            "hardware_port_a": cast(QAction, menu.addAction("Port A: Unknown")),
            "hardware_port_b": cast(QAction, menu.addAction("Port B: Unknown")),
            "last_error": cast(QAction, menu.addAction("Last Error: None")),
        }
        for action in self._status_actions.values():
            if action is not None:
                action.setEnabled(False)

        button.setMenu(menu)
        self._update_status_icon()
        return button

    def _update_status_icon(self) -> None:
        if not self._status_button:
            return
        color = STATUS_COLORS.get(self._status_level, STATUS_COLORS["unknown"])
        # Add a pulsing dot indicator using bullet character
        dot = "●"
        self._status_button.setText(f"{dot} {self._status_level.upper()}  v")
        self._status_button.setStyleSheet(status_tool_button_style(color))

    def _update_status_menu(self) -> None:
        if not self._status_actions:
            return
        system_action = cast(Optional[QAction], self._status_actions.get("system"))
        database_action = cast(Optional[QAction], self._status_actions.get("database"))
        hardware_port_a_action = cast(Optional[QAction], self._status_actions.get("hardware_port_a"))
        hardware_port_b_action = cast(Optional[QAction], self._status_actions.get("hardware_port_b"))
        last_error_action = cast(Optional[QAction], self._status_actions.get("last_error"))

        if system_action is not None:
            system_action.setText(f"System: {self._status_data['system']}")
        if database_action is not None:
            database_action.setText(f"Database: {self._status_data['database']}")
        if hardware_port_a_action is not None:
            hardware_port_a_action.setText(f"Port A: {self._status_data['hardware_port_a']}")
        if hardware_port_b_action is not None:
            hardware_port_b_action.setText(f"Port B: {self._status_data['hardware_port_b']}")
        if last_error_action is not None:
            last_error = self._truncate_text(self._status_data['last_error'])
            last_error_action.setText(f"Last Error: {last_error}")

    def _status_text_to_level(self, status: str) -> str:
        text = (status or "").lower()
        if any(word in text for word in ("fail", "error", "disconnect", "fault", "not initialized")):
            return "error"
        if any(word in text for word in ("warning", "warn", "degraded")):
            return "warning"
        if any(word in text for word in ("ok", "connected", "configured", "ready", "online")):
            return "ok"
        return "unknown"

    def _refresh_status_level(self) -> None:
        last_error = self._status_data.get("last_error", "None")
        if last_error and last_error != "None":
            self._status_level = "error"
            self._status_data["system"] = "Error"
        else:
            levels = [
                self._status_text_to_level(self._status_data.get("system", "Unknown")),
                self._status_text_to_level(self._status_data.get("database", "Unknown")),
                self._status_text_to_level(self._status_data.get("hardware", "Unknown")),
            ]
            if "error" in levels:
                self._status_level = "error"
            elif "warning" in levels:
                self._status_level = "warning"
            elif "ok" in levels:
                self._status_level = "ok"
            else:
                self._status_level = "unknown"

        self._update_status_icon()
        self._update_status_menu()

    def _truncate_text(self, text: str, limit: int = 60) -> str:
        if len(text) <= limit:
            return text
        return f"{text[:limit - 3]}..."

    def _build_info_item(self, title: str, value_label: QLabel) -> QWidget:
        """Build a compact info display without card borders."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(3)

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(STYLES["title_muted_11"])
        title_lbl.setFont(QFont("Segoe UI, Inter, Arial", 9))
        layout.addWidget(title_lbl)

        value_label.setStyleSheet("color: #1a1a2e; font-size: 13px; font-weight: bold;")
        value_label.setFont(QFont("Segoe UI, Inter, Arial", 13, QFont.Weight.Bold))
        value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(value_label)

        return widget

    def _build_field_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(STYLES["label_muted_12"])
        return label

    def _build_status_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(STYLES["status_label"])
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setFixedWidth(120)
        return label

    def _build_action_button(self, text: str, handler: Callable[[], None]) -> QPushButton:
        button = QPushButton(text)
        button.setMinimumHeight(40)
        button.setStyleSheet(STYLES["action_button"])
        button.clicked.connect(handler)
        return button

    def _update_database_status(self, status: str, last_write: str = "--", queue: str = "0") -> None:
        if "database_status" in self._admin_labels:
            self._apply_status_from_text(self._admin_labels["database_status"], status)
        if "database_last_write" in self._admin_labels:
            self._admin_labels["database_last_write"].setText(last_write)
        if "database_queue" in self._admin_labels:
            self._admin_labels["database_queue"].setText(queue)
        self._status_data["database"] = status
        self._refresh_status_level()

    def _update_uptime(self) -> None:
        if "system_uptime" not in self._admin_labels:
            return
        elapsed = int(time.monotonic() - self._start_time)
        hours = elapsed // 3600
        minutes = (elapsed % 3600) // 60
        seconds = elapsed % 60
        self._admin_labels["system_uptime"].setText(f"{hours:02d}:{minutes:02d}:{seconds:02d}")

    def _build_admin_card(self, title: str) -> Tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setStyleSheet(STYLES["card"])
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        label = QLabel(title)
        label.setStyleSheet(STYLES["card_title"])
        layout.addWidget(label)
        return card, layout

    def _build_admin_status_row(self, label: str, key: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        name = QLabel(label)
        name.setStyleSheet(STYLES["label_muted_12"])
        row.addWidget(name)
        value = self._build_status_label("Unknown")
        row.addWidget(value)
        row.addStretch()
        self._admin_labels[key] = value
        return row

    def _build_hardware_status_grid(self) -> QGridLayout:
        """Build a grid layout for hardware status with aligned label/badge columns and visual grouping."""
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        grid.setColumnMinimumWidth(0, 115)

        # Connectivity group
        connectivity_rows = [
            ("Alicat", "hardware_alicat"),
            ("DAQ", "hardware_daq"),
            ("Serial", "hardware_serial"),
        ]
        # Operational group
        operational_rows = [
            ("Precision Owner", "precision_owner"),
            ("Precision Queue", "precision_queue"),
            ("Alicat Poll", "alicat_poll_profile"),
        ]

        row_idx = 0
        for label_text, key in connectivity_rows:
            name = QLabel(label_text)
            name.setStyleSheet(STYLES["label_muted_12"])
            grid.addWidget(name, row_idx, 0)
            badge = QLabel("Unknown")
            badge.setStyleSheet(status_badge_style("unknown"))
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setMinimumWidth(120)
            grid.addWidget(badge, row_idx, 1, Qt.AlignmentFlag.AlignLeft)
            self._admin_labels[key] = badge
            row_idx += 1

        # Thin separator line between connectivity and operational groups
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setStyleSheet("color: rgba(0,0,0,0.10); margin: 2px 0;")
        grid.addWidget(separator, row_idx, 0, 1, 2)
        row_idx += 1

        for label_text, key in operational_rows:
            name = QLabel(label_text)
            name.setStyleSheet(STYLES["label_muted_12"])
            grid.addWidget(name, row_idx, 0)
            badge = QLabel("Unknown")
            badge.setStyleSheet(status_badge_style("unknown"))
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setMinimumWidth(120)
            grid.addWidget(badge, row_idx, 1, Qt.AlignmentFlag.AlignLeft)
            self._admin_labels[key] = badge
            row_idx += 1

        grid.setColumnStretch(2, 1)
        return grid

    def _build_admin_action_row(self, actions: list[Tuple[str, Any]]) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        for label, handler in actions:
            row.addWidget(self._build_action_button(label, handler))
        row.addStretch()
        return row

    def _clear_test_history(self) -> None:
        if self._test_history_table is None:
            return
        self._test_history_table.setRowCount(0)

    def _seed_admin_defaults(self) -> None:
        self._update_database_status("Unknown")
        if "system_version" in self._admin_labels:
            self._admin_labels["system_version"].setText(
                str(self.config.get("app", {}).get("version", "Unknown"))
            )
        if "system_uptime" in self._admin_labels:
            self._admin_labels["system_uptime"].setText("00:00:00")
        if "system_mode" in self._admin_labels:
            self._apply_status_from_text(self._admin_labels["system_mode"], "Production")

    def _append_test_history(self, port_id: str, result: Dict[str, Any]) -> None:
        if self._test_history_table is None:
            return
        unit_label = self._ui_bridge.get_pressure_unit() if self._ui_bridge else "PSI"
        activation = result.get("increasing_activation")
        deactivation = result.get("decreasing_deactivation")
        activation_display = (
            convert_pressure(activation, "PSI", unit_label) if activation is not None else "--"
        )
        deactivation_display = (
            convert_pressure(deactivation, "PSI", unit_label) if deactivation is not None else "--"
        )
        row = self._test_history_table.rowCount()
        self._test_history_table.insertRow(row)
        timestamp = datetime.now().strftime("%H:%M:%S")
        serial_text = self.get_port_widget(port_id).get_serial_text()
        data = [
            timestamp,
            port_id.upper().replace("_", " "),
            serial_text,
            "PASS" if result.get("in_spec") else "FAIL" if result.get("in_spec") is not None else "--",
            f"{activation_display}",
            f"{deactivation_display}",
        ]
        for column, value in enumerate(data):
            item = QTableWidgetItem(value)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._test_history_table.setItem(row, column, item)

    def _update_debug_switches(self, port_id: str, no_active: bool, nc_active: bool) -> None:
        """Update debug panel switch indicators."""
        panel = self._debug_panels.get(port_id)
        if panel:
            panel.update_switch_states(no_active, nc_active)

    def _update_debug_dio(self, port_id: str, dio_values: Dict[int, int]) -> None:
        panel = self._debug_panels.get(port_id)
        if panel:
            panel.update_dio_values(dio_values)
    
    def _update_debug_chart(
        self,
        port_id: str,
        timestamp: float,
        pressure: Optional[float],
        setpoint: Optional[float],
        alicat_pressure: Optional[float],
    ) -> None:
        """Update debug panel chart with new pressure data."""
        panel = self._debug_panels.get(port_id)
        if panel:
            panel.update_chart(timestamp, pressure, setpoint, alicat_pressure)

    def _on_debug_units_changed(self, units_label: str) -> None:
        if self._ui_bridge:
            self._ui_bridge.set_pressure_unit(units_label)
        for panel in self._debug_panels.values():
            panel.set_units_label(units_label)

    
    def _create_divider(self) -> QWidget:
        """Create a vertical divider line."""
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setStyleSheet("color: #d1d5db;")
        divider.setFixedWidth(1)
        return divider


    def _build_info_card(self, title: str, value_label: QLabel, style: str) -> QWidget:
        """Legacy method - kept for compatibility."""
        return self._build_info_item(title, value_label)

    
    def _create_main_tab(self) -> QWidget:
        """Create the main production tab with two port columns."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(2)  # Reduced from 8 to bring top bar closer to ports
        layout.setContentsMargins(0, 0, 0, 0)

        # Top bar inside Main tab
        self._top_bar = self._create_top_bar()
        layout.addWidget(self._top_bar)

        ports_row = QHBoxLayout()
        ports_row.setSpacing(16)
        ports_row.setContentsMargins(8, 2, 8, 8)  # Reduced top margin from 8 to 2
        
        # Port A column
        self._port_a_widget = PortColumn("port_a", "Port A (Left)")
        ports_row.addWidget(self._port_a_widget, 1)
        
        # Port B column
        self._port_b_widget = PortColumn("port_b", "Port B (Right)")
        ports_row.addWidget(self._port_b_widget, 1)

        self._apply_port_install_visibility()
        
        layout.addLayout(ports_row, 1)
        return tab

    def _apply_port_install_visibility(self) -> None:
        """Hide port columns that are not installed on this stand."""
        for port_id, widget in (
            ('port_a', self._port_a_widget),
            ('port_b', self._port_b_widget),
        ):
            if not is_port_installed(self.config, port_id):
                widget.hide()
                widget.setEnabled(False)
    
    def _create_protected_tab(self, placeholder_text: str) -> QWidget:
        """Create a tab with a PIN placeholder; content_layout and placeholder_label stored on widget."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)

        placeholder_label = QLabel(placeholder_text)
        placeholder_label.setFont(QFont("Segoe UI, Inter, Arial", 20, QFont.Weight.Bold))
        placeholder_label.setStyleSheet(STYLES["placeholder_heading"])
        placeholder_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(placeholder_label)
        layout.addStretch()

        tab.setProperty("placeholder_label", placeholder_label)
        tab.setProperty("content_layout", layout)
        return tab

    def _create_debug_tab(self) -> QWidget:
        """Create the debug tab for engineering use."""
        return self._create_protected_tab("Enter PIN to access Debug panel")

    def _populate_debug_tab(self) -> None:
        """Populate debug tab content after PIN verification."""
        if self._debug_pin_verified:
            return  # Already populated
        
        debug_tab = self._tabs.widget(self._TAB_DEBUG)
        if debug_tab is None:
            return
        
        placeholder_label = debug_tab.property("placeholder_label")
        content_layout = debug_tab.property("content_layout")
        
        if placeholder_label is None or content_layout is None:
            return
        
        # Remove placeholder
        content_layout.removeWidget(placeholder_label)
        placeholder_label.deleteLater()
        
        # Add actual content
        header = QHBoxLayout()
        header.setSpacing(16)

        title = QLabel("Debug Mode")
        title.setFont(QFont("Segoe UI, Inter, Arial", 20, QFont.Weight.Bold))
        title.setStyleSheet("color: #1a1a2e;")
        header.addWidget(title)

        header.addStretch()

        self._debug_units_combo = QComboBox()
        self._debug_units_combo.addItems(["PSIG", "PSIA", "PSI", "Torr", "mTorr", "mmHg", "inHg"])
        self._debug_units_combo.setFixedWidth(120)
        self._debug_units_combo.setStyleSheet(STYLES["combo_box"])
        current_units = self._ui_bridge.get_pressure_unit() if self._ui_bridge else "PSIG"
        if current_units:
            self._debug_units_combo.setCurrentText(current_units)
        self._debug_units_combo.currentTextChanged.connect(self._on_debug_units_changed)
        header.addWidget(self._debug_units_combo)

        safety_toggle = QCheckBox("Global Safety Override")
        safety_toggle.setStyleSheet("color: #1a1a2e; font-weight: bold;")
        safety_toggle.stateChanged.connect(
            lambda state: self._emit_admin_action("safety_override", {"enabled": state == Qt.CheckState.Checked})
        )
        header.addWidget(safety_toggle)

        active_port = QComboBox()
        active_port.addItems(["All Ports", "Port A", "Port B"])
        active_port.setFixedWidth(140)
        active_port.setStyleSheet(STYLES["combo_box"])
        header.addWidget(active_port)

        content_layout.addLayout(header)

        # Create debug port panels using new DebugPortPanel widget
        ports_row = QHBoxLayout()
        ports_row.setSpacing(16)
        labjack_config = self.config.get("hardware", {}).get("labjack", {})

        debug_noise_cfg = self.config.get("ui", {}).get("debug_noise", {})
        for port_id, title in [("port_a", "Port A Debug"), ("port_b", "Port B Debug")]:
            panel = DebugPortPanel(port_id, title, noise_config=debug_noise_cfg)
            panel.action_requested.connect(
                lambda action, payload, pid=port_id: self._emit_debug_action(pid, action, payload)
            )
            port_cfg = labjack_config.get(port_id, {})
            sensed_pins = port_cfg.get("switch_sensed_db9_pins") or []
            sensed_pin = sensed_pins[0] if isinstance(sensed_pins, list) and sensed_pins else None
            sensed_pin_int = _safe_int(sensed_pin)
            sensed_dio = (
                _map_db9_pin_to_dio(port_id, sensed_pin_int)
                if sensed_pin_int is not None
                else None
            )
            panel.set_switch_pins(
                sensed_dio if sensed_dio is not None else port_cfg.get("switch_no_dio"),
                sensed_dio if sensed_dio is not None else port_cfg.get("switch_nc_dio"),
            )
            self._emit_debug_action(port_id, "set_solenoid_mode", {"mode": "auto"})
            self._debug_panels[port_id] = panel
            ports_row.addWidget(panel, 1)
            if not is_port_installed(self.config, port_id):
                panel.hide()
                panel.setEnabled(False)

        content_layout.addLayout(ports_row, 2)
        
        self._debug_pin_verified = True

    def _create_admin_tab(self) -> QWidget:
        """Create the admin tab for observability."""
        return self._create_protected_tab("Enter PIN to access Admin panel")

    def _populate_admin_tab(self) -> None:
        """Populate admin tab content after PIN verification."""
        if self._admin_pin_verified:
            return  # Already populated
        
        admin_tab = self._tabs.widget(self._TAB_ADMIN)
        if admin_tab is None:
            return
        
        placeholder_label = admin_tab.property("placeholder_label")
        content_layout = admin_tab.property("content_layout")
        
        if placeholder_label is None or content_layout is None:
            return
        
        # Remove placeholder
        content_layout.removeWidget(placeholder_label)
        placeholder_label.deleteLater()
        
        # Add actual content
        title = QLabel("Admin / Observability")
        title.setFont(QFont("Segoe UI, Inter, Arial", 20, QFont.Weight.Bold))
        title.setStyleSheet("color: #1a1a2e;")
        content_layout.addWidget(title)

        top_row = QHBoxLayout()
        top_row.setSpacing(16)

        hardware_card, hardware_layout = self._build_admin_card("Hardware Status")
        hardware_layout.addLayout(self._build_hardware_status_grid())
        hardware_layout.addLayout(self._build_admin_action_row([
            ("Reconnect Hardware", lambda: self._emit_admin_action("reconnect_hardware", {})),
            ("Refresh", lambda: self._emit_admin_action("refresh_hardware", {})),
        ]))
        top_row.addWidget(hardware_card, 1)

        db_card, db_layout = self._build_admin_card("Database Status")
        db_layout.addLayout(self._build_admin_status_row("SQL Server", "database_status"))
        db_layout.addLayout(self._build_admin_status_row("Last Write", "database_last_write"))
        db_layout.addLayout(self._build_admin_status_row("Pending Queue", "database_queue"))
        db_layout.addLayout(self._build_admin_action_row([
            ("Reconnect DB", lambda: self._emit_admin_action("reconnect_db", {})),
            ("Refresh", lambda: self._emit_admin_action("refresh_db", {})),
        ]))
        ptp_label = QLabel("Current PTP")
        ptp_label.setStyleSheet(STYLES["label_muted_12"])
        db_layout.addWidget(ptp_label)
        self._ptp_preview = QTextEdit()
        self._ptp_preview.setReadOnly(True)
        self._ptp_preview.setPlaceholderText("No PTP loaded.")
        self._ptp_preview.setFixedHeight(180)
        self._ptp_preview.setStyleSheet(STYLES["readonly_text_edit"])
        db_layout.addWidget(self._ptp_preview)
        top_row.addWidget(db_card, 1)

        content_layout.addLayout(top_row)

        history_card, history_layout = self._build_admin_card("Recent Test History")
        self._test_history_table = QTableWidget(0, 6)
        self._test_history_table.setHorizontalHeaderLabels(
            ["Time", "Port", "Serial", "Result", "Activation", "Deactivation"]
        )
        header = cast(QHeaderView, self._test_history_table.horizontalHeader())
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        vheader = cast(QHeaderView, self._test_history_table.verticalHeader())
        vheader.setVisible(False)
        self._test_history_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._test_history_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._test_history_table.setStyleSheet(STYLES["table_widget"])
        history_layout.addWidget(self._test_history_table)
        history_layout.addLayout(self._build_admin_action_row([
            ("Export CSV", lambda: self._emit_admin_action("export_history", {})),
            ("Clear History", self._clear_test_history),
        ]))
        content_layout.addWidget(history_card, 2)

        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(16)

        system_card, system_layout = self._build_admin_card("System Info")
        system_layout.addLayout(self._build_admin_status_row("Version", "system_version"))
        system_layout.addLayout(self._build_admin_status_row("Uptime", "system_uptime"))
        system_layout.addLayout(self._build_admin_status_row("Mode", "system_mode"))
        measurement_row = QHBoxLayout()
        measurement_row.setSpacing(8)
        measurement_label = QLabel("Main Measurement")
        measurement_label.setStyleSheet(STYLES["label_muted_12"])
        measurement_row.addWidget(measurement_label)
        self._admin_measurement_source_combo = QComboBox()
        self._admin_measurement_source_combo.addItems(["auto", "transducer", "alicat"])
        self._admin_measurement_source_combo.setFixedWidth(140)
        self._admin_measurement_source_combo.setStyleSheet(STYLES["combo_box"])
        measurement_cfg = self.config.get("hardware", {}).get("measurement", {})
        preferred_source = "auto"
        if isinstance(measurement_cfg, dict):
            preferred_source = str(measurement_cfg.get("preferred_source", "auto") or "auto").strip().lower()
        if preferred_source not in {"auto", "transducer", "alicat"}:
            preferred_source = "auto"
        self._admin_measurement_source_combo.setCurrentText(preferred_source)
        self._admin_measurement_source_combo.currentTextChanged.connect(
            self._on_admin_measurement_source_changed
        )
        measurement_row.addWidget(self._admin_measurement_source_combo)
        measurement_row.addStretch()
        system_layout.addLayout(measurement_row)
        bottom_row.addWidget(system_card, 1)

        logs_card, logs_layout = self._build_admin_card("Logs")
        logs_layout.addLayout(self._build_admin_action_row([
            ("Open Logs", lambda: self._emit_admin_action("open_logs", {})),
            ("Open Bug Reports", lambda: self._emit_admin_action("open_bug_reports", {})),
            ("Export Logs", lambda: self._emit_admin_action("export_logs", {})),
        ]))
        self._logs_preview = QTextEdit()
        self._logs_preview.setReadOnly(True)
        self._logs_preview.setPlaceholderText("Log preview...")
        self._logs_preview.setFixedHeight(140)
        self._logs_preview.setStyleSheet(STYLES["readonly_text_edit"])
        logs_layout.addWidget(self._logs_preview)
        bottom_row.addWidget(logs_card, 1)

        content_layout.addLayout(bottom_row)
        self._seed_admin_defaults()
        
        self._admin_pin_verified = True

    
    def _prompt_for_pin_text(self, title: str, label: str) -> Tuple[str, bool]:
        dialog = QInputDialog(self)
        dialog.setWindowTitle(title)
        dialog.setLabelText(label)
        dialog.setTextEchoMode(QLineEdit.EchoMode.Password)
        dialog.setOkButtonText('Confirm')
        dialog.setCancelButtonText('Cancel')
        dialog.setStyleSheet(
            """
            QInputDialog {
                background-color: #f0f2f5;
            }
            QLabel {
                color: #1a1a2e;
            }
            QLineEdit {
                background-color: #ffffff;
                color: #1a1a2e;
                border: 1px solid rgba(0, 0, 0, 0.12);
                border-radius: 8px;
                padding: 8px 12px;
            }
            QLineEdit:focus {
                border: 2px solid #2563eb;
            }
            QPushButton {
                background-color: #e5e7eb;
                color: #1a1a2e;
                border: 1px solid rgba(0, 0, 0, 0.08);
                border-radius: 6px;
                padding: 6px 12px;
                font-weight: bold;
                min-width: 72px;
            }
            QPushButton:hover {
                background-color: #d1d5db;
            }
            """
        )
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        return dialog.textValue(), accepted

    def _require_pin_for_tab(
        self, index: int, verified_attr: str, populate_fn: Callable[[], None]
    ) -> bool:
        """Return True if access granted (tab allowed), False if reverted."""
        if getattr(self, verified_attr, False):
            self._last_tab_index = index
            return True
        pin, ok = self._prompt_for_pin_text('PIN Required', 'Enter PIN:')
        if ok and pin == self._admin_pin:
            populate_fn()
            setattr(self, verified_attr, True)
            self._last_tab_index = index
            return True
        if ok:
            self._show_styled_message(
                title='Access Denied',
                text='Incorrect PIN.',
                icon=QMessageBox.Icon.Warning,
            )
        self._tabs.setCurrentIndex(self._last_tab_index)
        return False

    def _create_styled_message_box(
        self,
        title: str,
        text: str,
        icon: QMessageBox.Icon = QMessageBox.Icon.Information,
        informative_text: Optional[str] = None,
        buttons: QMessageBox.StandardButton = QMessageBox.StandardButton.Ok,
        default_button: Optional[QMessageBox.StandardButton] = None,
        escape_button: Optional[QMessageBox.StandardButton] = None,
    ) -> QMessageBox:
        """Build a consistent, styled QMessageBox for app prompts."""
        dialog = QMessageBox(self)
        dialog.setWindowTitle(title)
        dialog.setIcon(icon)
        dialog.setText(text)
        if informative_text:
            dialog.setInformativeText(informative_text)
        dialog.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        dialog.setStandardButtons(buttons)
        if default_button is not None:
            dialog.setDefaultButton(default_button)
        if escape_button is not None:
            dialog.setEscapeButton(escape_button)
        dialog.setStyleSheet(
            """
            QMessageBox {
                background-color: #f0f2f5;
            }
            QLabel {
                color: #1a1a2e;
            }
            QPushButton {
                background-color: #e5e7eb;
                color: #1a1a2e;
                border: 1px solid rgba(0, 0, 0, 0.08);
                border-radius: 6px;
                padding: 6px 12px;
                font-weight: bold;
                min-width: 96px;
            }
            QPushButton:hover {
                background-color: #d1d5db;
            }
            """
        )
        return dialog

    def _show_styled_message(
        self,
        title: str,
        text: str,
        icon: QMessageBox.Icon,
        informative_text: Optional[str] = None,
    ) -> None:
        dialog = self._create_styled_message_box(
            title=title,
            text=text,
            icon=icon,
            informative_text=informative_text,
            buttons=QMessageBox.StandardButton.Ok,
            default_button=QMessageBox.StandardButton.Ok,
            escape_button=QMessageBox.StandardButton.Ok,
        )
        ok_button = dialog.button(QMessageBox.StandardButton.Ok)
        if ok_button is not None:
            ok_button.setText('OK')
        dialog.exec()

    def _confirm_end_work_order(self) -> bool:
        """Show a styled confirmation dialog before ending a work order."""
        shop_order = self._lbl_shop_order.text().strip()
        has_work_order = bool(shop_order and shop_order != '---')

        informative_text = (
            f'Active work order: {shop_order}\nBoth port sessions will reset.'
            if has_work_order
            else 'No active work order is loaded.'
        )
        dialog = self._create_styled_message_box(
            title='End Work Order',
            text='End the current work order?',
            icon=QMessageBox.Icon.Warning,
            informative_text=informative_text,
            buttons=(
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.Cancel
            ),
            default_button=QMessageBox.StandardButton.Cancel,
            escape_button=QMessageBox.StandardButton.Cancel,
        )

        end_button = dialog.button(QMessageBox.StandardButton.Yes)
        if end_button is not None:
            end_button.setText('End Work Order')
        cancel_button = dialog.button(QMessageBox.StandardButton.Cancel)
        if cancel_button is not None:
            cancel_button.setText('Keep Running')

        return dialog.exec() == QMessageBox.StandardButton.Yes

    @pyqtSlot(int)
    def _on_tab_changed(self, index: int) -> None:
        """Handle tab change - prompt for PIN if Debug/Admin."""
        if index == self._TAB_MAIN:
            self._last_tab_index = index
            return
        if index == self._TAB_DEBUG:
            self._require_pin_for_tab(index, "_debug_pin_verified", self._populate_debug_tab)
            return
        if index == self._TAB_ADMIN:
            self._require_pin_for_tab(index, "_admin_pin_verified", self._populate_admin_tab)
    
    @pyqtSlot()
    def _on_end_work_order(self) -> None:
        """Handle End Work Order button click."""
        if self._confirm_end_work_order():
            logger.info("User requested end work order")
            if self._ui_bridge:
                self._ui_bridge.logout_requested.emit()
            self._show_login_dialog()

    def _confirm_close_program(self) -> bool:
        """Show a styled confirmation dialog before closing the application."""
        shop_order = self._lbl_shop_order.text().strip()
        has_work_order = bool(shop_order and shop_order != '---')

        dialog = self._create_styled_message_box(
            title='Close Program',
            text='Close Stinger now?',
            icon=QMessageBox.Icon.Warning,
            informative_text=(
                f'Active work order: {shop_order}\nUnsaved progress may be lost.'
                if has_work_order
                else 'This will close the application.'
            ),
            buttons=(QMessageBox.StandardButton.Close | QMessageBox.StandardButton.Cancel),
            default_button=QMessageBox.StandardButton.Cancel,
            escape_button=QMessageBox.StandardButton.Cancel,
        )

        close_button = dialog.button(QMessageBox.StandardButton.Close)
        if close_button is not None:
            close_button.setText('Close Stinger')
        cancel_button = dialog.button(QMessageBox.StandardButton.Cancel)
        if cancel_button is not None:
            cancel_button.setText('Keep Running')
        return dialog.exec() == QMessageBox.StandardButton.Close
    
    @pyqtSlot()
    def _on_close_program(self) -> None:
        """Handle Close Program button click."""
        if self._confirm_close_program():
            logger.info("User requested close program")
            if self._ui_bridge:
                self._ui_bridge.close_program_requested.emit()
            self.close()

    @pyqtSlot()
    def _on_report_bug(self) -> None:
        """Save an operator bug report with current UI and diagnostic context."""
        dialog = BugReportDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        description, steps = dialog.values()
        if not description:
            self._show_styled_message(
                title='Report Bug',
                text='Please enter a description before saving the report.',
                icon=QMessageBox.Icon.Warning,
            )
            return

        screenshot = self.grab()
        screenshot_bytes = bytes()
        if not screenshot.isNull():
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            screenshot.save(buffer, 'PNG')
            screenshot_bytes = bytes(buffer.data())

        log_cfg = self.config.get('logging', {})
        log_dir = Path(log_cfg.get('log_dir', 'logs'))
        if not log_dir.is_absolute():
            log_dir = Path(__file__).resolve().parents[2] / log_dir
        report = create_bug_report(
            log_dir=log_dir,
            description=description,
            steps=steps,
            screenshot_bytes=screenshot_bytes,
            repo_root=Path(__file__).resolve().parents[2],
            context={
                'equipment_id': self.config.get('test_parameters', {}).get('equipment_id'),
                'hostname': self.config.get('app', {}).get('hostname'),
                'operator': self._lbl_operator.text(),
                'shop_order': self._lbl_shop_order.text(),
                'part': self._lbl_part.text(),
                'sequence': self._lbl_sequence.text(),
                'last_error': self._status_data.get('last_error'),
            },
        )
        self._show_styled_message(
            title='Report Bug',
            text=f'Bug report saved to:\n{report.report_dir}',
            icon=QMessageBox.Icon.Information,
        )
    
    # -------------------------------------------------------------------------
    # Public interface for controller
    # -------------------------------------------------------------------------
    
    def update_work_order_display(self, data: Dict[str, Any]) -> None:
        """Update the work order info display."""
        self._lbl_operator.setText(str(data.get('operator_id', '---')))
        self._lbl_shop_order.setText(str(data.get('shop_order', '---')))
        self._lbl_part.setText(str(data.get('part_id', '---')))
        self._lbl_sequence.setText(str(data.get('sequence_id', '---')))
        self._lbl_process.setText(str(data.get('process_id', data.get('process', '---'))))

        test_mode = bool(data.get('test_mode') or data.get('TestMode'))
        if test_mode:
            if not self._debug_pin_verified:
                self._populate_debug_tab()
            if not self._admin_pin_verified:
                self._populate_admin_tab()

        completed = data.get('completed', 0)
        total = data.get('total', 0)
        self._update_progress_display(completed, total)

        if not data:
            self._port_a_widget.reset_test_display()
            self._port_b_widget.reset_test_display()

    def update_work_order_progress(self, data: Dict[str, Any]) -> None:
        """Update progress only."""
        completed = data.get('completed', 0)
        total = data.get('total', 0)
        self._update_progress_display(completed, total)

    @staticmethod
    def _format_progress_display(completed: Any, total: Any) -> Tuple[str, str, int, int, str]:
        """Format work-order progress while guarding against invalid or overflowed totals."""
        total_value = max(_safe_int(total) or 0, 0)
        completed_value = max(_safe_int(completed) or 0, 0)
        progress_text = f'{completed_value} / {total_value}'
        tooltip = ''

        if total_value > 0:
            raw_percent = (completed_value / total_value) * 100
            percent_text = f'{min(int(round(raw_percent)), 100)}%'
            progress_max = total_value
            progress_value = min(completed_value, total_value)
            if completed_value > total_value:
                overflow = completed_value - total_value
                progress_text = f'{completed_value} / {total_value} (+{overflow})'
                tooltip = (
                    f'Completed results exceed the work order quantity by {overflow}. '
                    'Percent is capped at 100%.'
                )
        else:
            percent_text = '0%'
            progress_max = 1
            progress_value = 0
            if completed_value > 0:
                tooltip = (
                    'Completed results exist for this work order, but the order quantity is 0 or missing.'
                )

        return progress_text, percent_text, progress_max, progress_value, tooltip

    def _update_progress_display(self, completed: int, total: int) -> None:
        progress_text, percent_text, progress_max, progress_value, tooltip = (
            self._format_progress_display(completed, total)
        )
        self._lbl_progress.setText(progress_text)
        self._lbl_progress_percent.setText(percent_text)
        self._lbl_progress.setToolTip(tooltip)
        self._lbl_progress_percent.setToolTip(tooltip)
        self._progress_bar.setToolTip(tooltip)
        self._progress_bar.setRange(0, progress_max)
        self._progress_bar.setValue(progress_value)

    def _on_hardware_status_updated(self, status: Dict[str, Any]) -> None:
        self._status_data["hardware"] = self._format_hardware_status(status)
        self._status_data["hardware_port_a"] = self._format_single_port_status(status, "port_a")
        self._status_data["hardware_port_b"] = self._format_single_port_status(status, "port_b")
        alicat_status = self._format_hardware_component_status(status, "alicat")
        daq_status = self._format_hardware_component_status(status, "daq")
        serial_status = "Live"
        precision_owner = self._format_precision_owner(status.get("precision_owner"))
        precision_queue = self._format_precision_queue(status.get("precision_queue"))
        poll_profile = self._format_alicat_poll_profile(status.get("alicat_poll_divisors"))
        if "hardware_alicat" in self._admin_labels:
            self._apply_status_from_text(self._admin_labels["hardware_alicat"], alicat_status)
        if "hardware_daq" in self._admin_labels:
            self._apply_status_from_text(self._admin_labels["hardware_daq"], daq_status)
        if "hardware_serial" in self._admin_labels:
            self._apply_status_from_text(self._admin_labels["hardware_serial"], serial_status)
        if "precision_owner" in self._admin_labels:
            self._apply_status_from_text(self._admin_labels["precision_owner"], precision_owner)
        if "precision_queue" in self._admin_labels:
            self._apply_status_from_text(self._admin_labels["precision_queue"], precision_queue)
        if "alicat_poll_profile" in self._admin_labels:
            self._apply_status_from_text(self._admin_labels["alicat_poll_profile"], poll_profile)
        self._refresh_status_level()

    def _on_database_status_updated(self, status: Dict[str, Any]) -> None:
        if not status:
            return
        self._update_database_status(
            str(status.get("status", "Unknown")),
            str(status.get("last_write", "--")),
            str(status.get("queue", "0")),
        )

    def _on_ptp_updated(self, details: Dict[str, Any]) -> None:
        if not details:
            if self._ptp_preview is not None:
                self._ptp_preview.setPlainText("")
                self._ptp_preview.setPlaceholderText("No PTP loaded.")
            self._lbl_ptp_setpoint.setText("Setpoint: --")
            self._lbl_ptp_direction.setText("Direction: --")
            self._lbl_ptp_units.setText("Units: --")
            units_label = self._ui_bridge.get_pressure_unit() if self._ui_bridge else "PSIG"
            for panel in self._debug_panels.values():
                panel.set_units_label(units_label)
            if self._debug_units_combo is not None:
                self._debug_units_combo.setCurrentText(units_label)
            return

        part_id = str(details.get("part_id", ""))
        sequence_id = str(details.get("sequence_id", ""))
        source = str(details.get("source", "unknown"))
        units_label = str(details.get("units_label", "PSI") or "PSI")
        params = details.get("params", {}) if isinstance(details.get("params"), dict) else {}

        activation_target = _safe_float(params.get("ActivationTarget"))
        direction = str(params.get("TargetActivationDirection", "--") or "--").strip() or "--"
        if activation_target is None:
            self._lbl_ptp_setpoint.setText("Setpoint: --")
        else:
            self._lbl_ptp_setpoint.setText(f"Setpoint: {activation_target:g}")
        self._lbl_ptp_direction.setText(f"Direction: {direction.title()}")
        self._lbl_ptp_units.setText(f"Units: {units_label}")

        if self._ptp_preview is not None:
            lines = [
                f"Source: {source}",
                f"PartID: {part_id}",
                f"SequenceID: {sequence_id}",
                f"Units: {units_label}",
                "",
                "Parameters:",
            ]
            for key in sorted(params.keys()):
                value = params.get(key)
                lines.append(f"{key}: {value}")
            self._ptp_preview.setPlainText("\n".join(lines))

        for panel in self._debug_panels.values():
            panel.set_units_label(units_label)
        if self._debug_units_combo is not None:
            self._debug_units_combo.setCurrentText(units_label)

        no_pin = _safe_int(params.get("NormallyOpenTerminal"))
        nc_pin = _safe_int(params.get("NormallyClosedTerminal"))
        if no_pin is not None or nc_pin is not None:
            for port_id, panel in self._debug_panels.items():
                mapped_no = _map_db9_pin_to_dio(port_id, no_pin) if no_pin is not None else None
                mapped_nc = _map_db9_pin_to_dio(port_id, nc_pin) if nc_pin is not None else None
                panel.set_switch_pins(mapped_no, mapped_nc)

    def _format_hardware_status(self, status: Dict[str, Any]) -> str:
        if not status:
            return "Unknown"
        entries = []
        for port_id in ("port_a", "port_b"):
            port_status = status.get(port_id)
            if not isinstance(port_status, dict):
                continue
            daq_status = "Unknown"
            alicat_status = "Unknown"
            if isinstance(port_status.get("daq"), dict):
                daq_status = str(port_status["daq"].get("status", "Unknown"))
            if isinstance(port_status.get("alicat"), dict):
                alicat_status = str(port_status["alicat"].get("status", "Unknown"))
            entries.append(f"{port_id.upper()}: DAQ {daq_status}, Alicat {alicat_status}")
        return "; ".join(entries) if entries else "Unknown"

    def _format_hardware_component_status(self, status: Dict[str, Any], key: str) -> str:
        values = []
        for port_id in ("port_a", "port_b"):
            port_status = status.get(port_id)
            if not isinstance(port_status, dict):
                continue
            component = port_status.get(key)
            if isinstance(component, dict):
                values.append(str(component.get("status", "Unknown")))
        return " / ".join(values) if values else "Unknown"

    def _format_single_port_status(self, status: Dict[str, Any], port_id: str) -> str:
        if not is_port_installed(self.config, port_id):
            return 'Not installed'
        port_status = status.get(port_id)
        if not isinstance(port_status, dict):
            return "Unknown"
        daq_status = "Unknown"
        alicat_status = "Unknown"
        if isinstance(port_status.get("daq"), dict):
            daq_status = str(port_status["daq"].get("status", "Unknown"))
        if isinstance(port_status.get("alicat"), dict):
            alicat_status = str(port_status["alicat"].get("status", "Unknown"))
        return f"DAQ {daq_status} | Alicat {alicat_status}"

    @staticmethod
    def _format_precision_owner(owner: Any) -> str:
        owner_text = str(owner or "none").strip().lower()
        if owner_text in {"", "none"}:
            return "Idle"
        if owner_text in {"port_a", "port_b"}:
            return owner_text.replace("_", " ").upper()
        return str(owner)

    @staticmethod
    def _format_precision_queue(queue: Any) -> str:
        if not isinstance(queue, list) or not queue:
            return "Empty"
        display = [str(item).replace("_", " ").upper() for item in queue]
        return ", ".join(display)

    @staticmethod
    def _format_alicat_poll_profile(divisors: Any) -> str:
        if not isinstance(divisors, dict):
            return "Unknown"
        a = divisors.get("port_a")
        b = divisors.get("port_b")
        if a is None and b is None:
            return "Unknown"
        return f"A:{a if a is not None else '-'} B:{b if b is not None else '-'}"

    def get_port_widget(self, port_id: str) -> 'PortColumn':
        """Get the widget for a specific port."""
        if port_id == 'port_a':
            return self._port_a_widget
        elif port_id == 'port_b':
            return self._port_b_widget
        else:
            raise ValueError(f"Unknown port_id: {port_id}")

    def attach_work_order_controller(self, controller: Any) -> None:
        """Bind controller created after the window (same GUI thread)."""
        self.work_order_controller = controller


    def _connect_ui_bridge(self) -> None:
        """Connect UI bridge signals to UI widgets."""
        ui_bridge = cast('UIBridge', self._ui_bridge)
        ui_bridge.work_order_changed.connect(self.update_work_order_display)
        ui_bridge.work_order_progress_updated.connect(self.update_work_order_progress)
        ui_bridge.pressure_updated.connect(self._on_pressure_updated)
        ui_bridge.button_state_changed.connect(self._on_button_state_changed)
        ui_bridge.test_result_ready.connect(self._on_test_result_ready)
        ui_bridge.serial_updated.connect(self._on_serial_updated)
        ui_bridge.pressure_viz_updated.connect(self._on_pressure_viz_updated)
        ui_bridge.switch_state_updated.connect(self._on_switch_state_updated)
        ui_bridge.debug_chart_updated.connect(self._update_debug_chart)
        ui_bridge.debug_dio_updated.connect(self._update_debug_dio)
        ui_bridge.hardware_status_updated.connect(self._on_hardware_status_updated)
        ui_bridge.database_status_updated.connect(self._on_database_status_updated)
        ui_bridge.ptp_updated.connect(self._on_ptp_updated)
        ui_bridge.show_error.connect(self._show_error_message)
        ui_bridge.show_info.connect(self._show_info_message)

        self._port_a_widget.action_requested.connect(self._on_port_action)
        self._port_b_widget.action_requested.connect(self._on_port_action)
        self._port_a_widget.serial_increment_requested.connect(self._on_serial_increment)
        self._port_a_widget.serial_decrement_requested.connect(self._on_serial_decrement)
        self._port_b_widget.serial_increment_requested.connect(self._on_serial_increment)
        self._port_b_widget.serial_decrement_requested.connect(self._on_serial_decrement)
        self._port_a_widget.serial_manual_entry_requested.connect(self._on_serial_manual_entry)
        self._port_b_widget.serial_manual_entry_requested.connect(self._on_serial_manual_entry)


    def _show_login_dialog(self) -> None:
        """Prompt for operator/shop order on startup."""
        self._show_overlay()
        dialog = LoginDialog(self, config=self.config)
        dialog.loginSuccessful.connect(self._on_login_successful)
        result = dialog.exec()
        self._hide_overlay()
        if result != dialog.DialogCode.Accepted:
            self.close()
    
    def _show_overlay(self) -> None:
        """Show grey overlay over main window."""
        if self._overlay_widget is None:
            self._overlay_widget = QWidget(self)
            self._overlay_widget.setStyleSheet("background-color: rgba(0, 0, 0, 120);")
            self._overlay_widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._overlay_widget.setGeometry(self.rect())
        self._overlay_widget.show()
        self._overlay_widget.raise_()
    
    def _hide_overlay(self) -> None:
        """Hide grey overlay."""
        if self._overlay_widget is not None:
            self._overlay_widget.hide()
    
    def resizeEvent(self, a0) -> None:
        """Update overlay size when window is resized."""
        super().resizeEvent(a0)
        if self._overlay_widget is not None and self._overlay_widget.isVisible():
            self._overlay_widget.setGeometry(self.rect())

    def enter_dev_session(self) -> None:
        """Skip the login dialog and load the built-in dev/test work order."""
        custom = self.config.get('custom_work_orders', {})
        dev_key = next(iter(custom), None) if custom else None
        dev_wo = custom.get(dev_key, {}) if dev_key else {}
        payload = {
            'shop_order': dev_key or 'dev',
            'part_id': dev_wo.get('part_id', 'SPS00000'),
            'sequence_id': str(dev_wo.get('sequence_id', '300')),
            'order_qty': dev_wo.get('order_qty', 1),
            'operator_id': 'DEV',
            'test_mode': True,
            'TestMode': True,
        }
        logger.info('Entering dev session without login dialog: %s', payload)
        self._on_login_successful(payload)

    def _on_login_successful(self, payload: Dict[str, Any]) -> None:
        if self._ui_bridge:
            self._ui_bridge.login_requested.emit(payload)

    @pyqtSlot()
    def _on_logout_requested(self) -> None:
        """Handle logout by showing login dialog again."""
        logger.info("Logout requested - showing login dialog")
        self._show_login_dialog()

    def _on_pressure_updated(self, port_id: str, pressure: float, unit: str) -> None:
        self.get_port_widget(port_id).set_pressure(pressure, unit)


    def _on_button_state_changed(self, port_id: str, data: Dict[str, Any]) -> None:
        self.get_port_widget(port_id).set_button_state(
            data.get('primary', {}),
            data.get('cancel', {})
        )

    def _on_test_result_ready(self, port_id: str, result: Dict[str, Any]) -> None:
        unit_label = self._ui_bridge.get_pressure_unit() if self._ui_bridge else "PSI"
        activation = result.get('increasing_activation')
        deactivation = result.get('decreasing_deactivation')
        activation_display = (
            convert_pressure(activation, "PSI", unit_label) if activation is not None else None
        )
        deactivation_display = (
            convert_pressure(deactivation, "PSI", unit_label) if deactivation is not None else None
        )
        self.get_port_widget(port_id).set_result(
            activation_display,
            deactivation_display,
            result.get('in_spec')
        )
        self._append_test_history(port_id, result)


    def _on_serial_updated(self, port_id: str, serial: int) -> None:
        self.get_port_widget(port_id).set_serial(serial)

    def _on_pressure_viz_updated(self, port_id: str, viz_data: Dict[str, Any]) -> None:
        self.get_port_widget(port_id).set_pressure_visualization(viz_data)

    def _on_switch_state_updated(self, port_id: str, no_active: bool, nc_active: bool) -> None:
        self.get_port_widget(port_id).set_switch_state(no_active, nc_active)
        self._update_debug_switches(port_id, no_active, nc_active)

    def _on_port_action(self, port_id: str, action: str) -> None:

        if not self._ui_bridge or not action:
            return
        if action == "start_pressurize":
            self._ui_bridge.start_pressurize_requested.emit(port_id)
        elif action == "start_test":
            self._ui_bridge.start_test_requested.emit(port_id)
        elif action == "vent":
            self._ui_bridge.vent_requested.emit(port_id)
        elif action == "cancel":
            self._ui_bridge.cancel_requested.emit(port_id)
        elif action == "record_success":
            self._ui_bridge.record_success_requested.emit(port_id)
        elif action == "record_failure":
            self._ui_bridge.record_failure_requested.emit(port_id)
        elif action == "retest":
            self._ui_bridge.retest_requested.emit(port_id)
        elif action == "retry_no_switch":
            self._ui_bridge.retest_requested.emit(port_id)
        elif action == "fail_no_switch":
            self._ui_bridge.record_failure_requested.emit(port_id)
        elif action == "reset":
            # Reset from ERROR state - treat as cancel (vents + returns to IDLE)
            self._ui_bridge.cancel_requested.emit(port_id)

    def _on_serial_increment(self, port_id: str) -> None:
        if self._ui_bridge:
            self._ui_bridge.serial_increment_requested.emit(port_id)

    def _on_serial_decrement(self, port_id: str) -> None:
        if self._ui_bridge:
            self._ui_bridge.serial_decrement_requested.emit(port_id)

    def _on_serial_manual_entry(self, port_id: str, serial: int) -> None:
        if self._ui_bridge:
            self._ui_bridge.serial_manual_entry_requested.emit(port_id, serial)

    def _show_error_message(self, title: str, message: str) -> None:
        self._status_data["last_error"] = message
        self._refresh_status_level()
        self._show_styled_message(
            title=title,
            text=message,
            icon=QMessageBox.Icon.Critical,
        )


    def _show_info_message(self, title: str, message: str) -> None:
        self._show_styled_message(
            title=title,
            text=message,
            icon=QMessageBox.Icon.Information,
        )

    def _emit_debug_action(self, port_id: str, action: str, payload: Dict[str, Any]) -> None:
        if self._ui_bridge is None:
            return
        self._ui_bridge.request_debug_action(port_id, action, payload)

    def _emit_admin_action(self, action: str, payload: Dict[str, Any]) -> None:
        if self._ui_bridge is None:
            return
        self._ui_bridge.request_admin_action(action, payload)

    def _on_admin_measurement_source_changed(self, source: str) -> None:
        normalized = str(source or "auto").strip().lower()
        if normalized not in {"auto", "transducer", "alicat"}:
            normalized = "auto"
        self._emit_admin_action(
            "set_main_measurement_source",
            {"preferred_source": normalized},
        )

    def _apply_status_from_text(self, label: QLabel, status: str) -> None:
        label.setText(status)
        level = self._status_text_to_level(status)
        label.setStyleSheet(status_badge_style(level))
