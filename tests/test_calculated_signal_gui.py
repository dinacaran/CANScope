from __future__ import annotations

import array
import time

import pytest

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from core.calculated_signals import CalculatedSignalDefinition, ImportReport
from core.calculated_signals import parse_formula
from core.formula_library import load_formula_library, save_formula_library
from core.signal_store import SignalSeries
from gui.calculated_signal_dialog import (
    CalculatedSignalDialog,
    _FORMULA_HELP_SIGNAL,
    _formula_help_examples,
)
from gui.main_window import MainWindow


class _Store:
    def __init__(self, series: list[SignalSeries]) -> None:
        self._series = {item.key: item for item in series}
        self.raw_frame_store = None

    def all_keys(self):
        return sorted(self._series)

    def get_series(self, key):
        return self._series.get(key)


def _series(name: str, values) -> SignalSeries:
    return SignalSeries(
        channel=1,
        message_name="Message",
        message_id=1,
        signal_name=name,
        unit="V",
        timestamps=array.array("d", [0.0, 1.0, 2.0]),
        values=array.array("d", values),
    )


@pytest.fixture()
def window(qapp, monkeypatch):
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: QMessageBox.StandardButton.Ok)
    widget = MainWindow("CANScope", "00.00.99")
    widget.store = _Store([_series("A", [1.0, 2.0, 3.0])])
    widget._update_action_states()
    yield widget
    _wait_for_calculation(widget, qapp)
    widget.close()
    qapp.processEvents()


def _wait_for_calculation(window: MainWindow, qapp: QApplication) -> None:
    deadline = time.monotonic() + 5.0
    while window._calc_thread is not None and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    assert window._calc_thread is None


def test_stacked_plot_is_enabled_by_default(window):
    assert window.btn_stacked.isChecked()
    assert window.plot_panel._stacked_mode

    window.add_signals_to_plot([window.store.all_keys()[0]])

    assert window.plot_panel.view_stack.currentIndex() == 1


def test_data_point_toggle_thins_then_restores_curve(window):
    key = window.store.all_keys()[0]
    window.add_signals_to_plot([key])
    window.plot_panel.table.clearSelection()
    window.plot_panel._refresh_highlight()
    plotted = window.plot_panel._items[key]

    assert plotted.curve.opts["pen"].widthF() == pytest.approx(2.8)

    window.btn_points.setChecked(True)
    assert plotted.curve.opts["pen"].widthF() == pytest.approx(1.2)
    assert window.btn_points.text() == "Hide Data Points"

    window.btn_points.setChecked(False)
    assert plotted.curve.opts["pen"].widthF() == pytest.approx(2.8)
    assert window.btn_points.text() == "Show Data Points"


def test_hide_line_requires_data_points_and_restores_line(window):
    key = window.store.all_keys()[0]
    window.add_signals_to_plot([key])
    plotted = window.plot_panel._items[key]
    window.plot_panel._POINTS_VISIBLE_THRESHOLD = 2

    assert not window.btn_hide_line.isEnabled()
    assert not window.btn_hide_line.isChecked()
    window.btn_hide_line.setChecked(True)
    assert not window.btn_hide_line.isChecked()

    window.btn_points.setChecked(True)
    assert window.btn_hide_line.isEnabled()

    window.btn_hide_line.setChecked(True)
    assert window.plot_panel._hide_lines
    assert plotted.curve.opts["pen"].style() == Qt.PenStyle.NoPen
    assert plotted.scatter is not None
    point_x, _point_y = plotted.scatter.getData()
    assert len(point_x) == 2
    assert window.btn_hide_line.text() == "Hide Line"

    window.btn_points.setChecked(False)
    assert not window.btn_hide_line.isEnabled()
    assert not window.btn_hide_line.isChecked()
    assert not window.plot_panel._hide_lines
    assert plotted.curve.opts["pen"].style() != Qt.PenStyle.NoPen
    assert plotted.curve.opts["pen"].widthF() == pytest.approx(2.8)
    assert window.btn_hide_line.text() == "Hide Line"


def test_background_create_is_cached_but_not_auto_plotted(window, qapp):
    source_key = window.store.all_keys()[0]
    definition = CalculatedSignalDefinition("Scaled", f"`{source_key}` * 10", "V")

    window._queue_calculation(definition, "create", plot_after=False)
    assert window._calc_thread is not None
    assert not window._toolbar_actions["New Signal"].isEnabled()
    assert window._toolbar_actions["Load Config"].isEnabled()
    _wait_for_calculation(window, qapp)

    assert window.calculated_signals.definition(definition.key) == definition
    assert list(window.calculated_signals.cached_series(definition.key).values) == [10.0, 20.0, 30.0]
    assert definition.key not in window.plot_panel.plotted_keys()
    assert window._toolbar_actions["New Signal"].isEnabled()
    assert window._toolbar_actions["Load Config"].isEnabled()


def test_load_config_action_is_always_enabled(window):
    action = window._toolbar_actions["Load Config"]

    assert window.store is not None
    assert action.isEnabled()

    action.setEnabled(False)
    window._update_action_states()

    assert action.isEnabled()


def test_lazy_calculation_plots_then_delete_releases_it(window, qapp, monkeypatch):
    source_key = window.store.all_keys()[0]
    definition = CalculatedSignalDefinition("Lazy", f"`{source_key}` + 5")
    window.calculated_signals.commit(definition)
    window._refresh_generated_signal_tree()
    window._pending_plot_colors[definition.key] = "#123456"
    window._pending_plot_visible[definition.key] = False
    window._pending_plot_groups[definition.key] = "Restored"
    window._pending_plot_axis_visible[definition.key] = False
    window._pending_plot_own_axis[definition.key] = True

    assert window.add_signal_to_plot(definition.key) is False
    _wait_for_calculation(window, qapp)

    assert definition.key in window.plot_panel.plotted_keys()
    plotted = window.plot_panel._items[definition.key]
    assert plotted.color == "#123456"
    assert plotted.visible is False
    assert plotted.group == "Restored"
    assert plotted.axis_visible is False
    assert plotted.own_axis is True
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    window.delete_generated_signal(definition.key)

    assert not window.calculated_signals.contains_key(definition.key)
    assert definition.key not in window.plot_panel.plotted_keys()
    assert all(
        definition.key not in snapshot
        for snapshot, _current_key in window.plot_panel._undo_stack
    )


def test_large_preflight_can_cancel_before_worker_starts(window, monkeypatch):
    source_key = window.store.all_keys()[0]
    definition = CalculatedSignalDefinition("Large", f"`{source_key}` * 2")
    monkeypatch.setattr("gui.main_window.LARGE_OUTPUT_WARNING_POINTS", 1)
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args, **kwargs: QMessageBox.StandardButton.Cancel,
    )

    window._queue_calculation(definition, "create", plot_after=False)

    assert window._calc_thread is None
    assert not window.calculated_signals.contains_key(definition.key)


@pytest.mark.parametrize("mode", ["normal", "multi", "stacked"])
def test_replace_generated_series_preserves_time_and_cursors(qapp, mode):
    from gui.plot_widget import PlotPanel

    panel = PlotPanel()
    first = _series("Generated", [1.0, 2.0, 3.0])
    replacement = _series("Generated", [10.0, 20.0, 30.0])
    key = "CH?::Generate Signals::Generated"
    panel.add_series(key, first)
    if mode == "multi":
        panel.set_multi_axis(True)
    elif mode == "stacked":
        panel.set_stacked(True)
    panel.set_cursor1_enabled(True)
    panel.set_cursor2_enabled(True)
    panel.v_line.setPos(0.7)
    panel.v_line2.setPos(1.4)
    if panel._stacked_mode:
        panel._stacked_plots[0].setXRange(0.25, 1.75, padding=0)
    else:
        panel.plot.setXRange(0.25, 1.75, padding=0)
    qapp.processEvents()

    before_x = panel._visible_x_range()
    panel._push_undo()
    assert panel.replace_series(key, replacement)
    qapp.processEvents()
    qapp.processEvents()

    assert panel._visible_x_range() == pytest.approx(before_x)
    assert panel.v_line.value() == pytest.approx(0.7)
    assert panel.v_line2.value() == pytest.approx(1.4)
    assert panel._items[key].color
    assert all(
        snapshot[key].series is replacement
        for snapshot, _current_key in panel._undo_stack
        if key in snapshot
    )
    panel.close()


def test_generated_signal_name_is_italic_in_plotted_signal_list(qapp):
    from gui.plot_widget import PlotPanel

    panel = PlotPanel()
    generated_key = "CH?::Generate Signals::CalculatedSpeed"
    measurement_key = "CH1::EngineControl::EngSpeed"
    try:
        panel.add_series(generated_key, _series("CalculatedSpeed", [1.0, 2.0, 3.0]))
        panel.add_series(measurement_key, _series("EngSpeed", [4.0, 5.0, 6.0]))
        qapp.processEvents()

        generated_row = panel._row_lookup[generated_key]
        measurement_row = panel._row_lookup[measurement_key]
        assert panel.table.item(generated_row, 1).font().italic()
        assert not panel.table.item(measurement_row, 1).font().italic()

        panel.set_multi_axis(True)
        qapp.processEvents()
        generated_row = panel._row_lookup[generated_key]
        assert panel.table.item(generated_row, 1).font().italic()

        panel.set_stacked(True)
        qapp.processEvents()
        generated_row = panel._row_lookup[generated_key]
        assert panel.table.item(generated_row, 1).font().italic()
    finally:
        panel.close()


def test_formula_help_has_valid_example_for_every_supported_operation(qapp):
    sections = dict(_formula_help_examples())

    assert len(sections["Arithmetic"]) == 7
    assert len(sections["Comparisons"]) == 6
    assert len(sections["Logical"]) == 2

    for examples in sections.values():
        for labelled_example in examples:
            formula = labelled_example.split(":", 1)[1].strip()
            parse_formula(formula, [_FORMULA_HELP_SIGNAL])


def test_help_button_displays_formula_examples(qapp):
    signal_key = "CH1::Message::A"
    dialog = CalculatedSignalDialog([signal_key])
    try:
        assert dialog.help_button.text() == "Help"
        dialog.help_button.click()
        qapp.processEvents()

        assert dialog._help_dialog is not None
        assert dialog._help_dialog.isVisible()
        help_text = dialog._help_dialog.examples_edit.toPlainText()
        assert "Arithmetic" in help_text
        assert "Comparisons" in help_text
        assert "Logical" in help_text
        assert f"`{_FORMULA_HELP_SIGNAL}` + 100" in help_text
        assert signal_key not in help_text
    finally:
        dialog.close()


def test_library_buttons_disabled_without_callables(qapp):
    dialog = CalculatedSignalDialog(["CH1::Message::A"])
    try:
        assert not dialog.load_library_button.isEnabled()
        assert not dialog.save_library_button.isEnabled()
        assert dialog.load_library_button.toolTip()
        assert dialog.save_library_button.toolTip()
    finally:
        dialog.close()


def test_load_library_use_selected_fills_fields_in_create_mode(qapp, monkeypatch, tmp_path):
    signal_key = "CH1::Message::A"
    picked = [CalculatedSignalDefinition("Picked", f"`{signal_key}` * 2", "rpm")]
    path = tmp_path / "lib.formulas.json"
    save_formula_library(path, picked)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))

    dialog = CalculatedSignalDialog(
        [signal_key],
        library_definitions=lambda: [],
        import_definitions=lambda defs, overwrite: ImportReport([], [], []),
    )
    try:
        dialog.load_library_button.click()
        qapp.processEvents()
        picker = dialog._library_picker
        assert picker is not None
        picker.table.selectRow(0)
        picker.use_button.click()
        qapp.processEvents()

        assert dialog.name_edit.text() == "Picked"
        assert dialog.unit_edit.text() == "rpm"
        assert dialog.formula_edit.toPlainText() == f"`{signal_key}` * 2"
        assert "valid" in dialog.validation_label.text().lower()
    finally:
        dialog.close()


def test_load_library_use_selected_leaves_name_untouched_in_edit_mode(qapp, monkeypatch, tmp_path):
    signal_key = "CH1::Message::A"
    picked = [CalculatedSignalDefinition("Picked", f"`{signal_key}` * 2", "rpm")]
    path = tmp_path / "lib.formulas.json"
    save_formula_library(path, picked)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))

    existing = CalculatedSignalDefinition("Original", f"`{signal_key}` + 1", "V")
    dialog = CalculatedSignalDialog(
        [signal_key],
        existing=existing,
        library_definitions=lambda: [],
        import_definitions=lambda defs, overwrite: ImportReport([], [], []),
    )
    try:
        assert dialog.name_edit.isReadOnly()
        dialog.load_library_button.click()
        qapp.processEvents()
        picker = dialog._library_picker
        assert picker is not None
        picker.table.selectRow(0)
        picker.use_button.click()
        qapp.processEvents()

        assert dialog.name_edit.text() == "Original"
        assert dialog.unit_edit.text() == "rpm"
        assert dialog.formula_edit.toPlainText() == f"`{signal_key}` * 2"
    finally:
        dialog.close()


def test_import_checked_warns_about_missing_signal_references(qapp, monkeypatch, tmp_path):
    calls = []

    def fake_import(definitions, overwrite):
        calls.append(overwrite)
        return ImportReport(imported=[d.name for d in definitions], skipped=[], errors=[])

    present_key = "CH1::Message::A"
    missing_key = "CH1::Message::Missing"
    library_defs = [CalculatedSignalDefinition("UsesMissing", f"`{missing_key}` + 1", "")]
    path = tmp_path / "lib.formulas.json"
    save_formula_library(path, library_defs)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))

    questions = []

    def fake_question(*args, **kwargs):
        questions.append(args[2] if len(args) > 2 else "")
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", fake_question)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.StandardButton.Ok)

    dialog = CalculatedSignalDialog(
        [present_key],
        library_definitions=lambda: [],
        import_definitions=fake_import,
    )
    try:
        dialog.load_library_button.click()
        qapp.processEvents()
        picker = dialog._library_picker
        picker.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
        picker.import_button.click()
        qapp.processEvents()

        assert calls == [False]
        assert any("not present in this measurement" in q for q in questions)
    finally:
        dialog.close()


def test_import_checked_reports_collisions_and_offers_replace(qapp, monkeypatch, tmp_path):
    calls = []

    def fake_import(definitions, overwrite):
        calls.append((list(definitions), overwrite))
        if not overwrite:
            return ImportReport(imported=[], skipped=[d.name for d in definitions], errors=[])
        return ImportReport(imported=[d.name for d in definitions], skipped=[], errors=[])

    signal_key = "CH1::Message::A"
    library_defs = [CalculatedSignalDefinition("Existing", f"`{signal_key}` + 1", "V")]
    path = tmp_path / "lib.formulas.json"
    save_formula_library(path, library_defs)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    informed = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda *a, **k: informed.append(a[2]) or QMessageBox.StandardButton.Ok,
    )

    dialog = CalculatedSignalDialog(
        [signal_key],
        library_definitions=lambda: [],
        import_definitions=fake_import,
    )
    try:
        dialog.load_library_button.click()
        qapp.processEvents()
        picker = dialog._library_picker
        picker.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
        picker.import_button.click()
        qapp.processEvents()

        assert len(calls) == 2
        assert calls[0][1] is False
        assert calls[1][1] is True
        assert calls[1][0][0].name == "Existing"
        assert informed[-1] == "1 imported, 0 skipped"
    finally:
        dialog.close()


def test_save_library_writes_definitions_and_includes_current_formula(qapp, monkeypatch, tmp_path):
    signal_key = "CH1::Message::A"
    existing_defs = [CalculatedSignalDefinition("Saved", f"`{signal_key}` + 1", "V")]
    save_path = tmp_path / "out.formulas.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(save_path), ""))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.StandardButton.Ok)

    dialog = CalculatedSignalDialog(
        [signal_key],
        library_definitions=lambda: list(existing_defs),
        import_definitions=lambda defs, overwrite: ImportReport([], [], []),
    )
    try:
        dialog.name_edit.setText("Fresh")
        dialog.formula_edit.setPlainText(f"`{signal_key}` * 2")
        qapp.processEvents()
        assert dialog.save_button.isEnabled()

        dialog.save_library_button.click()
        qapp.processEvents()

        result = load_formula_library(save_path)
        assert sorted(d.name for d in result.definitions) == ["Fresh", "Saved"]
    finally:
        dialog.close()
