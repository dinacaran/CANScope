from __future__ import annotations

import array as _array

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollBar, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from core.raw_frame_store import RawFrameStore

_WINDOW = 5_000
_STEP   = 1_000
_COL    = 7


def _fmt_t(t: float | None) -> str:
    return f'{t:.3f} s' if t is not None else '—'


class _NavButton(QWidget):
    """Button with a time label displayed above it."""
    def __init__(self, label: str, parent=None) -> None:
        super().__init__(parent)
        self.time_lbl = QLabel('—')
        self.time_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.time_lbl.setStyleSheet('color: #6ea8d8; font-size: 10px;')
        self.btn = QPushButton(label)
        self.btn.setFixedWidth(max(72, len(label) * 8))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(1)
        lay.addWidget(self.time_lbl)
        lay.addWidget(self.btn)

    def set_time(self, t: float | None) -> None:
        self.time_lbl.setText(_fmt_t(t))

    def clicked_connect(self, slot) -> None:
        self.btn.clicked.connect(slot)


class RawFrameDialog(QDialog):
    """
    Raw CAN Frame viewer — on-disk sliding window, no frame cap.

    Accepts a RawFrameStore (Option B architecture):
    - Filtering uses vectorised numpy on in-memory arrays — no disk access
    - Rendering reads only the visible 5,000-frame window from disk
    - Signals decoded on-demand when a row is expanded
    """

    def __init__(self, raw_store: RawFrameStore, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Raw CAN Frame Table')
        self.resize(1260, 750)

        self._raw_store = raw_store
        self._win_start = 0
        self._is_expanded = False

        # _matching: None = all frames; array('I') = filtered indices
        self._matching: _array.array | None = None

        # ── Filter controls ───────────────────────────────────────────────
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            'Search frame name, ID, data, direction…'
        )

        self.channel_combo = QComboBox()
        self.channel_combo.addItem('All Channels', None)
        chs_seen = sorted(
            {int(c) for c in raw_store.channels if c != 255},
            key=lambda x: x,
        )
        for ch in chs_seen:
            self.channel_combo.addItem(f'CAN {ch}', ch)

        self.expand_btn   = QPushButton('Expand All')
        self.collapse_btn = QPushButton('Collapse All')

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel('Search'))
        top_row.addWidget(self.search_edit, 1)
        top_row.addWidget(QLabel('Channel'))
        top_row.addWidget(self.channel_combo)
        top_row.addWidget(self.expand_btn)
        top_row.addWidget(self.collapse_btn)

        # ── Status ────────────────────────────────────────────────────────
        self.status_label = QLabel()
        self.status_label.setStyleSheet('color: #8B949E; font-size: 11px;')

        # ── Navigation bar ────────────────────────────────────────────────
        self._nb_start = _NavButton('◀◀ Start')
        self._nb_back  = _NavButton('◀ −1000')
        self._nb_fwd   = _NavButton('+1000 ▶')
        self._nb_end   = _NavButton('End ▶▶')

        self._scrollbar = QScrollBar(Qt.Orientation.Horizontal)
        self._scrollbar.setMinimum(0)
        self._scrollbar.setPageStep(_WINDOW)
        self._scrollbar.setSingleStep(_STEP)
        self._scrollbar.setMinimumHeight(30)
        self._scrollbar.setStyleSheet("""
            QScrollBar:horizontal {
                border: 1px solid #4a6080; background: #1a2a3a;
                height: 30px; border-radius: 4px;
            }
            QScrollBar::handle:horizontal {
                background: #3a6090; min-width: 24px;
                border-radius: 3px; border: 1px solid #5080b0;
            }
            QScrollBar::handle:horizontal:hover  { background: #5090c0; }
            QScrollBar::handle:horizontal:pressed { background: #60a0d0; }
            QScrollBar::add-line:horizontal {
                border: 1px solid #4a6080; background: #253545;
                width: 20px; border-radius: 2px;
                subcontrol-position: right; subcontrol-origin: margin;
            }
            QScrollBar::sub-line:horizontal {
                border: 1px solid #4a6080; background: #253545;
                width: 20px; border-radius: 2px;
                subcontrol-position: left; subcontrol-origin: margin;
            }
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
                background: #1e2e3e;
            }
        """)

        self._jump_edit = QLineEdit()
        self._jump_edit.setFixedWidth(90)
        self._jump_edit.setPlaceholderText('time (s)')
        self._jump_edit.setToolTip('Type a time in seconds and press Enter or Go')
        self._jump_btn = QPushButton('Go')
        self._jump_btn.setFixedWidth(36)

        nav_row = QHBoxLayout()
        nav_row.addWidget(self._nb_start)
        nav_row.addWidget(self._nb_back)
        nav_row.addWidget(self._scrollbar, 1)
        nav_row.addWidget(self._nb_fwd)
        nav_row.addWidget(self._nb_end)
        nav_row.addSpacing(8)
        nav_row.addWidget(QLabel('Jump:'))
        nav_row.addWidget(self._jump_edit)
        nav_row.addWidget(self._jump_btn)

        # ── Tree ──────────────────────────────────────────────────────────
        self.tree = QTreeWidget()
        self.tree.setColumnCount(_COL)
        self.tree.setHeaderLabels(
            ['Time (s)', 'Chn', 'ID', 'Name', 'Dir', 'DLC', 'Data / Value']
        )
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(False)
        self.tree.setRootIsDecorated(True)

        # ── Layout ────────────────────────────────────────────────────────
        layout = QVBoxLayout(self)
        layout.addLayout(top_row)
        layout.addWidget(self.status_label)
        layout.addLayout(nav_row)
        layout.addWidget(self.tree, 1)

        # ── Connections ───────────────────────────────────────────────────
        self.search_edit.textChanged.connect(self._on_filter_changed)
        self.channel_combo.currentIndexChanged.connect(self._on_filter_changed)
        self._nb_start.clicked_connect(self._go_start)
        self._nb_back.clicked_connect(self._go_back)
        self._nb_fwd.clicked_connect(self._go_fwd)
        self._nb_end.clicked_connect(self._go_end)
        self._scrollbar.valueChanged.connect(self._on_scroll)
        self._jump_btn.clicked.connect(self._go_jump)
        self._jump_edit.returnPressed.connect(self._go_jump)
        self.expand_btn.clicked.connect(self._expand_window)
        self.collapse_btn.clicked.connect(self._collapse_all)
        self.tree.itemExpanded.connect(self._on_item_expanded)

        self._on_filter_changed()

    # ── Filter ────────────────────────────────────────────────────────────

    def _on_filter_changed(self) -> None:
        needle         = self.search_edit.text().strip().lower()
        channel_filter = self.channel_combo.currentData()

        mask = self._raw_store.build_match_mask(needle, channel_filter)
        if mask is None:
            # All frames match — use None sentinel (zero extra RAM)
            self._matching = None
        else:
            indices = np.where(mask)[0].astype(np.uint32)
            self._matching = _array.array('I', indices.tolist())

        self._win_start = 0
        self._update_scrollbar()
        self._render_window()

    def _total_matching(self) -> int:
        if self._matching is None:
            return len(self._raw_store)
        return len(self._matching)

    def _frame_index(self, pos: int) -> int:
        """Map a position in the matching list to a RawFrameStore index."""
        if self._matching is None:
            return pos
        return int(self._matching[pos])

    # ── Navigation ────────────────────────────────────────────────────────

    def _go_start(self)  -> None: self._set_window(0)
    def _go_end(self)    -> None: self._set_window(max(0, self._total_matching() - _WINDOW))
    def _go_back(self)   -> None: self._set_window(max(0, self._win_start - _STEP))
    def _go_fwd(self)    -> None:
        self._set_window(min(max(0, self._total_matching() - _WINDOW),
                             self._win_start + _STEP))

    def _on_scroll(self, value: int) -> None:
        if value != self._win_start:
            self._set_window(value, update_scrollbar=False)

    def _go_jump(self) -> None:
        text = self._jump_edit.text().strip().rstrip('s').strip()
        if not text:
            return
        try:
            target_t = float(text)
        except ValueError:
            self._jump_edit.setStyleSheet('border: 1px solid #c04040;')
            return
        self._jump_edit.setStyleSheet('')

        n = self._total_matching()
        if n == 0:
            return
        ts_store = np.frombuffer(self._raw_store.timestamps, dtype=np.float64)

        # Binary search in the matching subset
        lo, hi = 0, n - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if ts_store[self._frame_index(mid)] < target_t:
                lo = mid + 1
            else:
                hi = mid

        start = max(0, min(lo - _WINDOW // 2, n - _WINDOW))
        self._set_window(start)

    def _set_window(self, start: int, update_scrollbar: bool = True) -> None:
        n = self._total_matching()
        self._win_start = max(0, min(start, max(0, n - _WINDOW)))
        if update_scrollbar:
            self._scrollbar.blockSignals(True)
            self._scrollbar.setValue(self._win_start)
            self._scrollbar.blockSignals(False)
        if self._is_expanded:
            self.tree.collapseAll()
            self._is_expanded = False
        self._render_window()

    def _update_scrollbar(self) -> None:
        n       = self._total_matching()
        max_val = max(0, n - _WINDOW)
        self._scrollbar.setMaximum(max_val)
        self._scrollbar.setValue(0)

    # ── Render window ─────────────────────────────────────────────────────

    def _render_window(self) -> None:
        n       = self._total_matching()
        w_start = self._win_start
        w_end   = min(w_start + _WINDOW, n)

        # Collect frame indices for this window
        indices = [self._frame_index(i) for i in range(w_start, w_end)]

        # Fetch records from store (disk read for data bytes)
        records = self._raw_store.get_window(indices)

        top_items: list[QTreeWidgetItem] = []
        for rec in records:
            ch_text = f'CAN {rec.channel}' if rec.channel is not None else 'CAN ?'
            id_text = f'{rec.arbitration_id:X}'
            data_hex = ' '.join(f'{b:02X}' for b in rec.data[:rec.dlc])

            top = QTreeWidgetItem([
                f'{rec.time_s:.6f}',
                ch_text,
                id_text,
                rec.frame_name or ('(decoded)' if rec.decoded else '(unmatched)'),
                rec.direction,
                str(rec.dlc),
                data_hex,
            ])
            # Store store-index in item data for lazy signal decode
            top.setData(0, Qt.ItemDataRole.UserRole, indices[len(top_items)])
            # Placeholder child so the expand arrow appears for decoded frames
            if rec.decoded:
                placeholder = QTreeWidgetItem(['…'])
                placeholder.setData(0, Qt.ItemDataRole.UserRole, -1)  # sentinel
                top.addChild(placeholder)
            top_items.append(top)

        self.tree.setUpdatesEnabled(False)
        self.tree.clear()
        self.tree.insertTopLevelItems(0, top_items)
        self.tree.setUpdatesEnabled(True)

        for col in range(_COL):
            self.tree.resizeColumnToContents(col)

        # Update nav button time labels
        ts_arr = np.frombuffer(self._raw_store.timestamps, dtype=np.float64)
        n_store = len(self._raw_store)

        def _ts(pos: int) -> float | None:
            idx = self._frame_index(pos) if 0 <= pos < n else None
            return float(ts_arr[idx]) if idx is not None and 0 <= idx < n_store else None

        t_win_s = _ts(w_start)
        t_win_e = _ts(w_end - 1)
        self._nb_start.set_time(_ts(0))
        self._nb_back.set_time(_ts(max(0, w_start - _STEP)))
        self._nb_fwd.set_time(_ts(min(n - 1, w_start + _WINDOW + _STEP - 1)))
        self._nb_end.set_time(_ts(n - 1))

        self.status_label.setText(
            f'Showing frames {w_start+1:,}–{w_end:,} of {n:,} matching  '
            f'({_fmt_t(t_win_s)} → {_fmt_t(t_win_e)})  '
            f'| Total in file: {n_store:,}'
        )

    # ── Lazy signal decode on expand ──────────────────────────────────────

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        """Decode signals for this frame on first expand."""
        # Check if placeholder is still present
        if item.childCount() == 0:
            return
        first_child = item.child(0)
        if first_child is None or first_child.data(0, Qt.ItemDataRole.UserRole) != -1:
            return   # already decoded

        store_idx = item.data(0, Qt.ItemDataRole.UserRole)
        if store_idx is None or store_idx < 0:
            return

        # Remove placeholder
        item.removeChild(first_child)

        decoder = getattr(self._raw_store, 'decoder', None)
        if decoder is None:
            item.addChild(QTreeWidgetItem(['', '', '', '(no DBC)', '', '', '']))
            return

        # Reconstruct a minimal RawFrame for the decoder
        from core.models import RawFrame
        records = self._raw_store.get_window([store_idx])
        if not records:
            return
        rec = records[0]

        raw_frame = RawFrame(
            timestamp=rec.time_s,
            channel=rec.channel,
            arbitration_id=rec.arbitration_id,
            is_extended_id=rec.is_extended,
            is_fd=rec.is_fd,
            dlc=rec.dlc,
            data=rec.data[:rec.dlc],
            direction=rec.direction,
        )
        try:
            samples = decoder.decode_frame(raw_frame)
        except Exception:
            samples = []

        if not samples:
            item.addChild(QTreeWidgetItem(['', '', '', '(no signals)', '', '', '']))
            return

        for s in samples:
            val_str = f'{s.value} {s.unit}'.strip()
            child = QTreeWidgetItem(['', '', '', s.signal_name, '', '', val_str])
            child.setData(0, Qt.ItemDataRole.UserRole, -2)
            item.addChild(child)

    # ── Expand / Collapse ─────────────────────────────────────────────────

    def _expand_window(self) -> None:
        self._is_expanded = True
        self.tree.expandAll()

    def _collapse_all(self) -> None:
        self._is_expanded = False
        self.tree.collapseAll()
