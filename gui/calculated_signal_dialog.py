from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.calculated_signals import (
    _FUNCTIONS,
    TIME_SIGNAL_KEY,
    CalculatedSignalDefinition,
    CalculatedSignalError,
    FunctionSpec,
    ImportReport,
    calculate_series,
    formula_references,
    parse_formula,
    validate_name,
)
from core.formula_library import load_formula_library, save_formula_library
from core.signal_store import SignalSeries


_FORMULA_HELP_SIGNAL = "CH1::Message1::Signal1"


def _function_example(spec: FunctionSpec) -> str:
    """A runnable call for this function: the signal first, small integers after."""
    signal = f"`{_FORMULA_HELP_SIGNAL}`"
    fillers = ["2", "3", "4"]
    args = [signal, *fillers[: spec.max_args - 1]]
    return f"{spec.name}({', '.join(args)})"


def _formula_help_examples() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """One valid example for every formula operation, hand-written operators first.

    The function-derived sections are generated from core.calculated_signals._FUNCTIONS
    so this text and the registry cannot drift apart; see test_calculated_signals.py.
    """
    signal = f"`{_FORMULA_HELP_SIGNAL}`"
    sections: list[tuple[str, tuple[str, ...]]] = [
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
                f"NOT:               NOT ({signal} > 0)",
            ),
        ),
        (
            "Bitwise",
            (
                f"AND:               {signal} & 15",
                f"OR:                {signal} | 1",
                f"XOR:               {signal} ^ 255",
                f"Left shift:        {signal} << 2",
                f"Right shift:       {signal} >> 2",
                f"NOT (invert):      ~{signal}",
                f"With a comparison: ({signal} > 0) & ({signal} < 100)",
            ),
        ),
    ]

    by_category: dict[str, list[str]] = {}
    for spec in _FUNCTIONS.values():
        entry = (
            f"{spec.signature}\n"
            f"    {spec.description}\n"
            f"    Example: {_function_example(spec)}\n"
        )
        by_category.setdefault(spec.category, []).append(entry)
    for category in sorted(by_category):
        sections.append((category, tuple(by_category[category])))

    return tuple(sections)


def _formula_help_text() -> str:
    sections = []
    for heading, examples in _formula_help_examples():
        sections.append(f"{heading}\n{'-' * len(heading)}\n" + "\n".join(examples))
    sections.append(
        "Notes\n-----\n"
        "Use the measurement signal picker to insert exact references.\n"
        "AND and OR are case-insensitive. Zero is false; nonzero is true.\n"
        "Comparison and logical results are numeric 0 or 1.\n"
        "Division by zero and nonfinite results produce NaN (a plot gap).\n"
        "Function names are case-insensitive; NOT is too. IF is written uppercase "
        "for consistency with AND/OR/NOT — lowercase if(...) is rejected with a "
        "hint, since if is a Python keyword.\n"
        "mod(a, b) takes the sign of b, matching Python's % (not C's fmod).\n"
        "~ inverts a 64-bit signed integer, so ~0 is -1.\n"
        "Mixing a bitwise operator with a comparison needs both sides "
        "parenthesized: (a > 0) & (b > 0), not a > 0 & b > 0.\n"
        "Domain errors such as sqrt of a negative number or log of zero also "
        "produce NaN, the same as division by zero.\n"
        "lag(signal, samples) counts samples on that source signal; samples must "
        "be a nonnegative whole number. delay(signal, seconds) uses zero-order "
        "hold at t - seconds. Both produce NaN before enough history exists.\n"
        "rolling_sum(signal, samples) includes the current source sample and "
        "requires a complete positive-size window. integral(signal) accumulates "
        "value x elapsed seconds using zero-order hold and starts at zero.\n"
        "pi and e are available as constants."
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

        self._full_help_text = _formula_help_text()

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search functions and examples...")

        self.examples_edit = QPlainTextEdit(self._full_help_text)
        self.examples_edit.setReadOnly(True)
        self.examples_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.rejected.connect(self.close)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.search_edit)
        layout.addWidget(self.examples_edit, stretch=1)
        layout.addWidget(close_box)

        self.search_edit.textChanged.connect(self._filter_examples)

    @Slot(str)
    def _filter_examples(self, text: str) -> None:
        needle = text.strip().casefold()
        if not needle:
            self.examples_edit.setPlainText(self._full_help_text)
            return
        matches = [
            line for line in self._full_help_text.splitlines() if needle in line.casefold()
        ]
        self.examples_edit.setPlainText("\n".join(matches) if matches else "No matches.")


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
        generated_keys: Iterable[str] = (),
        excluded_keys: Iterable[str] = (),
        library_definitions: Callable[[], list[CalculatedSignalDefinition]] | None = None,
        import_definitions: Callable[[list[CalculatedSignalDefinition], bool], ImportReport]
        | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # Time is a synthetic measurement input whose values are the elapsed
        # timestamps in seconds. Keep it first so it is easy to find.
        self._signal_keys = [TIME_SIGNAL_KEY]
        self._signal_keys.extend(key for key in signal_keys if key != TIME_SIGNAL_KEY)
        # Excluded keys are the signal itself plus everything downstream of it, so a
        # formula built in this dialog can never close a dependency cycle.
        self._excluded_keys = set(excluded_keys)
        self._generated_keys = [
            key for key in generated_keys if key not in self._excluded_keys
        ]
        self._available_keys = self._signal_keys + self._generated_keys
        self._name_validator = name_validator
        self._library_definitions = library_definitions
        self._import_definitions = import_definitions
        self._help_dialog: FormulaHelpDialog | None = None
        self._library_picker: FormulaLibraryPickerDialog | None = None
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

        self.load_library_button = QPushButton("Load...")
        self.save_library_button = QPushButton("Save...")
        library_available = library_definitions is not None and import_definitions is not None
        if not library_available:
            tooltip = "Formula library loading and saving is not available here"
            self.load_library_button.setEnabled(False)
            self.load_library_button.setToolTip(tooltip)
            self.save_library_button.setEnabled(False)
            self.save_library_button.setToolTip(tooltip)
        library_buttons = QHBoxLayout()
        library_buttons.addWidget(self.load_library_button)
        library_buttons.addWidget(self.save_library_button)
        library_buttons.addStretch(1)

        form = QFormLayout()
        form.addRow("Signal name:", self.name_edit)
        form.addRow("Unit:", self.unit_edit)
        form.addRow(library_buttons)
        form.addRow(formula_label, self.formula_edit)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search signals...")
        self.signal_list = QListWidget()
        self._section_headers: list[tuple[QListWidgetItem, list[QListWidgetItem]]] = []
        self._add_signal_section("Measurement signals", self._signal_keys)
        self._add_signal_section("Generated signals", self._generated_keys)

        self.insert_button = QPushButton("Insert Signal")
        self.insert_button.setEnabled(bool(self._available_keys))
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
        layout.addWidget(QLabel("Signal picker:"))
        layout.addWidget(self.search_edit)
        layout.addWidget(self.signal_list, stretch=1)
        layout.addLayout(picker_buttons)
        layout.addWidget(self.validation_label)
        layout.addWidget(self.button_box)

        self.search_edit.textChanged.connect(self._filter_signals)
        self.help_button.clicked.connect(self._show_formula_help)
        self.load_library_button.clicked.connect(self._on_load_library)
        self.save_library_button.clicked.connect(self._on_save_library)
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

    def _add_signal_section(self, title: str, keys: list[str]) -> None:
        header = QListWidgetItem(title)
        header.setFlags(Qt.ItemFlag.NoItemFlags)
        font = header.font()
        font.setBold(True)
        header.setFont(font)
        self.signal_list.addItem(header)
        items: list[QListWidgetItem] = []
        for key in keys:
            item = QListWidgetItem(key)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setToolTip(key)
            self.signal_list.addItem(item)
            items.append(item)
        header.setHidden(not items)
        self._section_headers.append((header, items))

    @Slot()
    def _show_formula_help(self) -> None:
        if self._help_dialog is None:
            self._help_dialog = FormulaHelpDialog(self)
        self._help_dialog.show()
        self._help_dialog.raise_()
        self._help_dialog.activateWindow()

    @Slot()
    def _on_load_library(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load formula library",
            "",
            "Formula files (*.formulas.json *.json);;All files (*)",
        )
        if not path:
            return
        try:
            result = load_formula_library(path)
        except CalculatedSignalError as exc:
            QMessageBox.critical(self, "Load formula library failed", str(exc))
            return
        if result.errors:
            QMessageBox.warning(
                self,
                "Some formulas could not be read",
                "\n".join(result.errors),
            )
        if not result.definitions:
            QMessageBox.information(
                self,
                "No formulas found",
                "This file does not contain any usable formulas.",
            )
            return
        self._library_picker = FormulaLibraryPickerDialog(
            result.definitions,
            signal_keys=self._available_keys,
            import_definitions=self._import_definitions,
            parent=self,
        )
        self._library_picker.use_requested.connect(self._apply_library_definition)
        self._library_picker.show()
        self._library_picker.raise_()
        self._library_picker.activateWindow()

    @Slot(object)
    def _apply_library_definition(self, definition: CalculatedSignalDefinition) -> None:
        if not self.name_edit.isReadOnly():
            self.name_edit.setText(definition.name)
        self.unit_edit.setText(definition.unit)
        self.formula_edit.setPlainText(definition.formula)
        self._validate()

    def _current_definition_if_valid(self) -> CalculatedSignalDefinition | None:
        if not self.save_button.isEnabled():
            return None
        try:
            return self.definition()
        except CalculatedSignalError:
            return None

    @Slot()
    def _on_save_library(self) -> None:
        definitions = list(self._library_definitions()) if self._library_definitions else []

        current = self._current_definition_if_valid()
        if current is not None:
            answer = QMessageBox.question(
                self,
                "Include current formula?",
                f"Include the formula currently being edited ('{current.name}') "
                "in the saved file?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Yes:
                definitions = [
                    d for d in definitions if d.name.casefold() != current.name.casefold()
                ]
                definitions.append(current)

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save formula library",
            "canscope_formulas.formulas.json",
            "Formula files (*.formulas.json *.json);;All files (*)",
        )
        if not path:
            return
        try:
            save_formula_library(path, definitions)
        except OSError as exc:
            QMessageBox.critical(self, "Save formula library failed", str(exc))
            return
        QMessageBox.information(
            self,
            "Formula library saved",
            f"Saved {len(definitions)} formula(s):\n{path}",
        )

    def definition(self) -> CalculatedSignalDefinition:
        return CalculatedSignalDefinition(
            name=validate_name(self.name_edit.text()),
            formula=self.formula_edit.toPlainText().strip(),
            unit=self.unit_edit.text().strip(),
        )

    @Slot(str)
    def _filter_signals(self, text: str) -> None:
        needle = text.strip().casefold()
        for header, items in self._section_headers:
            for item in items:
                item.setHidden(bool(needle and needle not in item.text().casefold()))
            header.setHidden(all(item.isHidden() for item in items))

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

    def _reject_excluded_references(self) -> None:
        if not self._excluded_keys:
            return
        for key in formula_references(self.formula_edit.toPlainText()):
            if key in self._excluded_keys:
                name = key.rsplit("::", 1)[-1]
                raise CalculatedSignalError(
                    f"Cannot reference '{name}': it would create a circular reference"
                )

    @Slot()
    def _validate(self) -> None:
        try:
            name = validate_name(self.name_edit.text())
            if self._name_validator is not None:
                self._name_validator(name)
            self._reject_excluded_references()
            parse_formula(self.formula_edit.toPlainText(), self._available_keys)
        except CalculatedSignalError as exc:
            self.validation_label.setStyleSheet("color: #b00020;")
            self.validation_label.setText(str(exc))
            self.save_button.setEnabled(False)
            return
        self.validation_label.setStyleSheet("color: #187a28;")
        self.validation_label.setText("Formula is valid")
        self.save_button.setEnabled(True)


class FormulaLibraryPickerDialog(QDialog):
    """Lists formulas loaded from a formula-library file for reuse or import.

    Both buttons import: *Import Selected* takes the ticked rows, *Import All*
    takes every row regardless of its tick. Double-clicking a row instead loads
    that one formula into the editor behind this dialog (`use_requested`).
    """

    use_requested = Signal(object)

    def __init__(
        self,
        definitions: list[CalculatedSignalDefinition],
        *,
        signal_keys: list[str],
        import_definitions: Callable[[list[CalculatedSignalDefinition], bool], ImportReport]
        | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._definitions = list(definitions)
        self._signal_keys = list(signal_keys)
        self._import_definitions = import_definitions
        self.setWindowTitle("Formula Library")
        self.resize(760, 480)

        self.table = QTableWidget(len(self._definitions), 4)
        self.table.setHorizontalHeaderLabels(["☑", "Name", "Unit", "Formula"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)

        for row, definition in enumerate(self._definitions):
            check_item = QTableWidgetItem()
            check_item.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            check_item.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, check_item)
            self.table.setItem(row, 1, QTableWidgetItem(definition.name))
            self.table.setItem(row, 2, QTableWidgetItem(definition.unit))
            self.table.setItem(row, 3, QTableWidgetItem(definition.formula))
        if self._definitions:
            self.table.selectRow(0)

        self.import_selected_button = QPushButton("Import Selected")
        self.import_selected_button.setToolTip("Import only the formulas you have ticked")
        self.import_all_button = QPushButton("Import All")
        self.import_all_button.setToolTip("Import every formula in this file, ticked or not")
        if self._import_definitions is None:
            self.import_selected_button.setToolTip(
                "Importing into this measurement is not available"
            )
            self.import_all_button.setToolTip("Importing into this measurement is not available")
        self.import_all_button.setEnabled(
            self._import_definitions is not None and bool(self._definitions)
        )
        self.close_button = QPushButton("Close")

        buttons = QHBoxLayout()
        buttons.addWidget(self.import_selected_button)
        buttons.addWidget(self.import_all_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"{len(self._definitions)} formula(s) found:"))
        layout.addWidget(self.table, stretch=1)
        layout.addLayout(buttons)

        # Wired after the table is populated so the initial setItem calls do not
        # fire it.
        self.table.itemChanged.connect(self._sync_import_selected_enabled)
        self._sync_import_selected_enabled()
        # Both buttons import now, so double-clicking a row is the remaining way
        # to pull one formula into the editor behind this dialog.
        self.table.itemDoubleClicked.connect(self._on_row_double_clicked)
        self.import_selected_button.clicked.connect(self._on_import_selected)
        self.import_all_button.clicked.connect(self._on_import_all)
        self.close_button.clicked.connect(self.close)

    @Slot()
    def _sync_import_selected_enabled(self) -> None:
        self.import_selected_button.setEnabled(
            self._import_definitions is not None and bool(self._checked_definitions())
        )

    def _checked_definitions(self) -> list[CalculatedSignalDefinition]:
        result = []
        for row, definition in enumerate(self._definitions):
            item = self.table.item(row, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                result.append(definition)
        return result

    @Slot(object)
    def _on_row_double_clicked(self, item: QTableWidgetItem) -> None:
        row = item.row()
        if row < 0 or row >= len(self._definitions):
            return
        self.use_requested.emit(self._definitions[row])
        self.accept()

    @Slot()
    def _on_import_selected(self) -> None:
        checked = self._checked_definitions()
        if not checked:
            QMessageBox.information(self, "Import formulas", "Check at least one formula to import.")
            return
        self._import(checked)

    @Slot()
    def _on_import_all(self) -> None:
        if not self._definitions:
            return
        self._import(list(self._definitions))

    def _import(self, checked: list[CalculatedSignalDefinition]) -> None:
        if self._import_definitions is None:
            return

        missing = 0
        for definition in checked:
            try:
                parsed = parse_formula(definition.formula)
            except CalculatedSignalError:
                continue
            if any(reference not in self._signal_keys for reference in parsed.references):
                missing += 1
        if missing:
            answer = QMessageBox.question(
                self,
                "Formulas reference missing signals",
                f"{missing} of {len(checked)} formulas reference signals not present in "
                "this measurement. They will be imported but cannot be calculated until "
                "a matching measurement is loaded.\n\nImport anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        report = self._import_definitions(checked, False)
        imported = list(report.imported)
        skipped = list(report.skipped)
        errors = list(report.errors)

        if skipped:
            answer = QMessageBox.question(
                self,
                "Formulas already exist",
                f"{len(skipped)} formula(s) already exist and were skipped: "
                f"{', '.join(skipped)}.\n\nReplace them with the imported versions?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                skipped_fold = {name.casefold() for name in skipped}
                retry = [d for d in checked if d.name.casefold() in skipped_fold]
                retry_report = self._import_definitions(retry, True)
                imported.extend(retry_report.imported)
                replaced_fold = {name.casefold() for name in retry_report.imported}
                skipped = [name for name in skipped if name.casefold() not in replaced_fold]
                errors.extend(retry_report.errors)

        summary = f"{len(imported)} imported, {len(skipped)} skipped"
        if skipped:
            summary += ": name already exists"
        if errors:
            summary += "\n" + "\n".join(errors)
        QMessageBox.information(self, "Import formulas", summary)
        self.accept()
