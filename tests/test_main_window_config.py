"""
Tests for gui/main_window.py save_configuration / load_configuration.

Covers the measurement_path/blf_path generalisation: BLF+DBC, database-less
formats (MF4/CSV), legacy config files (blf_path + dbc_path only), and a
config pointing at a since-deleted measurement file.

These tests require a Qt platform plugin. On headless CI set
QT_QPA_PLATFORM=offscreen before running. If no display is found the
tests are skipped rather than erroring out.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture()
def window(qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from gui.main_window import MainWindow

    # Modal dialogs would block the (headless, non-interactive) event loop —
    # replace them with Mocks so tests can assert on how they were called.
    monkeypatch.setattr(QMessageBox, "warning", Mock(return_value=QMessageBox.StandardButton.Ok))
    monkeypatch.setattr(QMessageBox, "critical", Mock(return_value=QMessageBox.StandardButton.Ok))
    monkeypatch.setattr(QMessageBox, "information", Mock(return_value=QMessageBox.StandardButton.Ok))

    w = MainWindow('CANScope', '00.00.99')
    # Never let a test spin up a real background decode thread.
    monkeypatch.setattr(w, 'load_data', Mock())
    yield w
    w.close()
    qapp.processEvents()


def _mock_save_dialog(monkeypatch, out_path: Path) -> None:
    from PySide6.QtWidgets import QFileDialog
    monkeypatch.setattr(QFileDialog, 'getSaveFileName',
                         lambda *a, **k: (str(out_path), 'JSON Files (*.json)'))


def _mock_open_dialog(monkeypatch, in_path: Path) -> None:
    from PySide6.QtWidgets import QFileDialog
    monkeypatch.setattr(QFileDialog, 'getOpenFileName',
                         lambda *a, **k: (str(in_path), 'JSON Files (*.json)'))


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def test_save_writes_measurement_path_and_legacy_blf_path(window, monkeypatch, tmp_path, sample_dbc_path):
    window.measurement_path = str(tmp_path / "sample.blf")
    window.blf_path = window.measurement_path
    window.dbc_path = str(sample_dbc_path)

    out = tmp_path / "config.json"
    _mock_save_dialog(monkeypatch, out)
    window.save_configuration()

    data = json.loads(out.read_text())
    assert data['measurement_path'] == window.measurement_path
    assert data['blf_path'] == window.measurement_path
    assert data['dbc_path'] == str(sample_dbc_path)


def test_save_preserves_channel_config(window, monkeypatch, tmp_path, sample_dbc_path):
    from core.channel_config import ChannelConfig
    window.measurement_path = str(tmp_path / "sample.mf4")
    window.channel_config = ChannelConfig(name="Truck", channels={1: str(sample_dbc_path)})

    out = tmp_path / "config.json"
    _mock_save_dialog(monkeypatch, out)
    window.save_configuration()

    data = json.loads(out.read_text())
    assert data['channel_config']['name'] == 'Truck'
    assert data['channel_config']['channels']['1'] == str(sample_dbc_path)


# ---------------------------------------------------------------------------
# Load — (a) BLF + DBC
# ---------------------------------------------------------------------------

def test_load_blf_with_dbc_calls_load_data(window, monkeypatch, tmp_path, sample_dbc_path):
    blf = tmp_path / "sample.blf"
    blf.write_bytes(b"")  # existence is all that's checked before load_data runs
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        'measurement_path': str(blf),
        'blf_path': str(blf),
        'dbc_path': str(sample_dbc_path),
        'channel_config': {'name': 'X', 'channels': {'0': str(sample_dbc_path)}},
        'signals': [],
    }))

    _mock_open_dialog(monkeypatch, cfg)
    window.load_configuration()

    window.load_data.assert_called_once()
    from PySide6.QtWidgets import QMessageBox
    QMessageBox.warning.assert_not_called()
    assert window.measurement_path == str(blf)


# ---------------------------------------------------------------------------
# Load — (b) MF4 without a database
# ---------------------------------------------------------------------------

def test_load_mf4_without_database_proceeds(window, monkeypatch, tmp_path):
    mf4 = tmp_path / "sample.mf4"
    mf4.write_bytes(b"not a real mdf file")  # is_bus_logging() swallows parse errors -> False
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        'measurement_path': str(mf4),
        'signals': [],
    }))

    _mock_open_dialog(monkeypatch, cfg)
    window.load_configuration()

    window.load_data.assert_called_once()
    from PySide6.QtWidgets import QMessageBox
    QMessageBox.warning.assert_not_called()


def test_decoded_mdf_keeps_can_trace_action_discoverable(window):
    from PySide6.QtWidgets import QMessageBox

    reason = "Decoded MDF has no raw CAN_DataFrame records."
    window.store = SimpleNamespace(
        raw_frame_store=None,
        raw_trace_unavailable_reason=reason,
    )

    window._update_action_states()
    assert window._act_can_trace.isEnabled() is True

    window.show_raw_frames()
    QMessageBox.information.assert_called_once_with(
        window,
        'CAN Trace unavailable',
        reason,
    )


def test_mixed_mdf_notice_explains_decoded_first_loading(window, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QMessageBox

    path = str(tmp_path / "mixed.mf4")
    monkeypatch.setattr(
        "gui.main_window.has_mixed_mdf_content",
        lambda _path: True,
    )

    window._show_mixed_mdf_notice(path)
    window._show_mixed_mdf_notice(path)

    QMessageBox.information.assert_called_once()
    title, message = QMessageBox.information.call_args[0][1:]
    assert title == "Mixed MDF content detected"
    assert "load and plot the decoded signals" in message
    assert "assign a DBC or ARXML" in message


def test_load_mf4_bus_logging_without_database_warns(window, monkeypatch, tmp_path):
    """A bus-logging MF4 (raw CAN frames) still needs a database, same as BLF."""
    mf4 = tmp_path / "buslog.mf4"
    mf4.write_bytes(b"")
    monkeypatch.setattr('gui.main_window.dbc_required_for', lambda path: True)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        'measurement_path': str(mf4),
        'signals': [],
    }))

    _mock_open_dialog(monkeypatch, cfg)
    window.load_configuration()

    window.load_data.assert_not_called()
    from PySide6.QtWidgets import QMessageBox
    QMessageBox.warning.assert_called_once()
    assert 'database' in QMessageBox.warning.call_args[0][2].lower()


# ---------------------------------------------------------------------------
# Load — (c) legacy config (blf_path + dbc_path only, no measurement_path key)
# ---------------------------------------------------------------------------

def test_load_legacy_config_without_measurement_path_key(window, monkeypatch, tmp_path, sample_dbc_path):
    blf = tmp_path / "legacy.blf"
    blf.write_bytes(b"")
    cfg = tmp_path / "legacy_config.json"
    # Old-format file: no 'measurement_path' key, no 'channel_config' key.
    cfg.write_text(json.dumps({
        'version': '00.00.01',
        'blf_path': str(blf),
        'dbc_path': str(sample_dbc_path),
        'signals': ['Engine.EngSpeed'],
    }))

    _mock_open_dialog(monkeypatch, cfg)
    window.load_configuration()

    window.load_data.assert_called_once()
    from PySide6.QtWidgets import QMessageBox
    QMessageBox.warning.assert_not_called()
    assert window.measurement_path == str(blf)
    assert not window.channel_config.is_empty()  # populated via from_single_dbc


# ---------------------------------------------------------------------------
# Load — (d) config pointing to a deleted file
# ---------------------------------------------------------------------------

def test_load_missing_measurement_file_warns(window, monkeypatch, tmp_path):
    missing = tmp_path / "gone.blf"  # never created
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        'measurement_path': str(missing),
        'dbc_path': None,
        'signals': [],
    }))

    _mock_open_dialog(monkeypatch, cfg)
    window.load_configuration()

    window.load_data.assert_not_called()
    from PySide6.QtWidgets import QMessageBox
    QMessageBox.warning.assert_called_once()
    title, message = QMessageBox.warning.call_args[0][1], QMessageBox.warning.call_args[0][2]
    assert title == 'Measurement file not found'
    assert str(missing) in message


def test_load_no_measurement_path_warns(window, monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({'signals': []}))

    _mock_open_dialog(monkeypatch, cfg)
    window.load_configuration()

    window.load_data.assert_not_called()
    from PySide6.QtWidgets import QMessageBox
    QMessageBox.warning.assert_called_once()
    assert 'no measurement file path' in QMessageBox.warning.call_args[0][2].lower()
