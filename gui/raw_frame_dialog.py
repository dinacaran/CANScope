from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollBar,
    QSizePolicy,
    QSpacerItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.signal_store import RawFrameEntry

# Visible window size — rows shown at once
_WINDOW   = 5_000
# Step size for ±step buttons (frames)
_STEP     = 1_000
# Column index constants
_COL_COUNT = 7


def _fmt_t(t: float) -> str:
    """Format a timestamp as '123.456 s', or '—' for sentinel."""
    if t is None:
        return '—'
    return f'{t:.3f} s'


class _NavButton(QWidget):
    """
    A navigation button with a time label displayed above it.

        ┌──────────┐
        │ 0.000 s  │  ← time label (updates dynamically)
        │[◀◀ Start]│  ← QPushButton
        └──────────┘
    """
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
        self.time_lbl.setText(_fmt_t(t) if t is not None else '—')

    def clicked_connect(self, slot) -> None:
        self.btn.clicked.connect(slot)


class RawFrameDialog(QDialog):
    """
    Raw CAN Frame viewer with a sliding window and time-labelled navigation.

    Navigation bar layout:
        [0.000 s]  [33.450 s]              [38.512 s]  [60.123 s]
        [◀◀ Start] [◀ −1000] ──●────────── [+1000 ▶]  [End ▶▶]  Jump:[___s][Go]

    The QScrollBar spans the entire filtered frame list.  The visible
    QTreeWidget always contains exactly _WINDOW rows (or fewer at the ends).
    Expand All / Collapse All operate only on the visible window.
    Signal children are rendered for all visible rows (decode already done
    at load time; rendering is the only cost, capped by _WINDOW).
    """

    def __init__(self, raw_frames: list[RawFrameEntry], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Raw CAN Frame Table')
        self.resize(1260, 750)

        self.raw_frames: list[RawFrameEntry] = list(raw_frames)
        self._matching:  list[RawFrameEntry] = []   # filtered view
        self._win_start: int = 0                    # index into _matching
        self._is_expanded: bool = False             # True after Expand All

        # ── Filter controls ───────────────────────────────────────────────
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            'Search frame name, ID, data, value...'
        )

        self.channel_combo = QComboBox()
        self.channel_combo.addItem('All Channels', None)
        channels = sorted(
            {rf.channel for rf in self.raw_frames},
            key=lambda x: (999_999 if x is None else x),
        )
        for ch in channels:
            self.channel_combo.addItem(
                f'CH{ch}' if ch is not None else 'CH?', ch
            )


        self.expand_btn   = QPushButton('Expand All')
        self.collapse_btn = QPushButton('Collapse All')

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel('Search'))
        top_row.addWidget(self.search_edit, 1)
        top_row.addWidget(QLabel('Channel'))
        top_row.addWidget(self.channel_combo)
        top_row.addWidget(self.expand_btn)
        top_row.addWidget(self.collapse_btn)

        # ── Status line ───────────────────────────────────────────────────
        self.status_label = QLabel()
        self.status_label.setStyleSheet('color: #8B949E; font-size: 11px;')

        # ── Navigation bar ────────────────────────────────────────────────
        self._nb_start  = _NavButton('◀◀ Start')
        self._nb_back   = _NavButton('◀ −1000')
        self._nb_fwd    = _NavButton('+1000 ▶')
        self._nb_end    = _NavButton('End ▶▶')

        # Section scrollbar — full filtered range
        self._scrollbar = QScrollBar(Qt.Orientation.Horizontal)
        self._scrollbar.setMinimum(0)
        self._scrollbar.setPageStep(_WINDOW)
        self._scrollbar.setSingleStep(_STEP)
        self._scrollbar.setMinimumHeight(30)
        self._scrollbar.setStyleSheet("""
            QScrollBar:horizontal {
                border: 1px solid #4a6080;
                background: #1a2a3a;
                height: 30px;
                border-radius: 4px;
            }
            QScrollBar::handle:horizontal {
                background: #3a6090;
                min-width: 24px;
                border-radius: 3px;
                border: 1px solid #5080b0;
            }
            QScrollBar::handle:horizontal:hover {
                background: #5090c0;
            }
            QScrollBar::handle:horizontal:pressed {
                background: #60a0d0;
            }
            QScrollBar::add-line:horizontal {
                border: 1px solid #4a6080;
                background: #253545;
                width: 20px;
                border-radius: 2px;
                subcontrol-position: right;
                subcontrol-origin: margin;
                image: url(none);
            }
            QScrollBar::sub-line:horizontal {
                border: 1px solid #4a6080;
                background: #253545;
                width: 20px;
                border-radius: 2px;
                subcontrol-position: left;
                subcontrol-origin: margin;
                image: url(none);
            }
            QScrollBar::left-arrow:horizontal {
                width: 8px; height: 8px;
                background: #8ab4d4;
                clip-path: polygon(100% 0%, 100% 100%, 0% 50%);
            }
            QScrollBar::right-arrow:horizontal {
                width: 8px; height: 8px;
                background: #8ab4d4;
                clip-path: polygon(0% 0%, 100% 50%, 0% 100%);
            }
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
                background: #1e2e3e;
            }
        """)

        # Jump-to-time widgets
        self._jump_edit = QLineEdit()
        self._jump_edit.setFixedWidth(90)
        self._jump_edit.setPlaceholderText('time (s)')
        self._jump_edit.setToolTip(
            'Type a time in seconds and press Enter or Go'
        )
        self._jump_btn  = QPushButton('Go')
        self._jump_btn.setFixedWidth(36)

        nav_top   = QHBoxLayout()   # time labels row
        nav_top.addWidget(self._nb_start)
        nav_top.addWidget(self._nb_back)
        nav_top.addWidget(self._scrollbar, 1)
        nav_top.addWidget(self._nb_fwd)
        nav_top.addWidget(self._nb_end)
        nav_top.addSpacing(8)
        nav_top.addWidget(QLabel('Jump:'))
        nav_top.addWidget(self._jump_edit)
        nav_top.addWidget(self._jump_btn)

        # ── Tree ──────────────────────────────────────────────────────────
        self.tree = QTreeWidget()
        self.tree.setColumnCount(_COL_COUNT)
        self.tree.setHeaderLabels(
            ['Time (s)', 'Chn', 'ID', 'Name', 'Dir', 'DLC', 'Data / Value']
        )
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setRootIsDecorated(True)

        # ── Layout ────────────────────────────────────────────────────────
        layout = QVBoxLayout(self)
        layout.addLayout(top_row)
        layout.addWidget(self.status_label)
        layout.addLayout(nav_top)
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

        # Initial render
        self._on_filter_changed()

    # ── Filter ────────────────────────────────────────────────────────────

    def _on_filter_changed(self) -> None:
        """Rebuild the filtered list and reset window to start."""
        needle         = self.search_edit.text().strip().lower()
        channel_filter = self.channel_combo.currentData()

        self._matching = [
            e for e in self.raw_frames
            if self._match_entry(e, needle, channel_filter)
        ]
        self._win_start = 0
        self._update_scrollbar()
        self._render_window()

    def _match_entry(
        self,
        entry: RawFrameEntry,
        needle: str,
        channel_filter,
    ) -> bool:
        if channel_filter is not None and entry.channel != channel_filter:
            return False
        if not needle:
            return True
        hay = ' '.join([
            f'{entry.time_s:.6f}',
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
            if needle in f'{sig.signal_name} {sig.physical_value} {sig.unit} {sig.raw_value}'.lower():
                return True
        return False

    # ── Navigation ────────────────────────────────────────────────────────

    def _go_start(self)  -> None: self._set_window(0)
    def _go_end(self)    -> None: self._set_window(max(0, len(self._matching) - _WINDOW))
    def _go_back(self)   -> None: self._set_window(max(0, self._win_start - _STEP))
    def _go_fwd(self)    -> None:
        self._set_window(min(
            max(0, len(self._matching) - _WINDOW),
            self._win_start + _STEP
        ))

    def _on_scroll(self, value: int) -> None:
        if value != self._win_start:
            self._set_window(value, update_scrollbar=False)

    def _go_jump(self) -> None:
        text = self._jump_edit.text().strip().rstrip('s').strip()
        if not text or not self._matching:
            return
        try:
            target_t = float(text)
        except ValueError:
            self._jump_edit.setStyleSheet('border: 1px solid #c04040;')
            return
        self._jump_edit.setStyleSheet('')   # clear error state
        # Binary search for nearest frame
        lo, hi = 0, len(self._matching) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self._matching[mid].time_s < target_t:
                lo = mid + 1
            else:
                hi = mid
        # Centre the window on that frame
        idx = max(0, min(lo, len(self._matching) - 1))
        start = max(0, min(idx - _WINDOW // 2,
                           len(self._matching) - _WINDOW))
        self._set_window(start)

    def _set_window(self, start: int, update_scrollbar: bool = True) -> None:
        n = len(self._matching)
        self._win_start = max(0, min(start, max(0, n - _WINDOW)))
        if update_scrollbar:
            self._scrollbar.blockSignals(True)
            self._scrollbar.setValue(self._win_start)
            self._scrollbar.blockSignals(False)
        # If tree is expanded, collapse first to avoid rebuilding
        # thousands of signal children during the clear() call.
        if self._is_expanded:
            self.tree.collapseAll()
            self._is_expanded = False
        self._render_window()

    # ── Scrollbar ─────────────────────────────────────────────────────────

    def _update_scrollbar(self) -> None:
        n = len(self._matching)
        max_val = max(0, n - _WINDOW)
        self._scrollbar.setMaximum(max_val)
        self._scrollbar.setValue(0)
        # Update jump spin max

    # ── Render window ─────────────────────────────────────────────────────

    def _render_window(self) -> None:
        n       = len(self._matching)
        w_start = self._win_start
        w_end   = min(w_start + _WINDOW, n)
        visible = self._matching[w_start:w_end]

        # ── Build QTreeWidgetItems in memory ──────────────────────────────
        top_items: list[QTreeWidgetItem] = []
        for entry in visible:
            ch_text = f'CAN {entry.channel}' if entry.channel is not None else 'CAN ?'
            top = QTreeWidgetItem([
                f'{entry.time_s:.6f}',
                ch_text,
                f'{entry.arbitration_id:X}',
                entry.frame_name or '(unmatched)',
                entry.direction,
                str(entry.dlc),
                entry.data_hex,
            ])
            for sig in entry.signals:
                phys = f'{sig.physical_value} {sig.unit}'.strip()
                top.addChild(QTreeWidgetItem(
                    ['', '', '', sig.signal_name, '', '', phys]
                ))
            top_items.append(top)

        # ── Single bulk insert ────────────────────────────────────────────
        self.tree.setUpdatesEnabled(False)
        self.tree.clear()
        self.tree.insertTopLevelItems(0, top_items)
        self.tree.setUpdatesEnabled(True)

        for col in range(_COL_COUNT):
            self.tree.resizeColumnToContents(col)

        # ── Update nav button time labels ─────────────────────────────────
        t_first = self._matching[0].time_s      if self._matching else None
        t_last  = self._matching[-1].time_s     if self._matching else None
        t_back  = self._matching[max(0, w_start - _STEP)].time_s   if self._matching else None
        t_fwd_i = min(w_start + _WINDOW + _STEP - 1, n - 1)
        t_fwd   = self._matching[t_fwd_i].time_s if self._matching else None

        self._nb_start.set_time(t_first)
        self._nb_back.set_time(t_back)
        self._nb_fwd.set_time(t_fwd)
        self._nb_end.set_time(t_last)

        # ── Status line ───────────────────────────────────────────────────
        t_win_start = visible[0].time_s  if visible else 0.0
        t_win_end   = visible[-1].time_s if visible else 0.0
        self.status_label.setText(
            f'Showing frames {w_start+1:,}–{w_end:,} of {n:,} matching  '
            f'({_fmt_t(t_win_start)} → {_fmt_t(t_win_end)})  '
            f'| Total in file: {len(self.raw_frames):,}'
        )

    # ── Expand window only ────────────────────────────────────────────────

    def _collapse_all(self) -> None:
        self._is_expanded = False
        self.tree.collapseAll()

    def _expand_window(self) -> None:
        """Expand all rows in the current visible window (not all 29k+)."""
        self._is_expanded = True
        self.tree.expandAll()
