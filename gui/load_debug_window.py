"""Session-only plain-text window for measurement load forensics."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class LoadDebugWorker(QObject):
    """Run read-only inspection callables away from the GUI thread.

    One instance lives for the whole debug session and handles every
    inspection, so the owning QThread is never created or destroyed per
    inspection. Requests arrive on ``run_inspection`` as a queued signal.
    """

    completed = Signal(str, str)
    failed = Signal(str, str)

    @Slot(object)
    def run_inspection(
        self, payload: tuple[str, int, Callable[[], str]]
    ) -> None:
        # The generation rides along so the payload is self-describing, but
        # only the caller compares it — one inspection runs at a time.
        kind, _generation, inspector = payload
        try:
            self.completed.emit(kind, inspector())
        except Exception as exc:
            import traceback

            self.failed.emit(
                kind,
                f"{type(exc).__module__}.{type(exc).__name__}: {exc}\n\n"
                f"{traceback.format_exc()}",
            )


class LoadDebugWindow(QMainWindow):
    """Modeless debug console designed for readable screenshots."""

    openMeasurementRequested = Signal()
    openDatabaseRequested = Signal()
    loadDecodeRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("CANScope CAN Load Debug")
        self.resize(1500, 900)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        controls = QHBoxLayout()
        self.mode_label = QLabel("DEBUG MODE: ON")
        self.mode_label.setStyleSheet(
            "color: #ff6060; font-weight: bold; font-size: 14px;"
        )
        controls.addWidget(self.mode_label)
        controls.addStretch(1)

        open_measurement = QPushButton("Open Measurement")
        open_database = QPushButton("Open Database")
        load_decode = QPushButton("Load + Decode")
        copy_text = QPushButton("Copy Text")
        save_report = QPushButton("Save Report...")
        clear_text = QPushButton("Clear")
        hide_window = QPushButton("Hide")
        controls.addWidget(open_measurement)
        controls.addWidget(open_database)
        controls.addWidget(load_decode)
        controls.addWidget(copy_text)
        controls.addWidget(save_report)
        controls.addWidget(clear_text)
        controls.addWidget(hide_window)
        layout.addLayout(controls)

        self.state_label = QLabel(
            "Select the issue measurement and database. "
            "Inspection starts immediately after each selection."
        )
        self.state_label.setWordWrap(True)
        layout.addWidget(self.state_label)

        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(9)
        self.report.setFont(font)
        self.report.setPlainText(
            "CANScope LOAD DEBUG\n"
            + "=" * 100
            + "\nSTATUS: waiting for a measurement file"
        )
        layout.addWidget(self.report, 1)
        self.setCentralWidget(central)

        open_measurement.clicked.connect(self.openMeasurementRequested.emit)
        open_database.clicked.connect(self.openDatabaseRequested.emit)
        load_decode.clicked.connect(self.loadDecodeRequested.emit)
        copy_text.clicked.connect(self.copy_all)
        save_report.clicked.connect(self.save_report_as)
        clear_text.clicked.connect(self.clear_report)
        hide_window.clicked.connect(self.hide)

    def set_busy(self, message: str) -> None:
        self.state_label.setText(message)

    def set_report(self, text: str) -> None:
        self.report.setPlainText(text)
        self.report.moveCursor(QTextCursor.MoveOperation.Start)
        self.state_label.setText("Inspection complete.")

    def append_report(self, text: str) -> None:
        if not text:
            return
        cursor = self.report.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if self.report.document().characterCount() > 1:
            cursor.insertText("\n")
        cursor.insertText(text)
        self.report.setTextCursor(cursor)
        self.report.ensureCursorVisible()

    def clear_report(self) -> None:
        self.report.setPlainText(
            "CANScope LOAD DEBUG\n"
            + "=" * 100
            + "\nSTATUS: waiting for inspection"
        )
        self.state_label.setText("Debug report cleared.")

    def copy_all(self) -> None:
        QApplication.clipboard().setText(self.report.toPlainText())
        self.state_label.setText("Debug text copied to the clipboard.")

    def save_report_as(self) -> None:
        """Write the report out, only ever on an explicit click.

        The report is otherwise session-only — nothing about debug mode
        touches the disk on its own.
        """
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Debug Report", "canscope_debug_report.txt",
            "Text files (*.txt)",
        )
        if not path:
            return
        try:
            Path(path).write_text(self.report.toPlainText(), encoding="utf-8")
        except OSError as exc:
            self.state_label.setText(f"Could not save the report: {exc}")
            return
        self.state_label.setText(f"Debug report saved to {path}")

    def closeEvent(self, event) -> None:
        # Closing the report window only hides it; Ctrl+Alt+D owns the mode.
        event.ignore()
        self.hide()
