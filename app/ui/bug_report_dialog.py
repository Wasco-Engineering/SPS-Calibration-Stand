"""Modal dialog for operator bug reports."""

from __future__ import annotations

from typing import Optional, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)


class BugReportDialog(QDialog):
    """Collect a short description and optional repro steps."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Report Bug')
        self.setModal(True)
        self.setMinimumWidth(480)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        layout = QVBoxLayout(self)
        intro = QLabel(
            'Describe what went wrong. A screenshot of this window and the '
            'current session logs will be saved under logs/bug_reports/.'
        )
        intro.setWordWrap(True)
        intro.setFont(QFont('Segoe UI, Inter, Arial', 11))
        layout.addWidget(intro)

        form = QFormLayout()
        self._description = QPlainTextEdit()
        self._description.setPlaceholderText('What happened?')
        self._description.setFixedHeight(100)
        form.addRow('Description', self._description)

        self._steps = QPlainTextEdit()
        self._steps.setPlaceholderText('Optional: steps to reproduce')
        self._steps.setFixedHeight(80)
        form.addRow('Steps', self._steps)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText('Save Report')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> Tuple[str, str]:
        """Return (description, steps)."""
        return (
            self._description.toPlainText().strip(),
            self._steps.toPlainText().strip(),
        )
