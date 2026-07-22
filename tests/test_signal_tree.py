from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from gui.signal_tree import SignalTreeWidget


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def signal_tree(qapp):
    widget = SignalTreeWidget()
    widget.set_payload(
        {
            1: {
                "EngineControl": ["EngSpeed", "Throttle"],
                "GearStatus": ["Gear"],
            },
            2: {
                "BatteryStatus": ["Current", "Voltage"],
            },
        }
    )
    yield widget
    widget.close()


def _channels(widget: SignalTreeWidget):
    return [
        widget.tree.topLevelItem(index)
        for index in range(widget.tree.topLevelItemCount())
    ]


def _messages(widget: SignalTreeWidget):
    return [
        channel.child(index)
        for channel in _channels(widget)
        for index in range(channel.childCount())
    ]


def test_message_controls_are_enabled_after_payload_load(signal_tree):
    assert signal_tree.collapse_messages_button.isEnabled()
    assert signal_tree.expand_messages_button.isEnabled()
    assert all(channel.isExpanded() for channel in _channels(signal_tree))
    assert all(message.isExpanded() for message in _messages(signal_tree))


def test_search_and_message_controls_use_separate_rows(signal_tree):
    layout = signal_tree.layout()

    assert layout.itemAt(0).widget() is signal_tree.search_edit
    assert layout.itemAt(1).widget() is signal_tree.tree_header
    assert layout.itemAt(2).layout().indexOf(signal_tree.collapse_messages_button) >= 0
    assert layout.itemAt(2).layout().indexOf(signal_tree.expand_messages_button) >= 0
    assert layout.itemAt(3).widget() is signal_tree.tree
    assert signal_tree.tree.isHeaderHidden()

    signal_tree.resize(500, 600)
    signal_tree.show()
    QApplication.processEvents()

    assert signal_tree.search_edit.width() == signal_tree.tree_header.width()
    assert signal_tree.search_edit.width() == signal_tree.tree.width()


def test_collapse_operates_in_place_and_keeps_channels_open(signal_tree):
    messages_before = _messages(signal_tree)

    signal_tree.collapse_messages_button.click()

    assert all(channel.isExpanded() for channel in _channels(signal_tree))
    assert _messages(signal_tree) == messages_before
    assert all(not message.isExpanded() for message in _messages(signal_tree))


def test_expand_restores_every_message_across_channels(signal_tree):
    signal_tree.collapse_messages()

    signal_tree.expand_messages_button.click()

    assert all(channel.isExpanded() for channel in _channels(signal_tree))
    assert all(message.isExpanded() for message in _messages(signal_tree))


def test_collapse_preserves_selected_signal(signal_tree):
    signal_item = _channels(signal_tree)[0].child(0).child(0)
    signal_item.setSelected(True)
    selected_keys = signal_tree.selected_signal_keys()

    signal_tree.collapse_messages()

    assert signal_item.isSelected()
    assert signal_tree.selected_signal_keys() == selected_keys


def test_search_temporarily_expands_then_restores_collapsed_mode(signal_tree):
    signal_tree.collapse_messages()

    signal_tree.search_edit.setText("Speed")

    assert [message.text(0) for message in _messages(signal_tree)] == ["EngineControl"]
    assert _messages(signal_tree)[0].isExpanded()

    signal_tree.search_edit.clear()

    assert len(_messages(signal_tree)) == 3
    assert all(not message.isExpanded() for message in _messages(signal_tree))


def test_controls_are_disabled_for_empty_payload(qapp):
    widget = SignalTreeWidget()
    try:
        assert not widget.collapse_messages_button.isEnabled()
        assert not widget.expand_messages_button.isEnabled()
        widget.set_payload({})
        assert not widget.collapse_messages_button.isEnabled()
        assert not widget.expand_messages_button.isEnabled()
    finally:
        widget.close()


def test_generated_signals_have_separate_top_level_group(signal_tree):
    signal_tree.set_generated_signals([
        (
            "CH?::Generate Signals::ScaledSpeed",
            "ScaledSpeed",
            "ScaledSpeed [rpm] = `CH1::EngineControl::EngSpeed` * 100",
        )
    ])

    generated = signal_tree.tree.topLevelItem(0)
    assert generated.text(0) == "Generate Signals"
    assert generated.isExpanded()
    assert generated.child(0).text(0) == "ScaledSpeed"
    assert signal_tree._item_key(generated.child(0)) == "CH?::Generate Signals::ScaledSpeed"
    assert generated.child(0).font(0).italic()

    signal_tree.collapse_messages()
    assert generated.isExpanded()
    assert generated.child(0).isHidden() is False


def test_generated_signals_participate_in_search(signal_tree):
    signal_tree.set_generated_signals([
        ("CH?::Generate Signals::ScaledSpeed", "ScaledSpeed", "formula")
    ])

    signal_tree.search_edit.setText("Scaled")
    assert signal_tree.tree.topLevelItem(0).text(0) == "Generate Signals"
    assert signal_tree.tree.topLevelItem(0).childCount() == 1

    signal_tree.search_edit.setText("NoSuchSignal")
    assert all(item.text(0) != "Generate Signals" for item in _channels(signal_tree))
