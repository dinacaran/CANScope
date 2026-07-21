from __future__ import annotations

import fnmatch

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtGui import QAction, QDrag, QKeySequence, QShortcut, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QToolButton,
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
    generatedEditRequested = Signal(str)
    generatedDeleteRequested = Signal(str)
    MIME_TYPE = SignalTree.MIME_TYPE
    _GENERATED_ROLE = int(Qt.ItemDataRole.UserRole) + 1

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._payload: dict[int | None, dict[str, list[str]]] = {}
        self._generated_signals: list[tuple[str, str, str]] = []
        self._messages_expanded = True

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search signals... (type to filter)")

        self.collapse_messages_button = QToolButton()
        self.collapse_messages_button.setText("Collapse")
        self.collapse_messages_button.setToolTip(
            "Collapse all messages — show message names only"
        )
        self.collapse_messages_button.setAccessibleName("Collapse all messages")
        self.collapse_messages_button.setEnabled(False)
        self.collapse_messages_button.clicked.connect(self.collapse_messages)

        self.expand_messages_button = QToolButton()
        self.expand_messages_button.setText("Expand")
        self.expand_messages_button.setToolTip(
            "Expand all messages — show signals"
        )
        self.expand_messages_button.setAccessibleName("Expand all messages")
        self.expand_messages_button.setEnabled(False)
        self.expand_messages_button.clicked.connect(self.expand_messages)

        self.tree = SignalTree()
        self.tree.setColumnCount(1)
        self.tree.setHeaderHidden(True)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.tree.setDragEnabled(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)

        # Keep the tree heading visually consistent with a native QTreeWidget
        # header while allowing the message controls to sit directly below it.
        self._tree_header_model = QStandardItemModel(0, 1, self)
        self._tree_header_model.setHeaderData(
            0,
            Qt.Orientation.Horizontal,
            "Channels / Messages / Signals",
        )
        self.tree_header = QHeaderView(Qt.Orientation.Horizontal)
        self.tree_header.setModel(self._tree_header_model)
        self.tree_header.setSectionsClickable(False)
        self.tree_header.setStretchLastSection(True)
        self.tree_header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.tree_header.setFixedHeight(self.tree_header.sizeHint().height())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.addWidget(self.search_edit)
        layout.addWidget(self.tree_header)

        message_controls = QHBoxLayout()
        message_controls.setContentsMargins(0, 0, 0, 0)
        message_controls.addStretch(1)
        message_controls.addWidget(self.collapse_messages_button)
        message_controls.addWidget(self.expand_messages_button)
        layout.addLayout(message_controls)
        layout.addWidget(self.tree)

        self.search_edit.textChanged.connect(self.apply_filter)

        self._collapse_shortcut = QShortcut(QKeySequence("Ctrl+Shift+-"), self)
        self._collapse_shortcut.activated.connect(self.collapse_messages)
        self._expand_shortcut = QShortcut(QKeySequence("Ctrl+Shift++"), self)
        self._expand_shortcut.activated.connect(self.expand_messages)

    def set_payload(self, payload: dict[int | None, dict[str, list[str]]]) -> None:
        self._payload = payload
        self.rebuild_tree()

    def set_generated_signals(self, signals: list[tuple[str, str, str]]) -> None:
        """Set ``(key, name, tooltip)`` rows shown under Generate Signals."""
        self._generated_signals = list(signals)
        self.rebuild_tree()

    def rebuild_tree(self) -> None:
        self.tree.clear()
        pattern = self.search_edit.text().strip()
        p_low = pattern.lower()

        generated_root = QTreeWidgetItem(["Generate Signals"])
        generated_root.setData(0, self._GENERATED_ROLE, True)
        for key, name, tooltip in self._generated_signals:
            if pattern:
                searchable = f"{name} {tooltip}".lower()
                if '*' in p_low or '?' in p_low:
                    if not fnmatch.fnmatch(searchable, f"*{p_low}*"):
                        continue
                elif p_low not in searchable:
                    continue
            signal_item = QTreeWidgetItem([name])
            signal_item.setData(0, Qt.ItemDataRole.UserRole, key)
            signal_item.setData(0, self._GENERATED_ROLE, True)
            signal_item.setToolTip(0, tooltip)
            generated_font = signal_item.font(0)
            generated_font.setItalic(True)
            signal_item.setFont(0, generated_font)
            generated_root.addChild(signal_item)
        if generated_root.childCount():
            self.tree.addTopLevelItem(generated_root)
            generated_root.setExpanded(True)

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
        # Keep the accepted initial load behaviour: channels and messages are
        # expanded after the payload is built.  A search temporarily expands
        # its matching messages; clearing it restores the user's chosen mode.
        if pattern or self._messages_expanded:
            self.tree.expandToDepth(1)

        has_messages = any(
            not self.tree.topLevelItem(index).data(0, self._GENERATED_ROLE)
            for index in range(self.tree.topLevelItemCount())
        )
        self.collapse_messages_button.setEnabled(has_messages)
        self.expand_messages_button.setEnabled(has_messages)

    def apply_filter(self) -> None:
        self.rebuild_tree()

    def collapse_messages(self) -> None:
        """Show channel/message names while hiding all signal children."""
        self._set_messages_expanded(False)

    def expand_messages(self) -> None:
        """Expand every message under every visible channel."""
        self._set_messages_expanded(True)

    def _set_messages_expanded(self, expanded: bool) -> None:
        """Update existing message items in place; never rebuild the payload."""
        self._messages_expanded = expanded
        scroll_bar = self.tree.verticalScrollBar()
        old_scroll_value = scroll_bar.value()

        self.tree.setUpdatesEnabled(False)
        try:
            for channel_index in range(self.tree.topLevelItemCount()):
                channel_item = self.tree.topLevelItem(channel_index)
                channel_item.setExpanded(True)
                if channel_item.data(0, self._GENERATED_ROLE):
                    continue
                for message_index in range(channel_item.childCount()):
                    channel_item.child(message_index).setExpanded(expanded)
        finally:
            self.tree.setUpdatesEnabled(True)

        scroll_bar.setValue(min(old_scroll_value, scroll_bar.maximum()))
        self.tree.viewport().update()

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
        if key and item is not None and item.data(0, self._GENERATED_ROLE):
            menu.addSeparator()
            edit_action = QAction("Edit generated signal", self.tree)
            edit_action.triggered.connect(
                lambda _checked=False, generated_key=key: self.generatedEditRequested.emit(generated_key)
            )
            menu.addAction(edit_action)
            delete_action = QAction("Delete generated signal", self.tree)
            delete_action.triggered.connect(
                lambda _checked=False, generated_key=key: self.generatedDeleteRequested.emit(generated_key)
            )
            menu.addAction(delete_action)
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
