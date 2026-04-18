from __future__ import annotations

import fnmatch

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtGui import QAction, QDrag
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLineEdit,
    QMenu,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


class SignalTree(QTreeWidget):
    MIME_TYPE = "application/x-blfviewer-signal-key"

    def startDrag(self, supportedActions: Qt.DropActions) -> None:  # pragma: no cover - Qt hook
        items = self.selectedItems()
        keys = []
        for item in items:
            key = item.data(0, Qt.ItemDataRole.UserRole)
            if key:
                keys.append(str(key))
        if not keys:
            return
        mime = QMimeData()
        mime.setData(self.MIME_TYPE, "\n".join(keys).encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


class SignalTreeWidget(QWidget):
    signalActivated = Signal(list)
    MIME_TYPE = SignalTree.MIME_TYPE

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._payload: dict[int | None, dict[str, list[str]]] = {}

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search signals... (type to filter)")

        self.tree = SignalTree()
        self.tree.setColumnCount(1)
        self.tree.setHeaderLabels(["Channels / Messages / Signals"])
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.tree.setDragEnabled(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.search_edit)
        layout.addWidget(self.tree)

        self.search_edit.textChanged.connect(self.apply_filter)

    def set_payload(self, payload: dict[int | None, dict[str, list[str]]]) -> None:
        self._payload = payload
        self.rebuild_tree()

    def rebuild_tree(self) -> None:
        self.tree.clear()
        pattern = self.search_edit.text().strip()
        for channel, message_map in sorted(self._payload.items(), key=lambda x: (999999 if x[0] is None else x[0])):
            channel_label = f"CH{channel}" if channel is not None else "CH?"
            channel_item = QTreeWidgetItem([channel_label])
            added_channel = False
            for message_name, signals in sorted(message_map.items()):
                message_item = QTreeWidgetItem([message_name])
                added_message = False
                for signal_name in sorted(signals):
                    # Substring match: user types letters, no wildcards needed.
                    # Also supports wildcard patterns if user includes * or ?.
                    if pattern:
                        p_low = pattern.lower()
                        s_low = signal_name.lower()
                        # If pattern contains wildcard chars use fnmatch, else substring
                        if '*' in p_low or '?' in p_low:
                            if not fnmatch.fnmatch(s_low, p_low):
                                continue
                        elif p_low not in s_low:
                            continue
                    signal_item = QTreeWidgetItem([signal_name])
                    signal_item.setData(0, Qt.ItemDataRole.UserRole, f"{channel_label}::{message_name}::{signal_name}")
                    signal_item.setToolTip(0, "Double-click, right-click, Ctrl/Shift-select, or drag this signal to the plot area")
                    message_item.addChild(signal_item)
                    added_message = True
                if added_message:
                    channel_item.addChild(message_item)
                    added_channel = True
            if added_channel:
                self.tree.addTopLevelItem(channel_item)
                channel_item.setExpanded(True)
        self.tree.expandToDepth(1)

    def apply_filter(self) -> None:
        self.rebuild_tree()

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        keys = self.selected_signal_keys() or [self._item_key(item)]
        keys = [key for key in keys if key]
        if keys:
            self.signalActivated.emit(keys)

    def _show_context_menu(self, position) -> None:
        item = self.tree.itemAt(position)
        key = self._item_key(item)
        keys = self.selected_signal_keys()
        if not key and not keys:
            return
        if key and key not in keys:
            keys = [key]
        menu = QMenu(self.tree)
        plot_action = QAction(f"Plot selected signal(s) ({len(keys)})", self.tree)
        plot_action.triggered.connect(lambda: self.signalActivated.emit(keys))
        menu.addAction(plot_action)
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def selected_signal_keys(self) -> list[str]:
        keys: list[str] = []
        for item in self.tree.selectedItems():
            key = self._item_key(item)
            if key:
                keys.append(key)
        return keys

    @staticmethod
    def _item_key(item: QTreeWidgetItem | None) -> str | None:
        if item is None:
            return None
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if not key:
            return None
        return str(key)
