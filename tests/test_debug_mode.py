from __future__ import annotations

import threading
import time
from contextlib import contextmanager

import pytest

from PySide6.QtCore import qInstallMessageHandler
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog

from gui.main_window import MainWindow

THREAD_DESTROYED_WARNING = "Destroyed while thread is still running"


def _wait_for_debug_worker(window: MainWindow, qapp: QApplication) -> None:
    # The worker thread is now session-long, so idleness is the busy flag
    # plus an empty queue rather than the absence of a thread.
    for _ in range(500):
        qapp.processEvents()
        if (
            not window._debug_busy
            and not window._debug_pending_inspections
        ):
            return
        QTest.qWait(10)
    raise AssertionError("debug inspector did not finish")


@contextmanager
def _captured_qt_messages():
    """Collect Qt's own warnings, which never surface as Python exceptions."""
    messages: list[str] = []

    def handler(_mode, _context, message):
        messages.append(message)

    previous = qInstallMessageHandler(handler)
    try:
        yield messages
    finally:
        qInstallMessageHandler(previous)


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


def test_back_to_back_inspections_reuse_the_same_worker_thread(
    qapp, monkeypatch, tmp_path, sample_dbc_path
):
    measurement = tmp_path / "issue.csv"
    measurement.write_text("time,value\n0,1\n", encoding="utf-8")
    monkeypatch.setattr("gui.main_window.dbc_required_for", lambda _path: False)

    from core.channel_config import ChannelConfig

    window = MainWindow("CANScope", "test")
    window.toggle_debug_mode()

    with _captured_qt_messages() as messages:
        window._queue_measurement_debug_inspection(str(measurement))
        _wait_for_debug_worker(window, qapp)
        first_thread = window._debug_thread
        first_worker = window._debug_worker
        assert first_thread is not None

        window.channel_config = ChannelConfig.from_single_dbc(
            str(sample_dbc_path)
        )
        window._queue_database_debug_inspection()
        _wait_for_debug_worker(window, qapp)

        # Same objects: the thread outlives the individual inspections.
        assert window._debug_thread is first_thread
        assert window._debug_worker is first_worker

    assert not [m for m in messages if THREAD_DESTROYED_WARNING in m]

    window.toggle_debug_mode()
    assert window._debug_thread is None
    assert window._debug_worker is None
    window.close()


def test_closing_the_window_mid_inspection_tears_down_cleanly(
    qapp, monkeypatch, tmp_path
):
    measurement = tmp_path / "issue.csv"
    measurement.write_text("time,value\n0,1\n", encoding="utf-8")
    inspection_started = threading.Event()

    def slow_inspect(path, app_version=""):
        inspection_started.set()
        time.sleep(0.3)
        return "CANScope LOAD DEBUG\nslow inspection"

    monkeypatch.setattr("gui.main_window.inspect_measurement", slow_inspect)

    window = MainWindow("CANScope", "test")
    window.toggle_debug_mode()

    with _captured_qt_messages() as messages:
        window._queue_measurement_debug_inspection(str(measurement))
        assert inspection_started.wait(5.0), "inspection never reached the worker"
        # Close while the inspector is still running inside the worker thread.
        window.close()
        qapp.processEvents()

    assert window._debug_thread is None
    assert window._debug_worker is None
    assert window._debug_busy is False
    assert not [m for m in messages if THREAD_DESTROYED_WARNING in m]
