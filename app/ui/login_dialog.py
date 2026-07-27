"""
Login Dialog for the Stinger Test Stand.

Allows users to enter Operator ID and Shop Order to start a session.
Validates the Shop Order against the database and displays details.
Based on the Functional Stand Login Dialog.
"""
import sys
import logging
import re
from typing import Dict, Any, Optional, Tuple

from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QFormLayout,
    QMessageBox,
    QCheckBox,
    QInputDialog,
)
from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot, QTimer
from PyQt6.QtGui import QFont, QPalette, QColor

from ..database.operations import (
    describe_database_connectivity,
    is_calibration_database_available,
    is_shop_order_database_available,
    validate_shop_order,
)
from app.services import run_async

logger = logging.getLogger(__name__)

VALIDATION_DEBOUNCE_MS = 300
SHOP_ORDER_MIN_VALIDATE_LEN = 4
SHOP_ORDER_MAX_LEN = 10
OPERATOR_ID_MAX_LEN = 5
PART_ID_MAX_LEN = 30
SEQUENCE_MAX_LEN = 4


class LoginDialog(QDialog):
    """
    Dialog for user login and Work Order validation.
    Refactored UI and validation flow based on Functional Stand.
    """

    # Signal emitted upon successful login, passing Operator ID and WO details
    loginSuccessful = pyqtSignal(dict)

    def __init__(self, parent=None, config: Optional[Dict[str, Any]] = None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.WindowTitleHint
        )
        self.config = config or {}
        self.work_order_details: Optional[Dict[str, Any]] = None

        # Timer for debounced validation
        self.validation_timer = QTimer(self)
        self.validation_timer.setSingleShot(True)
        self.validation_timer.timeout.connect(self._validate_shop_order)

        self._test_mode_enabled = False
        self._manual_entry_mode = False
        self._test_mode_pin = self.config.get("ui", {}).get("admin_pin", "2245")
        self._validation_workers: set = set()
        self._part_id_user_edited = False
        self._sequence_user_edited = False

        self.setWindowTitle("Operator Login")
        self.setModal(True)
        self.setMinimumWidth(450)
        self._apply_dark_theme()

        self.setup_ui()
        self.connect_signals()

        # Initial state
        self.login_button.setEnabled(False)
        self._set_shop_order_validity(None)
        self._update_login_button_state()

        # Automatically focus on badge scan when Nexus is enabled, else operator ID
        nexus_enabled = bool((self.config.get("nexus") or {}).get("enabled", False))
        if nexus_enabled:
            self.badge_token_input.setFocus()
        else:
            self.operator_id_input.setFocus()

    def showEvent(self, a0) -> None:
        """Center the dialog on the parent window or screen when shown."""
        super().showEvent(a0)
        parent_widget = self.parentWidget()
        if parent_widget is not None:
            # Center on parent window
            try:
                parent_geometry = parent_widget.geometry()
                dialog_geometry = self.geometry()
                x = parent_geometry.x() + (parent_geometry.width() - dialog_geometry.width()) // 2
                y = parent_geometry.y() + (parent_geometry.height() - dialog_geometry.height()) // 2
                self.move(x, y)
                return
            except AttributeError:
                pass
        
        # Center on screen if no parent or parent doesn't have geometry
        screen_obj = QApplication.primaryScreen()
        if screen_obj is None:
            return
        screen = screen_obj.geometry()
        dialog_geometry = self.geometry()
        x = (screen.width() - dialog_geometry.width()) // 2
        y = (screen.height() - dialog_geometry.height()) // 2
        self.move(x, y)

    def _apply_dark_theme(self) -> None:
        """Apply the application's light cleanroom theme to the login dialog."""
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#f0f2f5"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#1a1a2e"))
        palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
        palette.setColor(QPalette.ColorRole.Text, QColor("#1a1a2e"))
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#9ca3af"))
        palette.setColor(QPalette.ColorRole.Button, QColor("#e5e7eb"))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor("#1a1a2e"))
        self.setPalette(palette)
        self.setAutoFillBackground(True)

        self.setStyleSheet("""
            QDialog { 
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
                padding: 12px 16px;
                font-size: 15px;
            }
            QLineEdit:focus {
                border: 2px solid #2563eb;
                background-color: #ffffff;
            }
            QLineEdit:disabled {
                background-color: #f3f4f6;
                color: #9ca3af;
            }
        """)

    def setup_ui(self) -> None:
        """Set up the UI components for the dialog."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.setSpacing(16)

        # Title
        title_label = QLabel("Operator Login")
        title_font = QFont("Segoe UI, Inter, Arial", 24, QFont.Weight.Bold)
        title_label.setFont(title_font)
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setStyleSheet("color: #1a1a2e; margin-bottom: 6px;")
        layout.addWidget(title_label)
        
        # Subtitle
        subtitle_label = QLabel("Scan badge or enter credentials to begin")
        subtitle_font = QFont("Segoe UI, Inter, Arial", 13)
        subtitle_label.setFont(subtitle_font)
        subtitle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle_label.setStyleSheet("color: #6b7280; margin-bottom: 12px;")
        layout.addWidget(subtitle_label)

        # Main Form Layout
        form_layout = QFormLayout()
        form_layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form_layout.setHorizontalSpacing(16)
        form_layout.setVerticalSpacing(14)

        label_font = QFont()
        label_font.setPointSize(14)
        label_font.setWeight(QFont.Weight.Medium)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        input_font = QFont()
        input_font.setPointSize(14)

        # Create input fields
        self.badge_token_input = self._create_input_field("Scan badge (Nexus)", input_font)
        form_layout.addRow("Badge Scan:", self.badge_token_input)

        self.operator_id_input = self._create_input_field("Scan or Enter Operator ID", input_font)
        form_layout.addRow("Operator ID:", self.operator_id_input)

        self.shop_order_input = self._create_input_field("Scan or Enter Work Order", input_font)
        form_layout.addRow("Work Order:", self.shop_order_input)

        self.part_id_input = self._create_input_field("Auto-populated", input_font, read_only=True)
        form_layout.addRow("Part ID:", self.part_id_input)

        self.sequence_input = self._create_input_field("Auto-populated", input_font)
        form_layout.addRow("Sequence:", self.sequence_input)

        self.order_qty_input = self._create_input_field("Auto-populated", input_font, read_only=True)
        form_layout.addRow("Order Qty:", self.order_qty_input)

        for row in range(form_layout.rowCount()):
            label_widget = form_layout.itemAt(row, QFormLayout.ItemRole.LabelRole).widget()
            if isinstance(label_widget, QLabel):
                label_widget.setFont(label_font)

        layout.addLayout(form_layout)

        # Status Label
        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setMinimumHeight(30)
        self.status_label.setStyleSheet("font-size: 13px;")
        layout.addWidget(self.status_label)

        # Test mode toggle (centered)
        test_mode_container = QHBoxLayout()
        test_mode_container.addStretch()
        self.test_mode_checkbox = QCheckBox("Test Mode (PIN required)")
        self.test_mode_checkbox.setStyleSheet(
            "color: #1a1a2e; font-weight: bold; font-size: 14px;"
        )
        test_mode_container.addWidget(self.test_mode_checkbox)
        test_mode_container.addStretch()
        layout.addLayout(test_mode_container)

        layout.addSpacing(6)

        # Buttons Layout
        button_layout = QHBoxLayout()
        button_layout.setSpacing(24)

        button_font = QFont()
        button_font.setPointSize(13)
        button_font.setWeight(QFont.Weight.Bold)

        self.login_button = QPushButton("Login")
        self.login_button.setDefault(True)
        self.login_button.setFont(button_font)
        self.login_button.setMinimumSize(150, 52)
        self.login_button.setStyleSheet(
            "QPushButton {"
            " background-color: #16a34a; color: white;"
            " font-size: 13pt; font-weight: bold;"
            " padding: 12px; border-radius: 7px;"
            " border: none;"
            "}"
            "QPushButton:hover { background-color: #15803d; }"
            "QPushButton:disabled { background-color: #d1d5db; color: #9ca3af; }"
        )

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setFont(button_font)
        self.cancel_button.setMinimumSize(150, 52)
        self.cancel_button.setStyleSheet(
            "QPushButton {"
            " background-color: #dc2626; color: white;"
            " font-size: 13pt; font-weight: bold;"
            " padding: 12px; border-radius: 7px;"
            " border: none;"
            "}"
            "QPushButton:hover { background-color: #b91c1c; }"
            "QPushButton:disabled { background-color: #d1d5db; color: #9ca3af; }"
        )

        button_layout.addStretch(1)
        button_layout.addWidget(self.login_button)
        button_layout.addWidget(self.cancel_button)
        button_layout.addStretch(1)
        layout.addLayout(button_layout)

    def connect_signals(self) -> None:
        """Connect UI signals to slots."""
        self.cancel_button.clicked.connect(self.reject)
        self.login_button.clicked.connect(self.attempt_login)

        # Update login button state when Operator ID changes
        self.badge_token_input.textChanged.connect(self._on_badge_changed)
        self.operator_id_input.textChanged.connect(self._update_login_button_state)
        self.part_id_input.textChanged.connect(self._update_login_button_state)
        self.sequence_input.textChanged.connect(self._update_login_button_state)
        # Connect validation to textChanged for debounced validation
        self.shop_order_input.textChanged.connect(self._schedule_validation)
        self.shop_order_input.textChanged.connect(self._on_shop_order_changed)
        self.part_id_input.textEdited.connect(self._on_part_id_edited)
        self.sequence_input.textEdited.connect(self._on_sequence_edited)
        # Force uppercase for Operator ID
        self.operator_id_input.textEdited.connect(self._force_uppercase)

        # Handle Enter key presses
        self.badge_token_input.returnPressed.connect(self._on_badge_enter)
        self.operator_id_input.returnPressed.connect(self._on_operator_id_enter)
        self.shop_order_input.returnPressed.connect(self._on_shop_order_enter)
        self.part_id_input.returnPressed.connect(self._on_part_id_enter)
        self.sequence_input.returnPressed.connect(self._on_sequence_enter)
        self.order_qty_input.returnPressed.connect(self.attempt_login)
        self.test_mode_checkbox.stateChanged.connect(self._on_test_mode_toggled)

    def _update_login_button_state(self) -> None:
        """Enable login button based on mode: test, manual-entry, or validated WO."""
        has_badge = bool(self.badge_token_input.text().strip())
        operator_id_ok = has_badge or bool(self.operator_id_input.text().strip())
        part_ok = bool(self.part_id_input.text().strip())
        sequence_ok = bool(self.sequence_input.text().strip())
        shop_order_validated = self.work_order_details is not None
        shop_order_entered = bool(self.shop_order_input.text().strip())
        if self._test_mode_enabled:
            # In test mode, only require operator ID / badge (other fields will use defaults)
            can_login = operator_id_ok
        elif self._manual_entry_mode:
            # Manual entry: need operator/badge, WO text, and manually-entered Part ID + Sequence
            can_login = operator_id_ok and shop_order_entered and part_ok and sequence_ok
        else:
            can_login = operator_id_ok and shop_order_validated and part_ok and sequence_ok
        self.login_button.setEnabled(can_login)
        logger.debug(
            "Login button state updated: Operator OK=%s, Badge=%s, WO Validated=%s, "
            "Manual Entry=%s, Test Mode=%s -> Enabled=%s",
            operator_id_ok,
            has_badge,
            shop_order_validated,
            self._manual_entry_mode,
            self._test_mode_enabled,
            can_login,
        )

    @pyqtSlot(str)
    def _on_badge_changed(self, text: str) -> None:
        """When a badge is present, operator ID is filled by Nexus on submit."""
        has_badge = bool(text.strip())
        self.operator_id_input.setEnabled(not has_badge)
        if has_badge:
            self.operator_id_input.setPlaceholderText("Filled by badge login")
        else:
            self.operator_id_input.setPlaceholderText("Scan or Enter Operator ID")
        self._update_login_button_state()

    @pyqtSlot()
    def _on_badge_enter(self) -> None:
        """Handle Enter in Badge field — move to Work Order or login."""
        if not self.badge_token_input.text().strip():
            self.operator_id_input.setFocus()
            return
        if self.shop_order_input.text().strip() or self._test_mode_enabled:
            self.attempt_login()
            return
        self.shop_order_input.setFocus()

    @pyqtSlot()
    def _schedule_validation(self) -> None:
        """Schedule validation to run shortly after user stops typing."""
        if self._test_mode_enabled:
            return
        self.validation_timer.stop()
        self.validation_timer.start(VALIDATION_DEBOUNCE_MS)

    @pyqtSlot(str)
    def _on_shop_order_changed(self, _text: str) -> None:
        """Reset manual field edit guards when WO input changes."""
        self._part_id_user_edited = False
        self._sequence_user_edited = False

    @pyqtSlot(str)
    def _on_part_id_edited(self, _text: str) -> None:
        self._part_id_user_edited = True

    @pyqtSlot(str)
    def _on_sequence_edited(self, _text: str) -> None:
        self._sequence_user_edited = True

    @pyqtSlot(int)
    def _on_test_mode_toggled(self, state: int) -> None:
        if state == Qt.CheckState.Checked.value:
            if not self._prompt_for_pin():
                self.test_mode_checkbox.blockSignals(True)
                self.test_mode_checkbox.setChecked(False)
                self.test_mode_checkbox.blockSignals(False)
                return
            self._test_mode_enabled = True
            self._manual_entry_mode = False
            self.validation_timer.stop()
            self._set_shop_order_validity(None)
            self.status_label.setText("Test mode enabled - DB/PTP validation skipped.")
            self.status_label.setStyleSheet("color: #d97706; font-weight: bold;")
            self.work_order_details = None
            self.part_id_input.setReadOnly(False)
            self.sequence_input.setReadOnly(False)
            self.order_qty_input.setReadOnly(False)
        else:
            self._test_mode_enabled = False
            if not self._manual_entry_mode and self.work_order_details is None:
                self.part_id_input.setReadOnly(True)
            self.order_qty_input.setReadOnly(True)
            self.status_label.setText("")
            if self.shop_order_input.text().strip():
                self._schedule_validation()
        self._update_login_button_state()

    def _prompt_for_pin(self) -> bool:
        dialog = QInputDialog(self)
        dialog.setWindowTitle('Test Mode PIN')
        dialog.setLabelText('Enter PIN:')
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
        return bool(accepted and dialog.textValue() == self._test_mode_pin)

    @pyqtSlot()
    def _on_operator_id_enter(self) -> None:
        """Handle Enter key press in Operator ID field - move focus to Work Order."""
        if self.operator_id_input.text().strip():
            self.shop_order_input.setFocus()
            return
        self._show_warning_dialog("Input Missing", "Please enter an Operator ID.")

    @pyqtSlot()
    def _on_shop_order_enter(self) -> None:
        """Handle Enter in Work Order field - trigger validation and move focus forward."""
        if not self.shop_order_input.text().strip():
            self._show_warning_dialog("Input Missing", "Please enter a Work Order.")
            return

        if not self._test_mode_enabled:
            self.validation_timer.stop()
            self._validate_shop_order()
        self.part_id_input.setFocus()

    @pyqtSlot()
    def _on_part_id_enter(self) -> None:
        """Handle Enter in Part ID field - move focus to Sequence."""
        self.sequence_input.setFocus()

    @pyqtSlot()
    def _on_sequence_enter(self) -> None:
        """Handle Enter in Sequence field - move focus to Order Qty or login."""
        if self._test_mode_enabled or self._manual_entry_mode:
            self.order_qty_input.setFocus()
            return
        self.attempt_login()

    def _show_warning_dialog(self, title: str, message: str) -> None:
        """Show a warning dialog styled to match the rest of the login UI."""
        dialog = QMessageBox(self)
        dialog.setWindowTitle(title)
        dialog.setText(message)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
        dialog.setDefaultButton(QMessageBox.StandardButton.Ok)
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
        dialog.exec()

    @pyqtSlot()
    def _validate_shop_order(self) -> None:
        """Validate the Shop Order input against the database when text changes."""
        if self._test_mode_enabled:
            return
        shop_order = self.shop_order_input.text().strip()

        if not shop_order:
            self._set_shop_order_validity(None)
            self._clear_details()
            self.status_label.setText("")
            self.work_order_details = None
            self._manual_entry_mode = False
            self._update_login_button_state()
            return

        if len(shop_order) < SHOP_ORDER_MIN_VALIDATE_LEN or not re.fullmatch(r'[A-Za-z0-9_-]+', shop_order):
            # Avoid hammering DB/logs while operators are still typing or scanner payload is partial.
            self._set_shop_order_validity(None)
            self.work_order_details = None
            self._manual_entry_mode = False
            self.status_label.setText('Enter full Shop Order to validate.')
            self.status_label.setStyleSheet("color: #4b5563;")
            self._clear_details()
            self._update_login_button_state()
            return

        self.status_label.setText(f"Validating Shop Order '{shop_order}'...")
        self.status_label.setStyleSheet("color: #4b5563;")

        def _run_validation() -> tuple[Optional[Dict[str, Any]], bool, bool]:
            validation_result = None
            nexus_cfg = self.config.get("nexus") or {}
            if bool(nexus_cfg.get("enabled", False)):
                try:
                    from app.services.nexus_service import (
                        build_nexus_client,
                        resolve_work_order_via_nexus,
                    )

                    client = build_nexus_client(self.config)
                    if client is not None:
                        try:
                            validation_result = resolve_work_order_via_nexus(client, shop_order)
                            if not validation_result.get("PartID"):
                                validation_result = None
                        finally:
                            try:
                                client.close()
                            except Exception:
                                pass
                except Exception as exc:
                    logger.warning("Nexus WO resolve failed during login validate: %s", exc)
            if validation_result is None:
                validation_result = validate_shop_order(shop_order)
            wasco_ok = is_calibration_database_available()
            max_ok = is_shop_order_database_available()
            # Nexus success counts as online enough to skip offline-manual path.
            if validation_result and validation_result.get("PartID"):
                wasco_ok = True
            return validation_result, wasco_ok, max_ok

        def _on_validation_done(result: Any, error: Optional[Exception]) -> None:
            self._validation_workers.discard(worker)
            if shop_order != self.shop_order_input.text().strip():
                return
            if error is not None:
                logger.error("Error during database validation call: %s", error, exc_info=True)
                validation_result, wasco_ok, max_ok = None, False, False
            else:
                validation_result, wasco_ok, max_ok = result

            if validation_result:
                self._manual_entry_mode = False
                self.work_order_details = validation_result
                message = "Shop Order Validated."
                logger.info("Shop Order '%s' validated.", shop_order)
                self._update_details(validation_result)
                self._set_shop_order_validity(True)
                self.status_label.setText(message)
            elif wasco_ok or max_ok:
                # WO not found but at least one SQL source is reachable — allow manual entry
                self._manual_entry_mode = True
                self.work_order_details = None
                message = (
                    f"'{shop_order}' not found \u2014 "
                    "enter Part ID and Sequence manually"
                )
                logger.info("Manual entry mode enabled: %s", message)
                self._prepare_manual_entry()
                # Amber indicator for manual-entry state
                palette = self.shop_order_input.palette()
                palette.setColor(QPalette.ColorRole.Base, QColor("#fef3c7"))
                palette.setColor(QPalette.ColorRole.Text, QColor("#92400e"))
                self.shop_order_input.setPalette(palette)
                self.shop_order_input.update()
                self.shop_order_input.repaint()
                self.status_label.setText(message)
                self.status_label.setStyleSheet(
                    "color: #d97706; font-weight: bold;"
                )
            else:
                # SQL unreachable — allow offline manual entry (PTP from local dumps)
                self._manual_entry_mode = True
                self.work_order_details = None
                db_detail = describe_database_connectivity()
                message = (
                    f"{db_detail} "
                    "You may enter Part ID and Sequence manually; "
                    "results will not save until SQL is restored."
                )
                logger.warning(
                    "Database unavailable (%s); manual entry for shop order %s",
                    db_detail,
                    shop_order,
                )
                self._prepare_manual_entry()
                palette = self.shop_order_input.palette()
                palette.setColor(QPalette.ColorRole.Base, QColor("#fef3c7"))
                palette.setColor(QPalette.ColorRole.Text, QColor("#92400e"))
                self.shop_order_input.setPalette(palette)
                self._set_shop_order_validity(None)
                self.status_label.setText(message)
                self.status_label.setStyleSheet(
                    "color: #d97706; font-weight: bold;"
                )

            self._update_login_button_state()

        worker = run_async(_run_validation, _on_validation_done)
        self._validation_workers.add(worker)

    @pyqtSlot()
    def attempt_login(self) -> None:
        """Handle the login button click (validation should already be done)."""
        badge_token = self.badge_token_input.text().strip()
        operator_id = self.operator_id_input.text().strip()
        part_id = self.part_id_input.text().strip()
        sequence_id = self.sequence_input.text().strip()
        shop_order = self.shop_order_input.text().strip()

        if not badge_token and not operator_id:
            self._show_warning_dialog(
                "Input Missing",
                "Please scan a badge or enter an Operator ID.",
            )
            return
        if not badge_token and len(operator_id) > OPERATOR_ID_MAX_LEN:
            self._show_warning_dialog(
                "Operator ID Too Long",
                f"Operator ID must be {OPERATOR_ID_MAX_LEN} characters or fewer "
                "to save results to SQL Server.",
            )
            return
        if shop_order and len(shop_order) > SHOP_ORDER_MAX_LEN:
            self._show_warning_dialog(
                "Work Order Too Long",
                f"Work Order must be {SHOP_ORDER_MAX_LEN} characters or fewer "
                "to save results to SQL Server.",
            )
            return
        if part_id and len(part_id) > PART_ID_MAX_LEN:
            self._show_warning_dialog(
                "Part ID Too Long",
                f"Part ID must be {PART_ID_MAX_LEN} characters or fewer to save results.",
            )
            return
        if sequence_id and len(sequence_id.strip()) > SEQUENCE_MAX_LEN:
            self._show_warning_dialog(
                "Sequence Too Long",
                f"Sequence must be {SEQUENCE_MAX_LEN} characters or fewer.",
            )
            return
        if self._test_mode_enabled:
            # In test mode, use defaults if Part ID/Sequence not provided
            if not part_id:
                part_id = "TEST-MODE"
            if not sequence_id:
                sequence_id = "1"
        elif self._manual_entry_mode:
            # Manual entry — require Part ID and Sequence (qty is optional)
            if not part_id or not sequence_id:
                self._show_warning_dialog(
                    "Input Missing",
                    "Part ID and Sequence are required for manual entry.\n"
                    "Enter a valid Part ID and Sequence to load PTP data.",
                )
                return
        else:
            if not self.work_order_details:
                self._show_warning_dialog(
                    "Validation Missing",
                    "Shop Order must be validated successfully first.",
                )
                return
            validated_part_id = str(self.work_order_details.get("PartID", "")).strip()
            if validated_part_id and part_id != validated_part_id:
                self.part_id_input.setText(validated_part_id)
                self._show_warning_dialog(
                    "Part ID Mismatch",
                    "Part ID comes from the validated Work Order. "
                    "Cancel and rescan the Work Order if this part is not correct.",
                )
                return
            part_id = validated_part_id or part_id
            if not sequence_id:
                sequence_id = str(self.work_order_details.get("SequenceID", "")).strip()
            if not part_id or not sequence_id:
                self._show_warning_dialog("Input Missing", "Part ID and Sequence must be set.")
                return

        shop_order_for_log = (
            self.work_order_details.get("ShopOrder", "N/A")
            if self.work_order_details
            else shop_order or "N/A"
        )
        logger.info(
            "Login attempt successful for Operator: %s, WO: %s (badge=%s)",
            operator_id or "(badge)",
            shop_order_for_log,
            bool(badge_token),
        )

        full_wo_details = (self.work_order_details or {}).copy()
        full_wo_details["OperatorID"] = operator_id
        full_wo_details["ShopOrder"] = shop_order
        full_wo_details["PartID"] = part_id
        full_wo_details["SequenceID"] = sequence_id
        full_wo_details["OrderQTY"] = self._parse_order_qty()
        full_wo_details["OrderQty"] = full_wo_details["OrderQTY"]
        full_wo_details["TestMode"] = self._test_mode_enabled
        full_wo_details["ManualEntry"] = self._manual_entry_mode
        full_wo_details["WOValidated"] = self.work_order_details is not None
        if badge_token:
            full_wo_details["BadgeToken"] = badge_token

        self.loginSuccessful.emit(full_wo_details)
        self.accept()

    def _set_shop_order_validity(self, is_valid: Optional[bool]) -> None:
        """Update the visual style of the shop order input."""
        palette = self.shop_order_input.palette()
        if is_valid is True:
            palette.setColor(QPalette.ColorRole.Base, QColor("#dcfce7"))
            palette.setColor(QPalette.ColorRole.Text, QColor("#166534"))
            self.status_label.setStyleSheet("color: #16a34a; font-weight: bold;")
        elif is_valid is False:
            palette.setColor(QPalette.ColorRole.Base, QColor("#fee2e2"))
            palette.setColor(QPalette.ColorRole.Text, QColor("#991b1b"))
            self.status_label.setStyleSheet("color: #dc2626; font-weight: bold;")
        else:
            palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
            palette.setColor(QPalette.ColorRole.Text, QColor("#1a1a2e"))
            self.status_label.setStyleSheet("color: #4b5563;")
        self.shop_order_input.setPalette(palette)
        self.shop_order_input.update()
        self.shop_order_input.repaint()

    def _update_details(self, details: Dict[str, Any]) -> None:
        """Populate the detail LineEdit fields with validated WO details."""
        self.part_id_input.setReadOnly(True)
        self.sequence_input.setReadOnly(False)
        self._part_id_user_edited = False
        self.part_id_input.setText(str(details.get("PartID", "N/A")))
        sequence = str(details.get("SequenceID") or "").strip()
        if not self._sequence_user_edited:
            if sequence:
                self.sequence_input.setText(sequence)
            else:
                self.sequence_input.clear()
                self.sequence_input.setPlaceholderText("Enter Sequence")
        order_qty = details.get("OrderQTY", details.get("OrderQty", "N/A"))
        self.order_qty_input.setText(str(order_qty))
        self.order_qty_input.setReadOnly(not self._test_mode_enabled)
        if not sequence:
            self.sequence_input.setFocus()

    def _clear_details(self) -> None:
        """Clear the detail LineEdit fields."""
        self._part_id_user_edited = False
        self._sequence_user_edited = False
        self.part_id_input.setReadOnly(not self._test_mode_enabled)
        self.sequence_input.setReadOnly(False)
        for field, placeholder in [
            (self.part_id_input, "Auto-populated"),
            (self.sequence_input, "Auto-populated"),
            (self.order_qty_input, "Auto-populated"),
        ]:
            field.clear()
            field.setPlaceholderText(placeholder)
        self.order_qty_input.setReadOnly(not self._test_mode_enabled)

    def _prepare_manual_entry(self) -> None:
        """Prepare fields for manual entry (WO not found, user fills Part/Sequence)."""
        self.part_id_input.setReadOnly(False)
        self.sequence_input.setReadOnly(False)
        if not self._part_id_user_edited:
            self.part_id_input.clear()
        if not self._sequence_user_edited:
            self.sequence_input.clear()
        self.order_qty_input.clear()
        if not self.part_id_input.text().strip():
            self.part_id_input.setPlaceholderText("Enter Part ID")
        if not self.sequence_input.text().strip():
            self.sequence_input.setPlaceholderText("Enter Sequence")
        self.order_qty_input.setPlaceholderText("Optional")
        self.order_qty_input.setReadOnly(False)

    def get_work_order_details(self) -> Optional[Dict[str, Any]]:
        """Return the validated work order details if available."""
        return self.work_order_details

    def get_operator_id(self) -> Optional[str]:
        """Return the entered operator ID if login was successful."""
        if self.result() == QDialog.DialogCode.Accepted:
            return self.operator_id_input.text().strip()
        return None

    def get_login_details(self) -> Optional[Tuple[str, dict]]:
        """Return operator ID and validated WO details if login was successful."""
        if self.result() == QDialog.DialogCode.Accepted and self.work_order_details:
            operator_id = self.operator_id_input.text().strip()
            if operator_id:
                return operator_id, self.work_order_details
        return None

    def clear_inputs(self) -> None:
        """Clear inputs, validation status, and details."""
        self.validation_timer.stop()
        self.operator_id_input.clear()
        self.shop_order_input.clear()
        self.status_label.setText("")
        self._clear_details()
        self._set_shop_order_validity(None)
        self.work_order_details = None
        self._manual_entry_mode = False
        self._update_login_button_state()
        self.operator_id_input.setFocus()

    def _create_input_field(self, placeholder: str, font: QFont, read_only: bool = False) -> QLineEdit:
        """Create a styled input field."""
        input_field = QLineEdit()
        input_field.setPlaceholderText(placeholder)
        input_field.setFont(font)
        input_field.setMinimumHeight(50)
        input_field.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if read_only:
            input_field.setReadOnly(True)
        # Apply light theme styling
        palette = input_field.palette()
        palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
        palette.setColor(QPalette.ColorRole.Text, QColor("#1a1a2e"))
        input_field.setPalette(palette)
        return input_field

    def _force_uppercase(self, text: str) -> None:
        """Force text in the sender QLineEdit to uppercase."""
        sender = self.sender()
        if isinstance(sender, QLineEdit):
            current_pos = sender.cursorPosition()
            sender.setText(text.upper())
            sender.setCursorPosition(current_pos)

    def _parse_order_qty(self) -> int:
        text = self.order_qty_input.text().strip()
        try:
            return int(float(text))
        except (TypeError, ValueError):
            return 0


if __name__ == "__main__":
    print("WARNING: Running login_dialog.py directly is not recommended.")
    app = QApplication(sys.argv)
    dialog = LoginDialog()
    dialog.show()
    sys.exit(app.exec())
