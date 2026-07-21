from __future__ import annotations

from collections.abc import Callable, Mapping

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.calculated_signals import (
    CalculatedSignalDefinition,
    CalculatedSignalError,
    calculate_series,
    parse_formula,
    validate_name,
)
from core.signal_store import SignalSeries


_FORMULA_HELP_SIGNAL = "CH1::Message1::Signal1"


def _formula_help_examples() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return one valid example for every formula operation supported by v1."""
    signal = f"`{_FORMULA_HELP_SIGNAL}`"
    return (
        (
            "Arithmetic",
            (
                f"Addition:          {signal} + 100",
                f"Subtraction:       {signal} - 10",
                f"Multiplication:    {signal} * 100",
                f"Division:          {signal} / 10",
                f"Unary positive:    +{signal}",
                f"Unary negative:    -{signal}",
                f"Parentheses:       ({signal} + 10) * 2",
            ),
        ),
        (
            "Comparisons",
            (
                f"Less than:         {signal} < 100",
                f"Less or equal:     {signal} <= 100",
                f"Greater than:      {signal} > 100",
                f"Greater or equal:  {signal} >= 100",
                f"Equal:             {signal} == 100",
                f"Not equal:         {signal} != 100",
            ),
        ),
        (
            "Logical",
            (
                f"AND:               ({signal} > 0) AND ({signal} < 100)",
                f"OR:                ({signal} == 0) OR ({signal} >= 100)",
            ),
        ),
    )


def _formula_help_text() -> str:
    sections = []
    for heading, examples in _formula_help_examples():
        sections.append(f"{heading}\n{'-' * len(heading)}\n" + "\n".join(examples))
    sections.append(
        "Notes\n-----\n"
        "Use the measurement signal picker to insert exact references.\n"
        "AND and OR are case-insensitive. Zero is false; nonzero is true.\n"
        "Comparison and logical results are numeric 0 or 1.\n"
        "Division by zero and nonfinite results produce NaN (a plot gap)."
    )
    return "\n\n".join(sections)


class FormulaHelpDialog(QDialog):
    """Non-blocking reference window for the calculated-signal formula syntax."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Calculated Signal Formula Help")
        self.resize(780, 620)

        intro = QLabel(
            "These examples use synthetic signal names. Use the measurement signal "
            "picker to replace them with exact references from your measurement."
        )
        intro.setWordWrap(True)

        self.examples_edit = QPlainTextEdit(_formula_help_text())
        self.examples_edit.setReadOnly(True)
        self.examples_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.rejected.connect(self.close)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.examples_edit, stretch=1)
        layout.addWidget(close_box)


class CalculationWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        definition: CalculatedSignalDefinition,
        source_series: Mapping[str, SignalSeries],
    ) -> None:
        super().__init__()
        self._definition = definition
        self._source_series = dict(source_series)

    @Slot()
    def run(self) -> None:
        try:
            self.finished.emit(calculate_series(self._definition, self._source_series))
        except Exception as exc:
            self.failed.emit(str(exc))


class CalculatedSignalDialog(QDialog):
    """Create/edit dialog with exact-key insertion and live validation."""

    def __init__(
        self,
        signal_keys: list[str],
        *,
        existing: CalculatedSignalDefinition | None = None,
        name_validator: Callable[[str], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._signal_keys = list(signal_keys)
        self._name_validator = name_validator
        self._help_dialog: FormulaHelpDialog | None = None
        self.setWindowTitle("Edit Generated Signal" if existing else "New Signal")
        self.resize(760, 560)

        self.name_edit = QLineEdit(existing.name if existing else "")
        self.name_edit.setPlaceholderText("Example: ScaledSpeed")
        if existing is not None:
            self.name_edit.setReadOnly(True)
            self.name_edit.setToolTip("The name is fixed while editing")

        self.unit_edit = QLineEdit(existing.unit if existing else "")
        self.unit_edit.setPlaceholderText("Optional, for example rpm")

        self.formula_edit = QPlainTextEdit(existing.formula if existing else "")
        self.formula_edit.setPlaceholderText("Select a measurement signal, then add an expression")
        self.formula_edit.setMinimumHeight(100)

        self.help_button = QPushButton("Help")
        formula_label = QWidget()
        formula_label_layout = QVBoxLayout(formula_label)
        formula_label_layout.setContentsMargins(0, 0, 0, 0)
        formula_label_layout.addWidget(QLabel("Formula:"))
        formula_label_layout.addWidget(self.help_button)
        formula_label_layout.addStretch(1)

        form = QFormLayout()
        form.addRow("Signal name:", self.name_edit)
        form.addRow("Unit:", self.unit_edit)
        form.addRow(formula_label, self.formula_edit)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search measurement signals...")
        self.signal_list = QListWidget()
        for key in self._signal_keys:
            item = QListWidgetItem(key)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setToolTip(key)
            self.signal_list.addItem(item)

        self.insert_button = QPushButton("Insert Signal")
        self.insert_button.setEnabled(bool(self._signal_keys))
        picker_buttons = QHBoxLayout()
        picker_buttons.addStretch(1)
        picker_buttons.addWidget(self.insert_button)

        self.validation_label = QLabel()
        self.validation_label.setWordWrap(True)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.save_button = self.button_box.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText("Update" if existing else "Create")

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel("Measurement signal picker:"))
        layout.addWidget(self.search_edit)
        layout.addWidget(self.signal_list, stretch=1)
        layout.addLayout(picker_buttons)
        layout.addWidget(self.validation_label)
        layout.addWidget(self.button_box)

        self.search_edit.textChanged.connect(self._filter_signals)
        self.help_button.clicked.connect(self._show_formula_help)
        self.insert_button.clicked.connect(self._insert_selected_signal)
        self.signal_list.itemDoubleClicked.connect(lambda _item: self._insert_selected_signal())
        self.signal_list.itemSelectionChanged.connect(
            lambda: self.insert_button.setEnabled(bool(self.signal_list.selectedItems()))
        )
        self.name_edit.textChanged.connect(self._validate)
        self.formula_edit.textChanged.connect(self._validate)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        self._validate()

    @Slot()
    def _show_formula_help(self) -> None:
        if self._help_dialog is None:
            self._help_dialog = FormulaHelpDialog(self)
        self._help_dialog.show()
        self._help_dialog.raise_()
        self._help_dialog.activateWindow()

    def definition(self) -> CalculatedSignalDefinition:
        return CalculatedSignalDefinition(
            name=validate_name(self.name_edit.text()),
            formula=self.formula_edit.toPlainText().strip(),
            unit=self.unit_edit.text().strip(),
        )

    @Slot(str)
    def _filter_signals(self, text: str) -> None:
        needle = text.strip().casefold()
        for index in range(self.signal_list.count()):
            item = self.signal_list.item(index)
            item.setHidden(bool(needle and needle not in item.text().casefold()))

    @Slot()
    def _insert_selected_signal(self) -> None:
        items = self.signal_list.selectedItems()
        if not items:
            return
        key = str(items[0].data(Qt.ItemDataRole.UserRole))
        cursor = self.formula_edit.textCursor()
        before = self.formula_edit.toPlainText()
        token = f"`{key}`"
        if before and cursor.position() > 0 and not before[cursor.position() - 1].isspace():
            token = " " + token
        cursor.insertText(token)
        self.formula_edit.setTextCursor(cursor)
        self.formula_edit.setFocus()

    @Slot()
    def _validate(self) -> None:
        try:
            name = validate_name(self.name_edit.text())
            if self._name_validator is not None:
                self._name_validator(name)
            parse_formula(self.formula_edit.toPlainText(), self._signal_keys)
        except CalculatedSignalError as exc:
            self.validation_label.setStyleSheet("color: #b00020;")
            self.validation_label.setText(str(exc))
            self.save_button.setEnabled(False)
            return
        self.validation_label.setStyleSheet("color: #187a28;")
        self.validation_label.setText("Formula is valid")
        self.save_button.setEnabled(True)
