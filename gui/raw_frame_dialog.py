
from __future__ import annotations

from collections import OrderedDict

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from core.signal_store import RawFrameEntry


class RawFrameDialog(QDialog):
    def __init__(self, raw_frames: list[RawFrameEntry], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Raw CAN Frame Table')
        self.resize(1200, 700)
        self.raw_frames = list(raw_frames)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText('Search frame name, signal name, ID, data, value...')
        self.channel_combo = QComboBox()
        self.channel_combo.addItem('All Channels', None)
        channels = sorted({rf.channel for rf in self.raw_frames}, key=lambda x: (999999 if x is None else x))
        for ch in channels:
            label = f'CH{ch}' if ch is not None else 'CH?'
            self.channel_combo.addItem(label, ch)

        self.decode_combo = QComboBox()
        self.decode_combo.addItem('All Frames', 'all')
        self.decode_combo.addItem('Decoded Only', 'decoded')
        self.decode_combo.addItem('Undecoded Only', 'undecoded')

        self.expand_btn = QPushButton('Expand All')
        self.collapse_btn = QPushButton('Collapse All')

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel('Search'))
        top_row.addWidget(self.search_edit, 1)
        top_row.addWidget(QLabel('Channel'))
        top_row.addWidget(self.channel_combo)
        top_row.addWidget(QLabel('Filter'))
        top_row.addWidget(self.decode_combo)
        top_row.addWidget(self.expand_btn)
        top_row.addWidget(self.collapse_btn)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(8)
        self.tree.setHeaderLabels(['Time', 'Start of Frame', 'Chn', 'ID', 'Name', 'Dir', 'DLC', 'Data / Value'])
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setRootIsDecorated(True)

        layout = QVBoxLayout(self)
        layout.addLayout(top_row)
        layout.addWidget(self.tree, 1)

        self.search_edit.textChanged.connect(self._refresh)
        self.channel_combo.currentIndexChanged.connect(self._refresh)
        self.decode_combo.currentIndexChanged.connect(self._refresh)
        self.expand_btn.clicked.connect(self.tree.expandAll)
        self.collapse_btn.clicked.connect(self.tree.collapseAll)

        self._refresh()

    def _match_entry(self, entry: RawFrameEntry, needle: str, channel_filter, decode_filter: str) -> bool:
        if channel_filter is not None and entry.channel != channel_filter:
            return False
        if decode_filter == 'decoded' and not entry.decoded:
            return False
        if decode_filter == 'undecoded' and entry.decoded:
            return False
        if not needle:
            return True

        hay = ' '.join([
            f'{entry.time_s:.6f}',
            f'{entry.start_of_frame_s:.6f}',
            f'CH{entry.channel}' if entry.channel is not None else 'CH?',
            f'{entry.arbitration_id:X}',
            entry.frame_name,
            entry.direction,
            str(entry.dlc),
            entry.data_hex,
        ]).lower()

        if needle in hay:
            return True

        for sig in entry.signals:
            sig_hay = f'{sig.signal_name} {sig.physical_value} {sig.unit} {sig.raw_value}'.lower()
            if needle in sig_hay:
                return True
        return False

    def _refresh(self) -> None:
        self.tree.clear()
        needle = self.search_edit.text().strip().lower()
        channel_filter = self.channel_combo.currentData()
        decode_filter = self.decode_combo.currentData()

        for entry in self.raw_frames:
            if not self._match_entry(entry, needle, channel_filter, decode_filter):
                continue

            ch_text = f'CAN {entry.channel}' if entry.channel is not None else 'CAN ?'
            id_text = f'{entry.arbitration_id:X}' if entry.arbitration_id > 0x7FF else f'{entry.arbitration_id:X}'
            name_text = entry.frame_name or '(unmatched)'
            top = QTreeWidgetItem([
                f'{entry.time_s:.6f}',
                f'{entry.start_of_frame_s:.6f}',
                ch_text,
                id_text,
                name_text,
                entry.direction,
                str(entry.dlc),
                entry.data_hex,
            ])
            if entry.decoded:
                top.setExpanded(False)
            self.tree.addTopLevelItem(top)

            for sig in entry.signals:
                phys = f'{sig.physical_value} {sig.unit}'.strip()
                child = QTreeWidgetItem([
                    '',
                    '',
                    '',
                    '',
                    sig.signal_name,
                    '',
                    '',
                    phys,
                ])
                top.addChild(child)

        for col in range(self.tree.columnCount()):
            self.tree.resizeColumnToContents(col)
