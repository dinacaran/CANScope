from __future__ import annotations

import numpy as np
import pytest


def _series(name: str, unit: str):
    from core.signal_store import SignalSeries

    timestamps = np.linspace(0.0, 1.0, 20)
    return SignalSeries(
        channel=1,
        message_name="Message",
        message_id=0x100,
        signal_name=name,
        unit=unit,
        timestamps=timestamps,
        values=np.sin(timestamps * 4.0),
        raw_values=[],
        has_labels=False,
    )


def test_multistack_starts_with_one_signal_per_stack(panel, qapp):
    panel.add_series("SpeedA", _series("SpeedA", "km/h"))
    panel.add_series("SpeedB", _series("SpeedB", "km/h"))

    panel.set_multistack(True)
    qapp.processEvents()

    assert panel._multistack_mode is True
    assert panel._stacked_mode is True
    assert len(panel._stacked_plots) == 2
    assert panel._stacked_row_keys == [["SpeedA"], ["SpeedB"]]


def test_multistack_move_overlays_same_unit_signals(panel, qapp):
    panel.add_series("SpeedA", _series("SpeedA", "km/h"))
    panel.add_series("SpeedB", _series("SpeedB", "KM/H"))
    panel.add_series("Temp", _series("Temp", "degC"))
    panel.set_multistack(True)
    target = panel._items["SpeedA"].multistack_id

    assert panel.move_signals_to_stack(["SpeedB"], target)
    qapp.processEvents()

    assert len(panel._stacked_plots) == 2
    assert panel._stacked_row_keys[0] == ["SpeedA", "SpeedB"]
    assert panel._items["SpeedA"].view_box is panel._items["SpeedB"].view_box
    assert panel._items["SpeedA"].curve is not panel._items["SpeedB"].curve


def test_multistack_different_unit_can_be_cancelled(panel, monkeypatch, qapp):
    panel.add_series("Speed", _series("Speed", "km/h"))
    panel.add_series("Temp", _series("Temp", "degC"))
    panel.set_multistack(True)
    target = panel._items["Speed"].multistack_id
    original = panel._items["Temp"].multistack_id

    prompted = []
    monkeypatch.setattr(
        panel,
        "confirm_multistack_units",
        lambda keys, stack, extra_units=None: prompted.append((keys, stack)) or False,
    )

    assert not panel.move_signals_to_stack(["Temp"], target)
    qapp.processEvents()

    assert prompted == [(["Temp"], target)]
    assert panel._items["Temp"].multistack_id == original
    assert len(panel._stacked_plots) == 2


def test_multistack_unit_comparison_includes_unplotted_drop(panel):
    panel.add_series("Speed", _series("Speed", "km/h"))
    panel.set_multistack(True)
    target = panel._items["Speed"].multistack_id

    assert not panel.multistack_units_mismatch([], target, [" KM/H "])
    assert panel.multistack_units_mismatch([], target, ["rpm"])


def test_switching_away_does_not_merge_regular_stacked_rows(panel, qapp):
    panel.add_series("SpeedA", _series("SpeedA", "km/h"))
    panel.add_series("SpeedB", _series("SpeedB", "km/h"))
    panel.set_multistack(True)
    target = panel._items["SpeedA"].multistack_id
    panel.move_signals_to_stack(["SpeedB"], target)
    assert len(panel._stacked_plots) == 1

    panel.set_multistack(False)
    panel.set_stacked(True)
    qapp.processEvents()

    assert panel._multistack_mode is False
    assert len(panel._stacked_plots) == 2
    assert panel._stacked_row_keys == [["SpeedA"], ["SpeedB"]]


def test_signal_tree_drop_is_routed_to_the_stack_under_pointer(panel, qapp):
    from PySide6.QtCore import QByteArray, QMimeData, QPointF
    from gui.signal_tree import SignalTreeWidget

    panel.add_series("Speed", _series("Speed", "km/h"))
    panel.set_multistack(True)
    qapp.processEvents()
    target = panel._items["Speed"].multistack_id

    mime = QMimeData()
    mime.setData(SignalTreeWidget.MIME_TYPE, QByteArray(b"NewSpeed"))
    local = panel.glw.mapFromScene(
        panel._stacked_plots[0].sceneBoundingRect().center()
    )

    class DropEvent:
        accepted = False

        def mimeData(self):
            return mime

        def position(self):
            return QPointF(local)

        def acceptProposedAction(self):
            self.accepted = True

        def ignore(self):
            self.accepted = False

    received = []
    panel.signalDroppedToStack.connect(
        lambda keys, stack: received.append((keys, stack))
    )
    event = DropEvent()

    panel._stacked_drop(event)

    assert event.accepted
    assert received == [(["NewSpeed"], target)]


def test_move_signal_to_new_stack_detaches_it_below_source(panel, qapp):
    panel.add_series("SpeedA", _series("SpeedA", "km/h"))
    panel.add_series("SpeedB", _series("SpeedB", "km/h"))
    panel.add_series("Temp", _series("Temp", "degC"))
    panel.set_multistack(True)
    panel.move_signals_to_stack(
        ["SpeedB"], panel._items["SpeedA"].multistack_id
    )

    assert panel.can_move_signals_to_new_stack(["SpeedB"])
    assert panel.move_signals_to_new_stack(["SpeedB"])
    qapp.processEvents()

    assert panel._stacked_row_keys == [["SpeedA"], ["SpeedB"], ["Temp"]]
    assert [panel._items[key].multistack_id for key in (
        "SpeedA", "SpeedB", "Temp"
    )] == [0, 1, 2]
    assert not panel.can_move_signals_to_new_stack(["SpeedB"])


def test_move_to_new_stack_preserves_existing_view_ranges(panel, qapp):
    panel.add_series("SpeedA", _series("SpeedA", "km/h"))
    panel.add_series("SpeedB", _series("SpeedB", "km/h"))
    panel.add_series("Temp", _series("Temp", "degC"))
    panel.set_multistack(True)
    panel.move_signals_to_stack(
        ["SpeedB"], panel._items["SpeedA"].multistack_id
    )
    qapp.processEvents()
    panel._stacked_plots[0].setXRange(0.2, 0.7, padding=0)
    panel._stacked_plots[0].setYRange(-4.0, 8.0, padding=0)
    panel._stacked_plots[1].setYRange(10.0, 30.0, padding=0)

    assert panel.move_signals_to_new_stack(["SpeedB"])
    qapp.processEvents()

    for plot in panel._stacked_plots:
        assert plot.vb.viewRange()[0] == pytest.approx([0.2, 0.7])
    assert panel._stacked_plots[0].vb.viewRange()[1] == pytest.approx([-4.0, 8.0])
    assert panel._stacked_plots[1].vb.viewRange()[1] == pytest.approx([-4.0, 8.0])
    assert panel._stacked_plots[2].vb.viewRange()[1] == pytest.approx([10.0, 30.0])


def test_new_stack_drop_zone_detaches_signal_at_boundary(panel, qapp):
    from PySide6.QtCore import QByteArray, QMimeData, QPointF
    from gui.plot_widget import _ReorderTable

    panel.add_series("SpeedA", _series("SpeedA", "km/h"))
    panel.add_series("SpeedB", _series("SpeedB", "km/h"))
    panel.add_series("Temp", _series("Temp", "degC"))
    panel.set_multistack(True)
    panel.move_signals_to_stack(
        ["SpeedB"], panel._items["SpeedA"].multistack_id
    )
    qapp.processEvents()

    first = panel._stacked_plots[0].sceneBoundingRect()
    second = panel._stacked_plots[1].sceneBoundingRect()
    scene_point = QPointF(
        first.center().x(), (first.bottom() + second.top()) / 2.0
    )
    local = panel.glw.mapFromScene(scene_point)
    mime = QMimeData()
    mime.setData(_ReorderTable._ROW_REORDER_MIME, QByteArray(b"SpeedB"))

    class DropEvent:
        accepted = False

        def mimeData(self):
            return mime

        def position(self):
            return QPointF(local)

        def acceptProposedAction(self):
            self.accepted = True

        def ignore(self):
            self.accepted = False

    event = DropEvent()
    panel._stacked_drag_move(event)
    assert event.accepted
    assert not panel._new_stack_drop_indicator.isHidden()
    assert panel._new_stack_drop_indicator.text() == "Create new stack"

    panel._stacked_drop(event)
    qapp.processEvents()

    assert event.accepted
    assert panel._new_stack_drop_indicator.isHidden()
    assert panel._stacked_row_keys == [["SpeedA"], ["SpeedB"], ["Temp"]]


def test_multistack_signal_menu_offers_move_to_new_stack(panel, qapp):
    from PySide6.QtWidgets import QMenu

    panel.add_series("SpeedA", _series("SpeedA", "km/h"))
    panel.add_series("SpeedB", _series("SpeedB", "km/h"))
    panel.set_multistack(True)
    panel.move_signals_to_stack(
        ["SpeedB"], panel._items["SpeedA"].multistack_id
    )
    qapp.processEvents()

    menu = QMenu(panel)
    action = panel._add_move_to_new_stack_action(menu, ["SpeedB"])
    assert action.text() == "Move to new stack"
    assert action.isEnabled()
    action.trigger()
    qapp.processEvents()
    assert panel._stacked_row_keys == [["SpeedA"], ["SpeedB"]]
