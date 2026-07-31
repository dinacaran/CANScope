from __future__ import annotations

import threading
import time
from contextlib import contextmanager

import pytest

from PySide6.QtCore import qInstallMessageHandler
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

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


# ── Auto-launch on failure ─────────────────────────────────────────────────


def _prepared_window(monkeypatch, measurement) -> MainWindow:
    """A window with a measurement selected and the failure modal stubbed."""
    monkeypatch.setattr(
        QMessageBox, "critical", lambda *args, **kwargs: None
    )
    window = MainWindow("CANScope", "test")
    window.measurement_path = str(measurement)
    return window


def test_load_failure_auto_opens_the_debug_window_with_debug_mode_off(
    qapp, monkeypatch, tmp_path
):
    measurement = tmp_path / "issue.csv"
    measurement.write_text("time,value\n0,1\n", encoding="utf-8")
    window = _prepared_window(monkeypatch, measurement)
    assert window._debug_mode is False
    assert window._debug_window is None

    window._on_worker_failed("MDF wrong signal data block refence")
    _wait_for_debug_worker(window, qapp)

    assert window._debug_mode is True
    assert window._debug_auto_launched is True
    assert window._debug_window is not None

    report = window._debug_window.report.toPlainText()
    # Both the failure banner and the measurement inspection are present.
    assert "LOAD + DECODE RESULT: FAIL" in report
    assert "Load + Decode failed" in report
    assert "File: issue.csv" in report
    assert not list(tmp_path.glob("*.json"))
    assert not list(tmp_path.glob("debug*.txt"))

    window.toggle_debug_mode()
    assert window._debug_auto_launched is False
    window.close()


def test_load_failure_with_debug_mode_already_on_does_not_restart_the_report(
    qapp, monkeypatch, tmp_path
):
    measurement = tmp_path / "issue.csv"
    measurement.write_text("time,value\n0,1\n", encoding="utf-8")
    window = _prepared_window(monkeypatch, measurement)
    window.toggle_debug_mode()
    window._queue_measurement_debug_inspection(str(measurement))
    _wait_for_debug_worker(window, qapp)

    existing_window = window._debug_window
    generation_before = window._debug_generation

    window._on_worker_failed("decode blew up")
    _wait_for_debug_worker(window, qapp)

    # Same window, same report generation: the banner is appended, the
    # existing inspection is not thrown away and re-run.
    assert window._debug_window is existing_window
    assert window._debug_generation == generation_before
    assert window._debug_auto_launched is False
    report = window._debug_window.report.toPlainText()
    assert "File: issue.csv" in report
    assert "LOAD + DECODE RESULT: FAIL" in report

    window.toggle_debug_mode()
    window.close()


def test_canscope_auto_debug_zero_suppresses_auto_launch(
    qapp, monkeypatch, tmp_path
):
    measurement = tmp_path / "issue.csv"
    measurement.write_text("time,value\n0,1\n", encoding="utf-8")
    monkeypatch.setenv("CANSCOPE_AUTO_DEBUG", "0")
    window = _prepared_window(monkeypatch, measurement)

    window._on_worker_failed("decode blew up")
    qapp.processEvents()

    assert window._debug_mode is False
    assert window._debug_window is None
    assert window._debug_thread is None
    assert not list(tmp_path.glob("*.txt"))
    assert not list(tmp_path.glob("*.json"))
    window.close()


def test_a_failing_inspector_does_not_re_enter_the_auto_launcher(
    qapp, monkeypatch, tmp_path
):
    measurement = tmp_path / "issue.csv"
    measurement.write_text("time,value\n0,1\n", encoding="utf-8")

    def exploding_inspect(path, app_version=""):
        raise RuntimeError("inspector exploded")

    monkeypatch.setattr(
        "gui.main_window.inspect_measurement", exploding_inspect
    )
    window = _prepared_window(monkeypatch, measurement)

    launches = []
    original = window._auto_launch_debug

    def counting_auto_launch(reason, detail):
        launches.append(reason)
        return original(reason, detail)

    monkeypatch.setattr(window, "_auto_launch_debug", counting_auto_launch)

    window._on_worker_failed("decode blew up")
    _wait_for_debug_worker(window, qapp)

    assert launches == ["Load + Decode failed"]
    report = window._debug_window.report.toPlainText()
    assert "DEBUG INSPECTOR INTERNAL FAILURE" in report
    assert "inspector exploded" in report

    window.toggle_debug_mode()
    window.close()


def test_auto_launch_is_not_reentrant(qapp, monkeypatch, tmp_path):
    measurement = tmp_path / "issue.csv"
    measurement.write_text("time,value\n0,1\n", encoding="utf-8")
    window = _prepared_window(monkeypatch, measurement)

    calls = []
    original_append = window._append_debug_runtime

    def reentering_append(message):
        calls.append(message)
        # Stand in for anything on the banner path that itself fails and
        # tries to launch the report again.
        window._auto_launch_debug("recursive", "should be ignored")
        original_append(message)

    monkeypatch.setattr(window, "_append_debug_runtime", reentering_append)

    window._auto_launch_debug("first", "detail")
    _wait_for_debug_worker(window, qapp)

    assert len(calls) == 1
    assert "should be ignored" not in "".join(calls)

    window.toggle_debug_mode()
    window.close()


def test_dbc_manager_records_databases_it_cannot_read(qapp, tmp_path):
    broken_dbc = tmp_path / "broken.dbc"
    broken_dbc.write_text("this is not a database at all\n", encoding="utf-8")

    from core.channel_config import ChannelConfig
    from gui.dbc_manager import DBCManagerDialog

    # Building the dialog computes match quality for every assigned row,
    # which is where the unreadable database is noticed.
    dlg = DBCManagerDialog(
        channel_config=ChannelConfig.from_single_dbc(str(broken_dbc)),
        channels_in_file=[1],
        ids_per_channel={1: {0x100}},
    )
    assert str(broken_dbc) in dlg.load_errors()
    dlg.deleteLater()
    qapp.processEvents()


def test_unreadable_database_auto_launches_the_debug_report(
    qapp, monkeypatch, tmp_path
):
    measurement = tmp_path / "issue.csv"
    measurement.write_text("time,value\n0,1\n", encoding="utf-8")
    broken_dbc = tmp_path / "broken.dbc"
    broken_dbc.write_text("this is not a database at all\n", encoding="utf-8")

    from core.channel_config import ChannelConfig
    from gui.dbc_manager import DBCManagerDialog

    class _AcceptingDialog:
        DialogCode = DBCManagerDialog.DialogCode

        def __init__(self, **_kwargs):
            pass

        def exec(self):
            return DBCManagerDialog.DialogCode.Accepted

        def result_config(self):
            return ChannelConfig.from_single_dbc(str(broken_dbc))

        def load_errors(self):
            return {str(broken_dbc): "Failed to load database file"}

    monkeypatch.setattr("gui.main_window.DBCManagerDialog", _AcceptingDialog)
    window = _prepared_window(monkeypatch, measurement)
    assert window._debug_mode is False

    window.choose_dbc()
    _wait_for_debug_worker(window, qapp)

    assert window._debug_mode is True
    assert window._debug_auto_launched is True
    report = window._debug_window.report.toPlainText()
    assert "broken.dbc" in report
    assert "Database failed to load" in report

    window.toggle_debug_mode()
    window.close()
