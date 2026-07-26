from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog

from gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _wait_for_debug_worker(window: MainWindow, qapp: QApplication) -> None:
    for _ in range(500):
        qapp.processEvents()
        if (
            window._debug_thread is None
            and not window._debug_pending_inspections
        ):
            return
        QTest.qWait(10)
    raise AssertionError("debug inspector did not finish")


def test_ctrl_alt_d_shortcut_toggles_debug_and_normal_mode(qapp):
    window = MainWindow("CANScope", "test")
    window.show()
    qapp.processEvents()

    shortcuts = [
        shortcut
        for shortcut in window.findChildren(QShortcut)
        if shortcut.key().toString(QKeySequence.SequenceFormat.PortableText)
        == "Ctrl+Alt+D"
    ]
    assert len(shortcuts) == 1

    shortcuts[0].activated.emit()
    qapp.processEvents()
    assert window._debug_mode is True
    assert window.debug_mode_label.isVisible()
    assert window._debug_window is not None
    assert window._debug_window.isVisible()

    shortcuts[0].activated.emit()
    qapp.processEvents()
    assert window._debug_mode is False
    assert window.debug_mode_label.isHidden()
    assert not window._debug_window.isVisible()

    window.close()


def test_selecting_measurement_in_debug_mode_starts_plain_text_inspection(
    qapp, monkeypatch, tmp_path, sample_dbc_path
):
    measurement = tmp_path / "issue.csv"
    measurement.write_text("time,value\n0,1\n", encoding="utf-8")
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: (str(measurement), ""),
    )
    monkeypatch.setattr("gui.main_window.dbc_required_for", lambda _path: False)

    window = MainWindow("CANScope", "test")
    window.show()
    window.toggle_debug_mode()
    window.choose_blf()
    _wait_for_debug_worker(window, qapp)

    report = window._debug_window.report.toPlainText()
    assert "CANScope LOAD DEBUG" in report
    assert "File: issue.csv" in report
    assert str(tmp_path) not in report
    assert not (tmp_path / "debug.json").exists()
    assert not (tmp_path / "debug.txt").exists()

    from core.channel_config import ChannelConfig

    window.channel_config = ChannelConfig.from_single_dbc(
        str(sample_dbc_path)
    )
    window._prescan_cache = (
        str(measurement),
        [1],
        {1: {0x100, 0x999}},
    )
    window._queue_database_debug_inspection()
    _wait_for_debug_worker(window, qapp)

    combined = window._debug_window.report.toPlainText()
    assert "DATABASE: sample.dbc" in combined
    assert "SCREENSHOT SUMMARY - MEASUREMENT + DATABASE" in combined
    assert "Observed IDs: 2 | Matched: 1 | Unmatched: 1" in combined

    window.toggle_debug_mode()
    window.close()
