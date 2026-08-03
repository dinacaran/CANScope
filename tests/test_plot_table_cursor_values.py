"""
Regression tests: _refresh_table() blanks the Cursor 1/2 readout columns for
every row in the signal table. Any handler that calls it must repopulate
those columns afterward (via _refresh_table_and_cursors()) or every signal's
cursor readout — not just the one being toggled — goes blank until the next
cursor drag.

Bug reproduced: in multi-axis mode, unchecking a signal's Axis checkbox
(_toggle_axis_visibility) wiped Cursor 1/2 values for ALL rows. Toggling a
signal's visibility checkbox (_apply_visibility) never had this problem,
which is why it serves as the reference/regression-guard case here.

Requires a Qt platform plugin; skipped (not failed) if none is available.
"""
from __future__ import annotations

import numpy as np
import pytest

from PySide6.QtCore import Qt


def _make_series(name: str, unit: str):
    from core.signal_store import SignalSeries
    ts = np.linspace(0.0, 10.0, 500)
    vs = np.sin(ts) + hash(name) % 5
    return SignalSeries(
        channel=1, message_name="Msg", message_id=0x100,
        signal_name=name, unit=unit,
        timestamps=ts, values=vs, raw_values=[], has_labels=False,
    )


def _setup_multi_axis_with_cursors(panel, qapp, keys_units):
    """Plot signals with distinct units, enable multi-axis + both cursors."""
    for key, unit in keys_units:
        panel.add_series(key, _make_series(key, unit))
    qapp.processEvents()
    panel.set_multi_axis(True)
    qapp.processEvents()
    panel.set_cursor2_enabled(True)
    qapp.processEvents()
    panel.v_line.setPos(3.0)
    panel.v_line2.setPos(6.0)
    qapp.processEvents()


def _cursor_cell_text(panel, key: str, col: int) -> str | None:
    row = panel._row_lookup.get(key)
    if row is None:
        return None
    item = panel.table.item(row, col)
    return item.text() if item else None


def _assert_all_cursor_values_populated(panel, keys):
    for key in keys:
        c1 = _cursor_cell_text(panel, key, 2)
        c2 = _cursor_cell_text(panel, key, 3)
        assert c1, f"Cursor 1 value for {key!r} was blanked: {c1!r}"
        assert c2, f"Cursor 2 value for {key!r} was blanked: {c2!r}"


_SIGNALS = [("Speed", "km/h"), ("Temp", "C"), ("Pressure", "bar")]


# ---------------------------------------------------------------------------
# The reported bug: Axis checkbox toggle
# ---------------------------------------------------------------------------

def test_toggle_axis_visibility_preserves_other_cursor_values(panel, qapp):
    _setup_multi_axis_with_cursors(panel, qapp, _SIGNALS)
    _assert_all_cursor_values_populated(panel, ["Speed", "Temp", "Pressure"])

    # Uncheck the Axis checkbox for just one signal.
    panel._toggle_axis_visibility("Speed", False)
    qapp.processEvents()

    # All signals (including the toggled one, which is still visible) must
    # keep their cursor readouts — only the axis display changed.
    _assert_all_cursor_values_populated(panel, ["Speed", "Temp", "Pressure"])

    # Re-check it too, for good measure.
    panel._toggle_axis_visibility("Speed", True)
    qapp.processEvents()
    _assert_all_cursor_values_populated(panel, ["Speed", "Temp", "Pressure"])


# ---------------------------------------------------------------------------
# Same bug class: other _refresh_table() call sites
# ---------------------------------------------------------------------------

def test_set_series_color_preserves_cursor_values(panel, qapp):
    _setup_multi_axis_with_cursors(panel, qapp, _SIGNALS)
    panel.set_series_color("Temp", "#ff00ff")
    qapp.processEvents()
    _assert_all_cursor_values_populated(panel, ["Speed", "Temp", "Pressure"])


def test_toggle_name_channel_preserves_cursor_values(panel, qapp):
    _setup_multi_axis_with_cursors(panel, qapp, _SIGNALS)
    panel._toggle_name_channel(True)
    qapp.processEvents()
    _assert_all_cursor_values_populated(panel, ["Speed", "Temp", "Pressure"])


def test_toggle_name_message_preserves_cursor_values(panel, qapp):
    _setup_multi_axis_with_cursors(panel, qapp, _SIGNALS)
    panel._toggle_name_message(True)
    qapp.processEvents()
    _assert_all_cursor_values_populated(panel, ["Speed", "Temp", "Pressure"])


# ---------------------------------------------------------------------------
# Regression guards: paths that already worked correctly before the fix
# ---------------------------------------------------------------------------

def test_visibility_toggle_still_preserves_cursor_values(panel, qapp):
    _setup_multi_axis_with_cursors(panel, qapp, _SIGNALS)

    panel._items["Speed"].visible = False
    panel._apply_visibility()
    qapp.processEvents()
    # The two still-visible signals must keep their readouts.
    _assert_all_cursor_values_populated(panel, ["Temp", "Pressure"])

    panel._items["Speed"].visible = True
    panel._apply_visibility()
    qapp.processEvents()
    _assert_all_cursor_values_populated(panel, ["Speed", "Temp", "Pressure"])


def test_selected_signal_checkboxes_toggle_together(panel, qapp):
    keys = [name for name, _unit in _SIGNALS]
    for key, unit in _SIGNALS:
        panel.add_series(key, _make_series(key, unit))
    qapp.processEvents()
    panel._restore_selection(keys)
    qapp.processEvents()

    speed_row = panel._row_lookup["Speed"]
    panel.table.item(speed_row, 0).setCheckState(Qt.CheckState.Unchecked)
    qapp.processEvents()

    assert all(not panel._items[key].visible for key in keys)
    assert set(panel.selected_keys()) == set(keys)

    speed_row = panel._row_lookup["Speed"]
    panel.table.item(speed_row, 0).setCheckState(Qt.CheckState.Checked)
    qapp.processEvents()

    assert all(panel._items[key].visible for key in keys)
    assert set(panel.selected_keys()) == set(keys)


def test_selected_axis_checkboxes_toggle_together(panel, qapp):
    keys = [name for name, _unit in _SIGNALS]
    _setup_multi_axis_with_cursors(panel, qapp, _SIGNALS)
    panel._restore_selection(keys)
    qapp.processEvents()

    speed_row = panel._row_lookup["Speed"]
    panel.table.item(speed_row, 6).setCheckState(Qt.CheckState.Unchecked)
    qapp.processEvents()

    assert all(not panel._items[key].axis_visible for key in keys)
    assert set(panel.selected_keys()) == set(keys)

    speed_row = panel._row_lookup["Speed"]
    panel.table.item(speed_row, 6).setCheckState(Qt.CheckState.Checked)
    qapp.processEvents()

    assert all(panel._items[key].axis_visible for key in keys)
    assert set(panel.selected_keys()) == set(keys)


def test_group_rename_still_preserves_cursor_values(panel, qapp):
    _setup_multi_axis_with_cursors(panel, qapp, _SIGNALS)

    panel._items["Speed"].group = "Powertrain"
    panel._items["Temp"].group = "Powertrain"
    panel._rebuild_curves(preserve_selection=True)
    qapp.processEvents()
    _assert_all_cursor_values_populated(panel, ["Speed", "Temp", "Pressure"])

    # Simulate the rename itself (what _on_table_item_double_clicked does
    # after QInputDialog accepts a new name).
    for p in panel._items.values():
        if p.group == "Powertrain":
            p.group = "Drivetrain"
    panel._rebuild_curves(preserve_selection=True)
    qapp.processEvents()
    _assert_all_cursor_values_populated(panel, ["Speed", "Temp", "Pressure"])
