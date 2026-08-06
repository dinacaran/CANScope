from __future__ import annotations

import array
import json

import numpy as np
import pytest

from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from core.signal_store import SignalSeries, SignalStore
from gui.main_window import MainWindow


def _series(signal_name: str = "OldSignal") -> SignalSeries:
    return SignalSeries(
        channel=1,
        message_name="OldMessage",
        message_id=1,
        signal_name=signal_name,
        unit="V",
        timestamps=array.array("d", [0.0, 1.0]),
        values=array.array("d", [1.0, 2.0]),
    )


def _store(*signal_names: str) -> SignalStore:
    store = SignalStore()
    for signal_name in signal_names:
        store.add_series_bulk(
            channel=1,
            message_name="OldMessage",
            message_id=1,
            signal_name=signal_name,
            unit="V",
            timestamps=np.array([0.0, 1.0], dtype=np.float64),
            values=np.array([10.0, 20.0], dtype=np.float64),
            raw_values=[],
            has_labels=False,
        )
    return store


class _FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)


class _FakeThread:
    def __init__(self, *_args, **_kwargs):
        self.started = _FakeSignal()
        self.finished = _FakeSignal()
        self.was_started = False

    def start(self):
        self.was_started = True

    def quit(self):
        pass

    def deleteLater(self):
        pass


class _FakeWorker:
    def __init__(self, *_args, **_kwargs):
        self.progress = _FakeSignal()
        self.finished = _FakeSignal()
        self.failed = _FakeSignal()
        self.tree_update = _FakeSignal()
        self.partial_ready = _FakeSignal()

    def moveToThread(self, _thread):
        pass

    def run(self):
        pass

    def deleteLater(self):
        pass


def test_opening_new_measurement_clears_stale_state_and_uses_plot_message(
    qapp, monkeypatch, tmp_path
):
    new_measurement = tmp_path / "new.csv"
    new_measurement.write_text(
        "time,signal,value\n0.0,NewSignal,1.0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: (str(new_measurement), ""),
    )
    monkeypatch.setattr("gui.main_window.dbc_required_for", lambda _path: False)

    window = MainWindow("CANScope", "00.00.99")
    temporary_config = tmp_path / "canscope_temp_plot_config.json"
    window._temporary_plot_config_path = temporary_config
    old_series = _series()
    window.store = object()
    window.signal_tree.set_payload(
        {1: {"OldMessage": ["OldSignal"]}}
    )
    window.plot_panel.add_series(old_series.key, old_series)

    window.choose_blf()

    saved = json.loads(temporary_config.read_text(encoding="utf-8"))
    assert saved == {
        "type": "canscope_temporary_plot_config",
        "version": 1,
        "plot_type": "stacked",
        "signals": [
            {
                "key": old_series.key,
                "color": window._temporary_plot_handoff["signals"][0]["color"],
                "visible": True,
                "group": "",
                "axis_visible": True,
                "own_axis": False,
                "multistack_id": -1,
            }
        ],
    }
    assert "measurement_path" not in saved
    assert "channel_config" not in saved
    assert "generated_signals" not in saved
    assert window.store is None
    assert window.plot_panel.plotted_keys() == []
    assert window.signal_tree.tree.topLevelItemCount() == 0
    assert window.plot_panel.table.rowCount() == 0
    assert not hasattr(window.plot_panel, "table_alert_label")
    assert "Measurement file selected" in window.plot_panel.overlay_label.text()
    assert "Load + Decode" in window.plot_panel.overlay_label.text()

    window.close()
    qapp.processEvents()


def test_load_decode_restores_plot_handoff_and_skips_missing_signals(
    qapp, monkeypatch, tmp_path
):
    new_measurement = tmp_path / "new.csv"
    new_measurement.write_text(
        "time,signal,value\n0.0,OldSignal,1.0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: (str(new_measurement), ""),
    )
    monkeypatch.setattr("gui.main_window.dbc_required_for", lambda _path: False)
    monkeypatch.setattr("gui.main_window.QThread", _FakeThread)
    monkeypatch.setattr("gui.main_window.LoadWorker", _FakeWorker)

    window = MainWindow("CANScope", "00.00.99")
    window._temporary_plot_config_path = (
        tmp_path / "canscope_temp_plot_config.json"
    )
    messages = []
    window._log = messages.append

    kept = _series("OldSignal")
    missing = _series("MissingSignal")
    window.plot_panel.add_series(kept.key, kept)
    window.plot_panel.add_series(missing.key, missing)
    window.plot_panel.set_series_color(kept.key, "#123456")
    kept_plot = window.plot_panel._items[kept.key]
    kept_plot.visible = False
    kept_plot.group = "Powertrain"
    kept_plot.axis_visible = False
    kept_plot.own_axis = True
    window.btn_stacked.setChecked(False)
    window.btn_multi_axis.setChecked(True)

    window.choose_blf()
    window._toolbar_actions["Load + Decode"].trigger()
    assert window._thread.was_started
    window._on_worker_finished(_store("OldSignal"))

    assert window.plot_panel.plotted_keys() == [kept.key]
    restored = window.plot_panel._items[kept.key]
    assert restored.color == "#123456"
    assert restored.visible is False
    assert restored.group == "Powertrain"
    assert restored.axis_visible is False
    assert restored.own_axis is True
    assert window.btn_multi_axis.isChecked()
    assert not window.btn_stacked.isChecked()
    assert window._temporary_plot_handoff is None
    assert any(f"Signal not found: {missing.key}" in message for message in messages)

    window._cleanup_worker()
    window.close()
    qapp.processEvents()


def test_cancelled_open_keeps_current_plot_and_creates_no_handoff(
    qapp, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: ("", ""),
    )
    window = MainWindow("CANScope", "00.00.99")
    temporary_config = tmp_path / "canscope_temp_plot_config.json"
    window._temporary_plot_config_path = temporary_config
    series = _series()
    window.plot_panel.add_series(series.key, series)

    window.choose_blf()

    assert window.plot_panel.plotted_keys() == [series.key]
    assert window._temporary_plot_handoff is None
    assert not temporary_config.exists()

    window.close()
    qapp.processEvents()


def test_open_without_plotted_signals_creates_no_handoff(
    qapp, monkeypatch, tmp_path
):
    new_measurement = tmp_path / "new.csv"
    new_measurement.write_text(
        "time,signal,value\n0.0,NewSignal,1.0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: (str(new_measurement), ""),
    )
    monkeypatch.setattr("gui.main_window.dbc_required_for", lambda _path: False)
    window = MainWindow("CANScope", "00.00.99")
    temporary_config = tmp_path / "canscope_temp_plot_config.json"
    window._temporary_plot_config_path = temporary_config

    window.choose_blf()

    assert window.measurement_path == str(new_measurement)
    assert window._temporary_plot_handoff is None
    assert not temporary_config.exists()

    window.close()
    qapp.processEvents()


def test_manual_configuration_takes_precedence_over_temporary_handoff(
    qapp, monkeypatch, tmp_path
):
    config_path = tmp_path / "manual.json"
    config_path.write_text(
        json.dumps({
            "signals": [],
            "stacked": False,
            "multi_axis": False,
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: (str(config_path), ""),
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )

    window = MainWindow("CANScope", "00.00.99")
    window.store = _store("OldSignal")
    window._temporary_plot_handoff = {
        "plot_type": "stacked",
        "signals": [{"key": _series().key}],
    }

    window.load_configuration()

    assert window._temporary_plot_handoff is None
    assert not window.btn_stacked.isChecked()
    assert not window.btn_multi_axis.isChecked()
    assert window.plot_panel.plotted_keys() == []

    window.close()
    qapp.processEvents()
