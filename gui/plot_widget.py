from __future__ import annotations

from dataclasses import dataclass
from itertools import cycle
from typing import Any

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal, QRectF
from PySide6.QtGui import QAction, QBrush, QColor, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QInputDialog,
    QGraphicsTextItem,
    QColorDialog,
    QGridLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.signal_store import SignalSeries
from gui.signal_tree import SignalTreeWidget


@dataclass(slots=True)
class PlottedSignal:
    key: str
    series: SignalSeries
    curve: Any
    color: str
    axis: Any = None
    view_box: Any = None
    visible: bool = True        # checkbox state — False hides curve and stacked row
    group: str = ''             # '' = ungrouped; any string = group name
    axis_visible: bool = True   # multi-axis mode: show/hide the Y axis for this signal



class _BottomClippedTextItem(QGraphicsTextItem):
    """
    QGraphicsTextItem that clips itself to a caller-specified local x-extent.

    After pyqtgraph rotates the axis label -90 degrees (CCW):
      local +x  →  screen UP
      local +y  →  screen RIGHT (toward data)

    Clipping local x to [0, _max_x] means the label text is only visible
    from the bottom of the row upward for max_x pixels — it never bleeds
    into the row above.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._max_x: float = 1e9   # effectively no clip until set_clip_x called

    def set_clip_x(self, max_x: float) -> None:
        self._max_x = max(0.0, max_x)
        self.update()

    def paint(self, painter, option, widget=None):
        if self._max_x < 1e8:
            br = self.boundingRect()
            # Clip in LOCAL (pre-rotation) space:
            #   x  [0, _max_x]  — along text direction (= screen UP after rotation)
            #   y  covers full font height with generous padding
            painter.setClipRect(
                QRectF(0, br.y() - 4, self._max_x, br.height() + 8)
            )
        super().paint(painter, option, widget)


class _StackedLeftAxis(pg.AxisItem):
    """
    Left axis for stacked mode:
    1. Label anchored to BOTTOM of row  (text start = bottom edge)
    2. Label clipped at TOP of row      (no overflow into row above)

    Coordinate reasoning (pyqtgraph left axis, rotation = -90° CCW):
      In the AxisItem's local coordinate frame:
        label.pos().y()  is the screen-y of the text START
        screen_y = axis_height  →  bottom edge of the row
      After setting pos to (x, ax_h - MARGIN), the text starts at the
      bottom and reads upward.  The clip limits local_x to [0, ax_h - 2*MARGIN]
      so the text stops exactly at the top edge.
    """
    _MARGIN = 3   # pixels gap from top/bottom edges

    def __init__(self, orientation, *args, **kwargs):
        super().__init__(orientation, *args, **kwargs)
        # Replace pyqtgraph's plain QGraphicsTextItem with our clippable subclass.
        # The old label is hidden; pyqtgraph will update self.label going forward.
        if hasattr(self, 'label') and self.label is not None:
            self.label.setVisible(False)          # hide original
            new_lbl = _BottomClippedTextItem(self)  # child of AxisItem
            new_lbl.setRotation(self.label.rotation())
            self.label = new_lbl                  # pyqtgraph uses this reference

    def resizeEvent(self, ev=None):
        # super() positions label at center (y = ax_h / 2)
        super().resizeEvent(ev)
        self._anchor_label_bottom()

    def _anchor_label_bottom(self) -> None:
        if not isinstance(getattr(self, 'label', None), _BottomClippedTextItem):
            return
        ax_h = self.size().height()
        if ax_h <= 0:
            return
        m = self._MARGIN
        label = self.label

        # Keep x (axis-width centering set by super) — change only y
        label.setPos(label.pos().x(), ax_h - m)

        # Clip: prevent text going above top of row
        # Screen_y of text top = ax_h - m - local_x; want >= m → local_x <= ax_h - 2m
        label.set_clip_x(max(0.0, ax_h - 2 * m))


class PlotPanel(QWidget):
    selectionChanged = Signal(str)
    signalDropped = Signal(list)
    backgroundColorChanged = Signal(str)
    signalColorChanged = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: dict[str, PlottedSignal] = {}
        self._current_key: str | None = None
        self._cursor_label_base = 'Cursor: move mouse over plot'
        self._show_points = False
        self._multi_axis = False
        self._stacked_mode = False
        self._extra_axes: list[tuple[Any, Any]] = []
        self._stacked_plots: list[pg.PlotItem] = []
        self._stacked_vlines: list[pg.InfiniteLine] = []   # legacy (unused)
        # Per-row cursor line instances for stacked mode
        # (one InfiniteLine per row — they cannot be shared across scenes)
        self._stacked_c1_lines: list[pg.InfiniteLine] = []
        self._stacked_c2_lines: list[pg.InfiniteLine] = []
        self._proxy = None          # legacy name kept; now stores the connected scene
        self._row_lookup: dict[str, int] = {}   # key → table row; rebuilt by _refresh_table
        self._color_cycle = cycle([
            '#e41a1c', '#377eb8', '#4daf4a', '#984ea3', '#ff7f00',
            '#a65628', '#f781bf', '#17becf', '#bcbd22', '#1f77b4',
        ])
        self._background_color = '#000000'
        # Undo stack — stores (items_snapshot, current_key) tuples
        # Each snapshot is a shallow copy of _items key order + color.
        # Limited to _UNDO_DEPTH entries (oldest dropped first).
        self._undo_stack: list[tuple[dict, str | None]] = []
        self._UNDO_DEPTH: int = 3
        self._cursor1_enabled: bool = True   # mirrors button default
        # Signal name display flags (default: signal name only)
        self._name_show_channel: bool = False
        self._name_show_message: bool = False
        self._collapsed_groups: dict[str, bool] = {}
        self._cursor2_enabled: bool = False
        self._group_vis_changed: bool = False  # True when itemChanged fired before cellClicked
        self._batch_mode: bool = False          # True while batch-adding signals; suppresses per-add rebuilds
        self.setAcceptDrops(True)

        # ── Normal / multi-axis plot ──────────────────────────────────────
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self._legend = self.plot.addLegend()
        self.plot.setLabel('bottom', 'Time (seconds)')
        self.plot.setBackground(self._background_color)
        self._install_plot_background_menu()
        self._install_plot_click_deselect()

        self.plot_host = QWidget()
        _host_layout = QGridLayout(self.plot_host)
        _host_layout.setContentsMargins(0, 0, 0, 0)
        _host_layout.addWidget(self.plot, 0, 0)

        self.overlay_label = QLabel()
        self.overlay_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.overlay_label.setWordWrap(True)
        self.overlay_label.setStyleSheet(
            'QLabel { color: white; font-size: 20px; font-weight: 600; '
            'background-color: rgba(0,0,0,110); padding: 18px; border-radius: 8px; }'
        )
        _host_layout.addWidget(self.overlay_label, 0, 0, alignment=Qt.AlignmentFlag.AlignCenter)

        # ── Stacked plot (GraphicsLayoutWidget) ──────────────────────────
        self.glw = pg.GraphicsLayoutWidget()
        self.glw.setBackground(self._background_color)

        # ── View switcher ─────────────────────────────────────────────────
        self.view_stack = QStackedWidget()
        self.view_stack.addWidget(self.plot_host)   # index 0 – normal
        self.view_stack.addWidget(self.glw)          # index 1 – stacked

        # ── Status / hint labels ──────────────────────────────────────────
        self.drop_hint = QLabel(
            'Drag signal(s) here, double-click them, or right-click and choose Plot selected signal(s)'
        )
        self.cursor_label = QLabel(self._cursor_label_base)
        self.cursor2_label = QLabel('')
        self.drop_hint.hide()
        self.cursor_label.hide()
        self.cursor2_label.hide()

        # ── Cursor 1: draggable vertical line (ON by default) ─────────────
        self.v_line = pg.InfiniteLine(
            angle=90, movable=True,
            pen=pg.mkPen(color='#0000ff', width=1.5),
            label='C1', labelOpts={'color': '#0000ff', 'position': 0.95}
        )
        self.h_line = pg.InfiniteLine(angle=0, movable=False,
                                      pen=pg.mkPen(color='#555', width=1))
        self.v_line.sigPositionChanged.connect(self._on_cursor1_moved)
        self.plot.addItem(self.v_line, ignoreBounds=True)
        self.plot.addItem(self.h_line, ignoreBounds=True)

        # ── Cursor 2: draggable, off by default ──────────────────────────
        self.v_line2 = pg.InfiniteLine(
            angle=90, movable=True,
            pen=pg.mkPen(color='#0000ff', width=1.5, style=Qt.PenStyle.DashLine),
            label='C2', labelOpts={'color': '#0000ff', 'position': 0.85}
        )
        self.v_line2.sigPositionChanged.connect(self._on_cursor2_moved)
        # v_line2 not added to plot until cursor 2 is enabled

        # ── Signal table ──────────────────────────────────────────────────
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ['☑', 'Signal', 'Cursor 1', 'Cursor 2', 'Unit', 'Axis'])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.itemSelectionChanged.connect(self._emit_selection)
        self.table.itemChanged.connect(self._on_table_item_changed)
        self.table.itemDoubleClicked.connect(self._on_table_item_double_clicked)
        self.table.cellClicked.connect(self._on_table_cell_clicked)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_table_menu)
        self.table.verticalHeader().setVisible(False)
        hdr = self.table.horizontalHeader()
        hdr.setStretchLastSection(False)
        hdr.setStretchLastSection(False)
        hdr.setMinimumSectionSize(0)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)      # ☑
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive) # Signal
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive) # Cursor 1
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive) # Cursor 2
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive) # Unit
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)      # Axis
        self.table.setColumnWidth(0, 36)    # ☑ checkbox + group arrow
        self.table.setColumnWidth(1, 190)   # Signal
        self.table.setColumnWidth(2, 90)    # Cursor 1
        self.table.setColumnWidth(3, 90)    # Cursor 2
        self.table.setColumnWidth(4, 55)    # Unit
        self.table.setColumnWidth(5, 36)    # Axis (multi-axis mode only)
        self.table.setColumnHidden(3, True) # hidden until cursor 2 enabled
        self.table.setColumnHidden(5, True) # hidden until multi-axis mode enabled

        self.table_panel = QWidget()
        _tbl_layout = QVBoxLayout(self.table_panel)
        _tbl_layout.setContentsMargins(0, 0, 0, 0)
        _tbl_layout.addWidget(self.table, stretch=1)
        # Drag-and-drop onto the signal table (same as plot area)
        self.table.setAcceptDrops(True)
        self.table.viewport().setAcceptDrops(True)
        self.table.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.table.dragEnterEvent = self._table_drag_enter
        self.table.dragMoveEvent  = self._table_drag_enter   # same check
        self.table.dropEvent      = self._table_drop
        self._apply_panel_background()

        # ── Root layout ───────────────────────────────────────────────────
        _root = QVBoxLayout(self)
        _root.setContentsMargins(4, 4, 4, 4)
        _root.addWidget(self.view_stack, stretch=1)
        _root.addWidget(self.drop_hint)
        _root.addWidget(self.cursor_label)
        _root.addWidget(self.cursor2_label)

        self._setup_mouse_proxy()
        self._update_empty_state_ui()

    # ── Drag / drop ───────────────────────────────────────────────────────

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasFormat(SignalTreeWidget.MIME_TYPE):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        if not event.mimeData().hasFormat(SignalTreeWidget.MIME_TYPE):
            event.ignore()
            return
        payload = bytes(event.mimeData().data(SignalTreeWidget.MIME_TYPE)).decode('utf-8')
        keys = [p.strip() for p in payload.splitlines() if p.strip()]
        if keys:
            self.signalDropped.emit(keys)
            event.acceptProposedAction()
        else:
            event.ignore()

    # ── Public setters ────────────────────────────────────────────────────

    def set_show_points(self, show: bool) -> None:
        self._show_points = bool(show)
        for plotted in self._items.values():
            self._apply_curve_style(plotted, set_data=False)

    def set_multi_axis(self, enabled: bool) -> None:
        self._multi_axis = bool(enabled)
        self.table.setColumnHidden(5, not self._multi_axis)
        self._rebuild_curves(preserve_selection=True)
        self.fit_to_window()

    def _current_view_centre(self) -> float:
        """Return the x-centre of the currently visible range (any mode)."""
        try:
            if self._stacked_mode and self._stacked_plots:
                xr = self._stacked_plots[0].vb.viewRange()[0]
            else:
                xr = self.plot.plotItem.vb.viewRange()[0]
            return (xr[0] + xr[1]) / 2.0
        except Exception:
            return 0.0

    def set_cursor1_enabled(self, enabled: bool) -> None:
        """Show/hide Cursor 1. When turned ON, always centres it in the current view."""
        self._cursor1_enabled = bool(enabled)
        cx = self._current_view_centre()
        if enabled:
            self.v_line.setPos(cx)
            if self._stacked_mode:
                # Stacked: show/move per-row lines; label only on bottom row
                last = len(self._stacked_c1_lines) - 1
                for i, line in enumerate(self._stacked_c1_lines):
                    line.setPos(cx)
                    line.setPen(pg.mkPen(color='#0000ff', width=1.5))
                    if hasattr(line, 'label') and line.label is not None:
                        line.label.setVisible(i == last)
            else:
                # Normal/multi-axis: v_line lives in self.plot only
                try: self.plot.addItem(self.v_line, ignoreBounds=True)
                except Exception: pass
            if self._items:
                self._update_table_values(cx, col=2)
        else:
            if self._stacked_mode:
                # Hide by making invisible (don't remove — rebuild re-adds them)
                for line in self._stacked_c1_lines:
                    line.setPen(pg.mkPen(color='#00000000', width=0))
                    if hasattr(line, 'label') and line.label is not None:
                        line.label.setVisible(False)
            else:
                try: self.plot.removeItem(self.v_line)
                except Exception: pass
        self._update_cursor_labels()

    def set_cursor2_enabled(self, enabled: bool) -> None:
        """Show/hide Cursor 2. When turned ON, always centres it in the current view."""
        self._cursor2_enabled = bool(enabled)
        cx = self._current_view_centre()
        try:
            if self._stacked_mode and self._stacked_plots:
                xr = self._stacked_plots[0].vb.viewRange()[0]
            else:
                xr = self.plot.plotItem.vb.viewRange()[0]
        except Exception:
            xr = [0.0, 10.0]
        span = max(abs(xr[1] - xr[0]) * 0.1, 0.5)
        cx2 = cx + span
        self.v_line2.setPos(cx2)
        if enabled:
            if self._stacked_mode:
                # Rebuild stacked rows with C2 lines (triggers _rebuild_curves)
                # If lines already exist, just show them
                if self._stacked_c2_lines:
                    last2 = len(self._stacked_c2_lines) - 1
                    for i, line in enumerate(self._stacked_c2_lines):
                        line.setPos(cx2)
                        line.setPen(pg.mkPen(color='#0000ff', width=1.5,
                                             style=Qt.PenStyle.DashLine))
                        if hasattr(line, 'label') and line.label is not None:
                            line.label.setVisible(i == last2)
                else:
                    # Rebuild to add C2 lines to all rows
                    self._rebuild_curves(preserve_selection=True)
            else:
                try: self.plot.addItem(self.v_line2, ignoreBounds=True)
                except Exception: pass
            self.table.setColumnHidden(3, False)
            if self._items:
                self._update_table_values(cx2, col=3)
        else:
            if self._stacked_mode:
                for line in self._stacked_c2_lines:
                    line.setPen(pg.mkPen(color='#00000000', width=0))
                    if hasattr(line, 'label') and line.label is not None:
                        line.label.setVisible(False)
            else:
                try: self.plot.removeItem(self.v_line2)
                except Exception: pass
            self.table.setColumnHidden(3, True)
            self.cursor2_label.hide()
        self._update_cursor_labels()

    def set_stacked(self, enabled: bool) -> None:
        """Toggle INCA/CANdb-style stacked layout (one row per signal, shared X)."""
        self._stacked_mode = bool(enabled)
        self._rebuild_curves(preserve_selection=True)
        self.fit_to_window()

    def add_series(self, key: str, series: SignalSeries, color: str | None = None) -> None:
        if key in self._items:
            return
        if not self._batch_mode:
            self._push_undo()
        color = color or next(self._color_cycle)
        self._items[key] = PlottedSignal(key=key, series=series, curve=None, color=color)
        if self._current_key is None:
            self._current_key = key
        if not self._batch_mode:
            self._rebuild_curves(preserve_selection=True)
            self._update_empty_state_ui()
            self.fit_to_window()

    def begin_batch_add(self) -> None:
        """Start a batch-add session. Suppresses per-signal rebuilds; call end_batch_add() when done."""
        self._push_undo()
        self._batch_mode = True

    def end_batch_add(self) -> None:
        """Finish a batch-add session. Triggers a single rebuild + fit for all queued signals."""
        self._batch_mode = False
        if self._items:
            self._rebuild_curves(preserve_selection=True)
            self._update_empty_state_ui()
            self.fit_to_window()

    # ── Internal: clear all rendered items ────────────────────────────────

    def _clear_rendered_items(self) -> None:
        # Disconnect resize hook before clearing
        try:
            self.plot.plotItem.vb.sigResized.disconnect(self._update_multi_axis_views)
        except Exception:
            pass

        try:
            self.plot.plotItem.clear()
        except Exception:
            pass

        for plotted in self._items.values():
            plotted.curve = None
            plotted.axis = None
            plotted.view_box = None

        for axis, vb in self._extra_axes:
            try:
                self.plot.plotItem.scene().removeItem(axis)
            except Exception:
                pass
            try:
                self.plot.plotItem.scene().removeItem(vb)
            except Exception:
                pass
        self._extra_axes.clear()

        try:
            if self._legend is not None:
                self.plot.plotItem.scene().removeItem(self._legend)
        except Exception:
            pass
        self._legend = self.plot.addLegend()

        self.plot.showAxis('right', False)
        self.plot.getAxis('right').setLabel('')
        self.plot.setLabel('left', 'Value')
        self.plot.getAxis('left').setWidth(55)

        # Save cursor positions before destroying the old InfiniteLines
        _saved_c1 = self.v_line.value() if hasattr(self, 'v_line') else 0.0
        _saved_c2 = self.v_line2.value() if (self._cursor2_enabled and hasattr(self, 'v_line2')) else 0.0

        # Recreate draggable cursor lines preserving movable=True
        self.v_line = pg.InfiniteLine(
            angle=90, movable=True,
            pen=pg.mkPen(color='#0000ff', width=1.5),
            label='C1', labelOpts={'color': '#0000ff', 'position': 0.95}
        )
        self.v_line.setPos(_saved_c1)   # restore position immediately
        self.h_line = pg.InfiniteLine(angle=0, movable=False,
                                      pen=pg.mkPen(color='#555', width=1))
        self.v_line.sigPositionChanged.connect(self._on_cursor1_moved)
        self.plot.addItem(self.v_line, ignoreBounds=True)
        self.plot.addItem(self.h_line, ignoreBounds=True)
        # Re-add cursor 2 if it was enabled
        if self._cursor2_enabled:
            try:
                self.v_line2.sigPositionChanged.disconnect()
            except Exception:
                pass
            self.v_line2 = pg.InfiniteLine(
                angle=90, movable=True,
                pen=pg.mkPen(color='#0000ff', width=1.5, style=Qt.PenStyle.DashLine),
                label='C2', labelOpts={'color': '#0000ff', 'position': 0.85}
            )
            self.v_line2.setPos(_saved_c2)  # restore position immediately
            self.v_line2.sigPositionChanged.connect(self._on_cursor2_moved)
            self.plot.addItem(self.v_line2, ignoreBounds=True)

        # Clear stacked items
        # Fix 2: disconnect stacked right-click before clearing scene
        try:
            self.glw.scene().sigMouseClicked.disconnect(self._on_stacked_scene_click)
        except Exception:
            pass
        self._stacked_vlines.clear()
        self._stacked_c1_lines.clear()  # per-row C1 lines (each row owns its instance)
        self._stacked_c2_lines.clear()  # per-row C2 lines
        self._stacked_plots.clear()
        try:
            self.glw.clear()
        except Exception:
            pass

    # ── Internal: rebuild ─────────────────────────────────────────────────

    def _rebuild_curves(self, preserve_selection: bool = False) -> None:
        selected = self.selected_keys() if preserve_selection else []

        # Save X view range and cursor positions before destroying rendered items
        _saved_xr: list | None = None
        if self._stacked_mode and self._stacked_plots:
            try:
                _saved_xr = list(self._stacked_plots[0].vb.viewRange()[0])
            except Exception:
                pass
        elif not self._stacked_mode:
            try:
                _saved_xr = list(self.plot.plotItem.vb.viewRange()[0])
            except Exception:
                pass

        self._clear_rendered_items()

        if not self._items:
            self._refresh_table()
            self._update_empty_state_ui()
            return

        if self._stacked_mode:
            self.view_stack.setCurrentIndex(1)
            self._rebuild_stacked()
            # Restore X range so the view doesn't jump when rows are added/removed
            if _saved_xr and self._stacked_plots:
                try:
                    self._stacked_plots[0].setXRange(_saved_xr[0], _saved_xr[1], padding=0)
                except Exception:
                    pass
        else:
            self.view_stack.setCurrentIndex(0)
            self._rebuild_overlay()
            # Restore X range for overlay mode
            if _saved_xr:
                try:
                    self.plot.setXRange(_saved_xr[0], _saved_xr[1], padding=0)
                except Exception:
                    pass

        self._set_axis_label(self._current_key)
        self._refresh_table()
        self._update_empty_state_ui()
        if selected:
            self._restore_selection(selected)
        self._refresh_highlight()
        self._setup_mouse_proxy()
        # Repopulate cursor value columns — _refresh_table wipes them to ''
        self._update_table_values(self.v_line.value(), col=2)
        if self._cursor2_enabled:
            self._update_table_values(self.v_line2.value(), col=3)

    def _rebuild_overlay(self) -> None:
        """Normal or multi-axis: all signals on one PlotWidget, extra axes to the left."""
        vis_idx = 0  # count only visible signals for multi-axis numbering
        for key in self._items:
            plotted = self._items[key]
            idx = vis_idx

            if not plotted.visible:
                plotted.curve = None
                continue
            vis_idx += 1

            if self._multi_axis and idx > 0:
                # Use PlotDataItem (not PlotCurveItem) so symbol kwargs work.
                # sigResized is connected below so geometry is maintained on resize.
                axis = pg.AxisItem('left')
                vb   = pg.ViewBox()
                self.plot.plotItem.scene().addItem(vb)
                self.plot.plotItem.scene().addItem(axis)
                axis.linkToView(vb)
                vb.setXLink(self.plot.plotItem.vb)
                curve = pg.PlotDataItem()
                self._configure_curve(curve)
                vb.addItem(curve)
                plotted.curve     = curve
                plotted.axis      = axis
                plotted.view_box  = vb
                self._extra_axes.append((axis, vb))
                # Restore axis visibility state (toggled via Axis column checkbox)
                axis.setVisible(plotted.axis_visible)
            else:
                curve = self.plot.plot([], [], name=key)
                self._configure_curve(curve)
                plotted.curve    = curve
                plotted.axis     = self.plot.getAxis('left')
                plotted.view_box = None
                # First signal uses main left axis — respect its axis_visible flag
                if self._multi_axis:
                    self.plot.getAxis('left').setVisible(plotted.axis_visible)

            self._apply_curve_style(plotted)
            try:
                self._legend.addItem(plotted.curve, key)
            except Exception:
                pass

        if self._extra_axes:
            # Reserve left-margin space for VISIBLE extra axes only.
            n_vis = sum(1 for axis, _ in self._extra_axes if axis.isVisible())
            total_left_w = self._MAIN_AXIS_W + n_vis * (self._EXTRA_AXIS_W + self._EXTRA_AXIS_GAP)
            self.plot.getAxis('left').setWidth(total_left_w)
            # Connect once; disconnect happens in _clear_rendered_items
            self.plot.plotItem.vb.sigResized.connect(self._update_multi_axis_views)
            # Defer initial geometry until widget is painted and layout has settled
            QTimer.singleShot(10, self._update_multi_axis_views)

    def _rebuild_stacked(self) -> None:
        """
        INCA/CANdb-style stacked layout.
        - Each signal in its own row, shared X axis.
        - Left axis label uses two-line HTML with pyqtgraph's -90° rotation:
            Line 1: unit  → rotates to RIGHT side → inner (between tick numbers and signal name)
            Line 2: name  → rotates to LEFT side  → outer (furthest from data)
        - Both lines are part of the same axis label, so no TextItem tracking needed.
        """
        # Only stack visible signals — invisible ones are completely hidden
        order = [k for k, p in self._items.items() if p.visible]
        n = len(order)
        ref_plot: pg.PlotItem | None = None

        for idx, key in enumerate(order):
            plotted = self._items[key]
            series  = plotted.series

            # Use custom axis: bottom-anchored, row-clipped label
            _left_ax = _StackedLeftAxis('left')
            p: pg.PlotItem = self.glw.addPlot(
                row=idx, col=0,
                axisItems={'left': _left_ax}
            )
            p.showGrid(x=True, y=True, alpha=0.25)
            p.setMenuEnabled(False)

            # Left axis: unit (inner) then signal name (outer), separated by <br>.
            # pyqtgraph rotates the label -90° (CCW), so line 1 → right (inner) and
            # line 2 → left (outer). This puts the unit between tick numbers and name.
            if series.unit:
                # Signal name first → outer (left after -90° rotation)
                # Unit second → inner (right after -90° rotation, between tick numbers and name)
                ylabel = (f'{series.signal_name}' +
                          f'<br><span style="font-size:80%">({series.unit})</span>')
            else:
                ylabel = series.signal_name
            # Fix 2: vertical-align middle via CSS on the axis label
            p.setLabel('left', ylabel, color=plotted.color)
            p.getAxis('left').setTextPen(pg.mkPen(plotted.color))
            p.getAxis('left').setWidth(85)
            p.showAxis('right', False)
            p.showAxis('top',   False)

            # X axis: tick labels only on the bottom row
            if idx < n - 1:
                p.getAxis('bottom').setStyle(showValues=False)
                p.getAxis('bottom').setLabel('')
                p.getAxis('bottom').setHeight(0)
            else:
                p.setLabel('bottom', 'Time (seconds)')

            # Link X axis to first row
            if ref_plot is None:
                ref_plot = p
            else:
                p.setXLink(ref_plot)

            curve = pg.PlotDataItem()
            self._configure_curve(curve)
            p.addItem(curve)
            plotted.curve    = curve
            plotted.axis     = p.getAxis('left')
            plotted.view_box = p.vb

            self._apply_curve_style(plotted)

            # Each row gets its own InfiniteLine instance.
            # A QGraphicsItem can only belong to ONE scene — sharing across
            # GLW rows causes crashes. Sync is done in _on_stacked_c1/c2_moved.
            _c1_label     = 'C1' if idx == n - 1 else ''
            _c1_labelOpts = {'color': '#0000ff', 'position': 0.95} if idx == n - 1 else {}
            c1 = pg.InfiniteLine(
                angle=90, movable=True,
                pen=pg.mkPen(color='#0000ff', width=1.5),
                label=_c1_label, labelOpts=_c1_labelOpts
            )
            c1.setPos(self.v_line.value())
            c1.sigPositionChanged.connect(self._on_stacked_c1_moved)
            # Respect current cursor1 toggle state on rebuild
            c1_enabled = getattr(self, '_cursor1_enabled', True)
            if not c1_enabled:
                c1.setPen(pg.mkPen(color='#00000000', width=0))
                if hasattr(c1, 'label') and c1.label is not None:
                    c1.label.setVisible(False)
            p.addItem(c1, ignoreBounds=True)
            self._stacked_c1_lines.append(c1)

            if self._cursor2_enabled:
                _c2_label     = 'C2' if idx == n - 1 else ''
                _c2_labelOpts = {'color': '#0000ff', 'position': 0.85} if idx == n - 1 else {}
                c2 = pg.InfiniteLine(
                    angle=90, movable=True,
                    pen=pg.mkPen(color='#0000ff', width=1.5,
                                 style=Qt.PenStyle.DashLine),
                    label=_c2_label, labelOpts=_c2_labelOpts
                )
                c2.setPos(self.v_line2.value())
                c2.sigPositionChanged.connect(self._on_stacked_c2_moved)
                p.addItem(c2, ignoreBounds=True)
                self._stacked_c2_lines.append(c2)

            self._stacked_plots.append(p)

    # ── Curve configuration & style ──────────────────────────────────────

    @staticmethod
    def _configure_curve(curve: pg.PlotDataItem) -> None:
        """One-time performance settings applied to every new PlotDataItem.

        clipToView  — pyqtgraph only sends samples inside the current
                      viewport to the renderer; the rest are never touched.
                      For a 150 MB file this keeps every redraw O(visible)
                      instead of O(total).
        autoDownsample / peak — when many samples map to the same pixel,
                      collapse them to min+max so fault spikes are never
                      missed even at high zoom-out levels.
        """
        curve.setClipToView(True)
        curve.setDownsampling(auto=True, method='peak')   # kwarg is 'method', not 'mode'

    def _apply_curve_style(self, plotted: PlottedSignal,
                           selected: bool = False,
                           set_data: bool = True) -> None:
        """Apply pen + symbol style.  Selected curves are drawn thicker.

        set_data=True  (default, called on initial add): pushes the full
            data buffer into pyqtgraph via setData().
        set_data=False (style toggle / highlight change): uses cheap
            per-property setters so the data arrays are never re-read.
            This is what makes show/hide points instant on large files.
        """
        if plotted.curve is None:
            return
        width = 5.0 if selected else 2.8
        pen   = pg.mkPen(color=plotted.color, width=width)

        if set_data:
            # np.asarray on array.array('d') is zero-copy via the buffer
            # protocol and returns a writable view — required by pyqtgraph.
            ts = np.asarray(plotted.series.timestamps, dtype=np.float64)
            vs = np.asarray(plotted.series.values,     dtype=np.float64)
            kwargs: dict = {'pen': pen, 'connect': 'finite'}
            if self._show_points:
                kwargs.update({
                    'symbol':     'o',
                    'symbolSize':  5,
                    'symbolBrush': plotted.color,
                    'symbolPen':   pg.mkPen(color=plotted.color, width=1.2),
                })
            else:
                kwargs['symbol'] = None
            plotted.curve.setData(ts, vs, **kwargs)
        else:
            # Style-only path: no data arrays touched.
            plotted.curve.setPen(pen)
            if self._show_points:
                plotted.curve.setSymbol('o')
                plotted.curve.setSymbolSize(5)
                plotted.curve.setSymbolBrush(pg.mkBrush(plotted.color))
                plotted.curve.setSymbolPen(pg.mkPen(color=plotted.color, width=1.2))
            else:
                plotted.curve.setSymbol(None)

    def _refresh_highlight(self) -> None:
        """Update curve thickness to reflect current table selection."""
        sel = set(self.selected_keys())
        for key, plotted in self._items.items():
            if plotted.curve is not None and plotted.visible:
                self._apply_curve_style(plotted, selected=(key in sel),
                                        set_data=False)

    # ── Multi-axis geometry (called via sigResized) ───────────────────────

    # Width constants for multi-axis layout
    _MAIN_AXIS_W: int = 55   # width of the main (first) left axis
    _EXTRA_AXIS_W: int = 55  # width of each additional floating axis
    _EXTRA_AXIS_GAP: int = 4 # gap between adjacent axis panels

    def _update_multi_axis_views(self) -> None:
        """
        Position floating ViewBoxes + axes to the LEFT of main plot.
        Only VISIBLE extra axes occupy layout slots — hidden axes are parked
        off-screen so their space is reclaimed by the viewport.
        Called on sigResized so geometry stays correct after window resize.
        """
        if not self._extra_axes:
            return
        rect = self.plot.plotItem.vb.sceneBoundingRect()
        if rect.width() < 10:                       # widget not yet painted
            QTimer.singleShot(20, self._update_multi_axis_views)
            return
        aw  = self._EXTRA_AXIS_W
        gap = self._EXTRA_AXIS_GAP
        mw  = self._MAIN_AXIS_W
        # Assign consecutive slots only to visible extra axes.
        # Hidden axes are placed off-screen (no layout space consumed).
        slot = 0
        for axis, vb in self._extra_axes:
            vb.setGeometry(rect)
            vb.linkedViewChanged(self.plot.plotItem.vb, vb.XAxis)
            if axis.isVisible():
                x = rect.left() - mw - gap - aw - slot * (aw + gap)
                axis.setGeometry(QRectF(x, rect.top(), aw, rect.height()))
                slot += 1
            else:
                # Park off-screen — invisible, no overlap with plot area
                axis.setGeometry(QRectF(-9999, rect.top(), 1, rect.height()))

    # ── Mouse proxy management ────────────────────────────────────────────

    def _setup_mouse_proxy(self) -> None:
        # Disconnect the previous scene first
        if self._proxy is not None:
            try:
                self._proxy.sigMouseMoved.disconnect(self._mouse_moved)
            except Exception:
                pass
            self._proxy = None

        scene = (
            self.glw.scene()
            if (self._stacked_mode and self._stacked_plots)
            else self.plot.scene()
        )
        # Direct connection — no SignalProxy throttle, so the h-line and cursor
        # updates fire immediately on every mouse-move instead of up to 16 ms late.
        scene.sigMouseMoved.connect(self._mouse_moved)
        self._proxy = scene   # keep ref so we can disconnect on next rebuild
        # Fix 2: connect right-click handler for stacked mode
        if self._stacked_mode and self._stacked_plots:
            try:
                self.glw.scene().sigMouseClicked.connect(self._on_stacked_scene_click)
            except Exception:
                pass

    # ── Cursor handlers ──────────────────────────────────────────────────────

    def _on_stacked_c1_moved(self) -> None:
        """Sync all stacked C1 lines when any one is dragged."""
        if not self._stacked_c1_lines:
            return
        # Find which line triggered (the one that moved)
        # Use the first line as the master position source since they're all
        # connected to this handler and we just want consistency
        x = self.sender().value() if self.sender() else self._stacked_c1_lines[0].value()
        self.v_line.setPos(x)   # keep normal-mode cursor in sync too
        self._syncing_c1 = True
        for line in self._stacked_c1_lines:
            if line is not self.sender():
                line.setPos(x)
        self._syncing_c1 = False
        self._update_table_values(x, col=2)
        self._update_cursor_labels()

    def _on_stacked_c2_moved(self) -> None:
        """Sync all stacked C2 lines when any one is dragged."""
        if not self._stacked_c2_lines:
            return
        x = self.sender().value() if self.sender() else self._stacked_c2_lines[0].value()
        self.v_line2.setPos(x)  # keep normal-mode cursor in sync too
        for line in self._stacked_c2_lines:
            if line is not self.sender():
                line.setPos(x)
        self._update_table_values(x, col=3)
        self._update_cursor_labels()

    def _on_cursor1_moved(self) -> None:
        """Called when Cursor 1 InfiniteLine is dragged."""
        x = self.v_line.value()
        self._update_table_values(x, col=2)
        self._update_cursor_labels()

    def _on_cursor2_moved(self) -> None:
        """Called when Cursor 2 InfiniteLine is dragged."""
        if not self._cursor2_enabled:
            return
        self._update_table_values(self.v_line2.value(), col=3)
        self._update_cursor_labels()

    def _update_cursor_labels(self) -> None:
        x1 = self.v_line.value()
        txt = f'C1: t={x1:.4f} s'
        if self._cursor2_enabled:
            x2   = self.v_line2.value()
            dt   = abs(x2 - x1)
            txt += f'   |   C2: t={x2:.4f} s   |   ΔT={dt:.4f} s'
            self.cursor2_label.setText(f'Time delta = {dt:.4f} s  (C1={x1:.4f} s  C2={x2:.4f} s)')
            self.cursor2_label.show()
        else:
            self.cursor2_label.hide()
        self.cursor_label.setText(txt)

    # ── Mouse cursor (hover tracking for h-line only) ─────────────────────

    def _mouse_moved(self, pos) -> None:
        """Track mouse for horizontal reference line only.
        Vertical position is now controlled by draggable InfiniteLines."""
        if not self._items:
            return
        if self._stacked_mode:
            return   # stacked rows handle cursors via sigPositionChanged
        if not self.plot.sceneBoundingRect().contains(pos):
            return
        mp = self.plot.plotItem.vb.mapSceneToView(pos)
        self.h_line.setPos(mp.y())

    def _update_table_values(self, x: float, col: int = 2) -> None:
        """Update Cursor 1 (col=2) or Cursor 2 (col=3) value column."""
        self.table.setUpdatesEnabled(False)
        try:
            for key, plotted in self._items.items():
                if not plotted.visible:
                    continue
                row = self._row_lookup.get(key)
                if row is None:
                    continue
                cell = self.table.item(row, col)
                if cell is None:
                    continue
                idx = self._nearest_index(plotted.series.timestamps, x)
                if idx is None:
                    continue
                value = plotted.series.display_value_at(idx)
                if isinstance(value, str):
                    cell.setText(value)
                else:
                    try:
                        cell.setText(f"{float(value):.3f}")
                    except (TypeError, ValueError):
                        cell.setText(str(value))
        finally:
            self.table.setUpdatesEnabled(True)

    # ── Fit to window ─────────────────────────────────────────────────────

    def fit_to_window(self) -> None:
        if not self._items:
            if not self._stacked_mode:
                self.plot.enableAutoRange()
                self.plot.autoRange()
            return

        vis = [p for p in self._items.values() if p.visible]
        all_ts = [ts for p in (vis or self._items.values()) for ts in p.series.timestamps]
        if not all_ts:
            return
        x_min, x_max = min(all_ts), max(all_ts)
        if x_min == x_max:
            x_max += 1.0

        if self._stacked_mode:
            order = [k for k, p in self._items.items() if p.visible]
            for i, key in enumerate(order):
                if i >= len(self._stacked_plots):
                    break
                plotted = self._items[key]
                p = self._stacked_plots[i]
                p.setXRange(x_min, x_max, padding=0.02)
                vals = [v for v in plotted.series.values if v == v]
                if vals:
                    y_min, y_max = min(vals), max(vals)
                    pad = (y_max - y_min) * 0.05 if y_min != y_max else (1.0 if y_min == 0 else abs(y_min) * 0.05)
                    p.setYRange(y_min - pad, y_max + pad, padding=0)
        elif self._multi_axis:
            self.plot.setXRange(x_min, x_max, padding=0.02)
            # Geometry must be current before setYRange on floating ViewBoxes
            self._update_multi_axis_views()
            for key, plotted in self._items.items():
                if not plotted.visible:
                    continue
                vals = [v for v in plotted.series.values if v == v]
                if not vals:
                    continue
                y_min, y_max = min(vals), max(vals)
                pad = (y_max - y_min) * 0.05 if y_min != y_max else (1.0 if y_min == 0 else abs(y_min) * 0.05)
                target = self.plot.plotItem.vb if plotted.view_box is None else plotted.view_box
                target.disableAutoRange()
                target.setYRange(y_min - pad, y_max + pad, padding=0)
        else:
            self.plot.setXRange(x_min, x_max, padding=0.02)
            numeric = [v for it in self._items.values() for v in it.series.values if v == v]
            if numeric:
                y_min, y_max = min(numeric), max(numeric)
                pad = (y_max - y_min) * 0.05 if y_min != y_max else (1.0 if y_min == 0 else abs(y_min) * 0.05)
                self.plot.setYRange(y_min - pad, y_max + pad, padding=0)

    def zoom_to_time(self, t_start: float, t_end: float, margin: float = 0.5) -> None:
        """Zoom X to [t_start - margin, t_end + margin] and rescale Y to match."""
        x0 = t_start - margin
        x1 = t_end + margin
        if x0 >= x1:
            x1 = x0 + 1.0
        if self._stacked_mode:
            for p in self._stacked_plots:
                p.setXRange(x0, x1, padding=0)
        elif self._multi_axis:
            self.plot.setXRange(x0, x1, padding=0)
            self._update_multi_axis_views()
        else:
            self.plot.setXRange(x0, x1, padding=0)
        self.fit_vertical()

    def fit_vertical(self) -> None:
        """
        Fit Y axis to the data currently visible in the X range.
        Does NOT change the X range — only rescales Y to match visible data.
        Works for normal, multi-axis, and stacked modes.
        """
        if not self._items:
            return

        def _y_range_for_visible(series, x_min: float, x_max: float):
            """Return (y_min, y_max, pad) for samples within [x_min, x_max]."""
            ts = series.timestamps
            vs = series.values
            vals = [
                float(v) for t, v in zip(ts, vs)
                if x_min <= float(t) <= x_max and v == v
            ]
            if not vals:
                return None
            y_min, y_max = min(vals), max(vals)
            pad = (y_max - y_min) * 0.05 if y_min != y_max else (
                1.0 if y_min == 0 else abs(y_min) * 0.05
            )
            return y_min - pad, y_max + pad

        if self._stacked_mode:
            order = [k for k, p in self._items.items() if p.visible]
            for i, key in enumerate(order):
                if i >= len(self._stacked_plots):
                    break
                plotted = self._items[key]
                p = self._stacked_plots[i]
                try:
                    xr = p.vb.viewRange()[0]
                except Exception:
                    continue
                result = _y_range_for_visible(plotted.series, xr[0], xr[1])
                if result:
                    p.setYRange(result[0], result[1], padding=0)

        elif self._multi_axis:
            self._update_multi_axis_views()  # ensure geometry is current
            for key, plotted in self._items.items():
                if not plotted.visible:
                    continue
                vb = self.plot.plotItem.vb if plotted.view_box is None else plotted.view_box
                try:
                    xr = vb.viewRange()[0]
                except Exception:
                    continue
                result = _y_range_for_visible(plotted.series, xr[0], xr[1])
                if result:
                    vb.disableAutoRange()
                    vb.setYRange(result[0], result[1], padding=0)

        else:
            # Normal mode: all signals share one Y axis
            try:
                xr = self.plot.plotItem.vb.viewRange()[0]
            except Exception:
                return
            all_vals = []
            for plotted in self._items.values():
                ts = plotted.series.timestamps
                vs = plotted.series.values
                all_vals += [
                    float(v) for t, v in zip(ts, vs)
                    if xr[0] <= float(t) <= xr[1] and v == v
                ]
            if all_vals:
                y_min, y_max = min(all_vals), max(all_vals)
                pad = (y_max - y_min) * 0.05 if y_min != y_max else (
                    1.0 if y_min == 0 else abs(y_min) * 0.05
                )
                self.plot.setYRange(y_min - pad, y_max + pad, padding=0)

    # ── Color / background ────────────────────────────────────────────────

    def set_series_color(self, key: str, color: str) -> None:
        if key not in self._items:
            return
        self._push_undo()
        self._items[key].color = color
        self._apply_curve_style(self._items[key])
        self._refresh_table()
        self._set_axis_label(self._current_key)
        self.signalColorChanged.emit(key, color)

    def series_colors(self) -> dict[str, str]:
        return {k: v.color for k, v in self._items.items()}

    def set_background_color(self, color: str) -> None:
        self._background_color = color
        self.plot.setBackground(color)
        self.glw.setBackground(color)
        self._apply_panel_background()
        self.backgroundColorChanged.emit(color)

    def background_color(self) -> str:
        return self._background_color

    # ── Overlay (empty state) ─────────────────────────────────────────────

    def set_status_overlay(self, state: str, next_step: str) -> None:
        if self._items:
            self.overlay_label.hide()
            return
        text = f"{state}\n{next_step}".strip()
        self.overlay_label.setText(text)
        self.overlay_label.setVisible(bool(text))

    def _update_empty_state_ui(self) -> None:
        has_items = bool(self._items)
        self.overlay_label.setVisible((not has_items) and bool(self.overlay_label.text()))
        self.drop_hint.setVisible(has_items)
        self.cursor_label.setVisible(has_items)
        if not has_items:
            self.cursor2_label.hide()
        if has_items and self._stacked_mode:
            self.view_stack.setCurrentIndex(1)
        else:
            self.view_stack.setCurrentIndex(0)

    # ── Axis label ────────────────────────────────────────────────────────

    def _set_axis_label(self, key: str | None) -> None:
        if self._stacked_mode:
            return   # each row already has its own label
        if not key or key not in self._items:
            self.plot.setLabel('left', 'Value')
            return
        series = self._items[key].series
        label  = series.signal_name + (f' ({series.unit})' if series.unit else '')
        self.plot.setLabel('left', label, color=self._items[key].color)
        self.plot.getAxis('left').setTextPen(pg.mkPen(self._items[key].color))
        if self._multi_axis:
            for axis_key in list(self._items.keys())[1:]:
                plotted = self._items[axis_key]
                if plotted.axis is not None and plotted.axis is not self.plot.getAxis('left'):
                    lbl = plotted.series.signal_name + (
                        f' ({plotted.series.unit})' if plotted.series.unit else ''
                    )
                    plotted.axis.setLabel(lbl, color=plotted.color)
                    plotted.axis.setTextPen(pg.mkPen(plotted.color))
                    plotted.axis.setWidth(55)

    # ── Table ─────────────────────────────────────────────────────────────

    def _format_signal_label(self, key: str) -> str:
        """
        Build the display label for a signal in the selected-signal table.
        Default: signal name only.  Channel and/or message name are prepended
        when the corresponding display flags are on.

        Key format: 'CH0::EEC1::EngSpeed'  →  parts[0]=CH0, [1]=EEC1, [2]=EngSpeed
        """
        parts = key.split('::')
        if len(parts) < 3:
            return key   # unexpected format — show as-is
        ch_part  = parts[0]       # e.g. CH0
        msg_part = parts[1]       # e.g. EEC1
        sig_part = '::'.join(parts[2:])  # signal name (may contain :: itself)
        label = sig_part
        if self._name_show_message:
            label = msg_part + '::' + label
        if self._name_show_channel:
            label = ch_part + '::' + label
        return label

    def _refresh_table(self) -> None:
        self.table.blockSignals(True)
        # Build ordered list: group headers interleaved with signal rows
        rows: list[tuple[str, str | None]] = []  # (type, key): type='group'|'signal'
        seen_groups: set[str] = set()
        for key, plotted in self._items.items():
            g = plotted.group
            if g and g not in seen_groups:
                rows.append(('group', g))
                seen_groups.add(g)
            rows.append(('signal', key))

        self.table.setRowCount(len(rows))
        for row_idx, (rtype, rval) in enumerate(rows):
            if rtype == 'group':
                self._set_group_header_row(row_idx, rval)
            else:
                key     = rval
                plotted = self._items[key]
                indent  = '  ' if plotted.group else ''
                chk_item = QTableWidgetItem()
                chk_item.setFlags(
                    Qt.ItemFlag.ItemIsUserCheckable |
                    Qt.ItemFlag.ItemIsEnabled |
                    Qt.ItemFlag.ItemIsSelectable
                )
                chk_item.setCheckState(
                    Qt.CheckState.Checked if plotted.visible
                    else Qt.CheckState.Unchecked
                )
                chk_item.setData(Qt.ItemDataRole.UserRole, key)
                sig_item = QTableWidgetItem(indent + self._format_signal_label(key))
                sig_item.setData(Qt.ItemDataRole.UserRole, key)
                c1_item  = QTableWidgetItem('')
                c2_item  = QTableWidgetItem('')
                un_item  = QTableWidgetItem(plotted.series.unit)
                brush = QBrush(QColor(
                    plotted.color if plotted.visible else '#606060'
                ))
                for item in (sig_item, c1_item, c2_item, un_item):
                    item.setForeground(brush)
                chk_item.setForeground(brush)
                # Col 5: Axis visibility checkbox (multi-axis mode only)
                ax_item = QTableWidgetItem()
                ax_item.setFlags(
                    Qt.ItemFlag.ItemIsUserCheckable |
                    Qt.ItemFlag.ItemIsEnabled |
                    Qt.ItemFlag.ItemIsSelectable
                )
                ax_item.setCheckState(
                    Qt.CheckState.Checked if plotted.axis_visible
                    else Qt.CheckState.Unchecked
                )
                ax_item.setData(Qt.ItemDataRole.UserRole, f'__axis__{key}')
                self.table.setItem(row_idx, 0, chk_item)
                self.table.setItem(row_idx, 1, sig_item)
                self.table.setItem(row_idx, 2, c1_item)
                self.table.setItem(row_idx, 3, c2_item)
                self.table.setItem(row_idx, 4, un_item)
                self.table.setItem(row_idx, 5, ax_item)
        self.table.blockSignals(False)
        self._apply_collapse_state()
        # Rebuild the key→row index used by _update_table_values on every cursor drag.
        self._row_lookup = {}
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 1)
            if item:
                key = str(item.data(Qt.ItemDataRole.UserRole))
                if not key.startswith('__'):
                    self._row_lookup[key] = row
        # widths are user-controlled — no resizeColumnsToContents

    def _emit_selection(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            self._refresh_highlight()
            return
        # Signal key lives in col 1; group header key lives in col 0
        item = self.table.item(row, 1) or self.table.item(row, 0)
        if not item or not item.isSelected():
            self._refresh_highlight()
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        # Skip group header rows
        if isinstance(key, str) and key.startswith('__'):
            return
        if key and key in self._items:
            self._current_key = str(key)
            self._set_axis_label(self._current_key)
            self.selectionChanged.emit(str(key))
        self._refresh_highlight()

    # ── Series management ─────────────────────────────────────────────────

    # ── Undo ──────────────────────────────────────────────────────────────

    def _push_undo(self) -> None:
        """
        Snapshot current plot state onto the undo stack.
        Snapshot captures: signal key order, per-signal color, current selection.
        SeriesSignal objects are NOT deep-copied (their data is immutable after load).
        """
        snapshot = {
            k: PlottedSignal(
                key=v.key, series=v.series,
                curve=None,          # curves are always rebuilt
                color=v.color,
                axis=None,
                view_box=None,
                visible=v.visible,
                group=v.group,
                axis_visible=v.axis_visible,
            )
            for k, v in self._items.items()
        }
        self._undo_stack.append((snapshot, self._current_key))
        if len(self._undo_stack) > self._UNDO_DEPTH:
            self._undo_stack.pop(0)

    def undo(self) -> None:
        """Restore the previous plot state (up to _UNDO_DEPTH levels)."""
        if not self._undo_stack:
            return
        snapshot, prev_key = self._undo_stack.pop()
        self._items = snapshot
        self._current_key = prev_key if prev_key in snapshot else next(iter(snapshot), None)
        self._rebuild_curves(preserve_selection=False)
        self._set_axis_label(self._current_key)
        self._update_empty_state_ui()
        if self._items:
            self.fit_to_window()

    def remove_series(self, key: str) -> None:
        if key not in self._items:
            return
        self._push_undo()
        self._items.pop(key)
        if self._current_key == key:
            self._current_key = next(iter(self._items), None)
        self._rebuild_curves(preserve_selection=False)
        self._set_axis_label(self._current_key)
        self._update_empty_state_ui()
        self.fit_to_window()

    def remove_selected_series(self) -> None:
        self._push_undo()
        for key in list(self.selected_keys()):
            self._items.pop(str(key), None)
        self._current_key = next(iter(self._items), None)
        self._rebuild_curves(preserve_selection=False)
        self._set_axis_label(self._current_key)
        self._update_empty_state_ui()
        self.fit_to_window()

    def clear_all(self) -> None:
        self._push_undo()
        self._items.clear()
        self._clear_rendered_items()
        self.table.setRowCount(0)
        self._current_key = None
        self.plot.setLabel('left', 'Value')
        self.cursor_label.setText(self._cursor_label_base)
        self.cursor2_label.hide()
        self._update_empty_state_ui()
        self.plot.enableAutoRange()
        self.plot.autoRange()

    def move_selected_up(self)   -> None: self._move_selected(-1)
    def move_selected_down(self) -> None: self._move_selected(1)

    def _move_selected(self, direction: int) -> None:
        keys  = self.selected_keys()
        if not keys:
            return
        self._push_undo()
        key_set = set(keys)
        order   = list(self._items.keys())
        if direction < 0:
            for key in keys:
                idx = order.index(key)
                if idx > 0 and order[idx - 1] not in key_set:
                    order[idx - 1], order[idx] = order[idx], order[idx - 1]
        else:
            for key in reversed(keys):
                idx = order.index(key)
                if idx < len(order) - 1 and order[idx + 1] not in key_set:
                    order[idx + 1], order[idx] = order[idx], order[idx + 1]
        self._items = {k: self._items[k] for k in order}

        if self._stacked_mode or self._multi_axis:
            # Stacked: row positions are visual — full rebuild required.
            # Multi-axis: axis numbering is index-based — full rebuild required.
            self._rebuild_curves(preserve_selection=True)
        else:
            # Overlay mode: curves are already rendered; only table order changes.
            # Skip the expensive curve teardown/rebuild and just refresh the table.
            self._refresh_table()
            self._restore_selection(keys)
            self._refresh_highlight()
            self._update_table_values(self.v_line.value(), col=2)
            if self._cursor2_enabled:
                self._update_table_values(self.v_line2.value(), col=3)

    def selected_keys(self) -> list[str]:
        keys: list[str] = []
        seen: set[str]  = set()
        for index in self.table.selectionModel().selectedRows():
            # Signal key is in col 1 (col 0 is checkbox / group arrow)
            item = self.table.item(index.row(), 1)
            if not item:
                continue
            key = item.data(Qt.ItemDataRole.UserRole)
            if key and str(key) not in seen and not str(key).startswith('__'):
                seen.add(str(key))
                keys.append(str(key))
        if not keys:
            row = self.table.currentRow()
            if row >= 0:
                item = self.table.item(row, 1)
                if item:
                    key = item.data(Qt.ItemDataRole.UserRole)
                    if key and not str(key).startswith('__'):
                        keys.append(str(key))
        return keys

    def plotted_series(self) -> list[SignalSeries]:
        return [item.series for item in self._items.values()]

    def plotted_keys(self) -> list[str]:
        return list(self._items.keys())

    def refresh_plotted_curves(self) -> None:
        """
        Re-read data from live SignalSeries references and update curves.
        Called periodically during decode so plots grow in real time.
        Thread-safe in CPython: array.array.append() is GIL-protected.
        """
        if not self._items:
            return
        for plotted in self._items.values():
            if plotted.curve is not None:
                self._apply_curve_style(plotted)
        # Auto-fit x range as data grows
        try:
            all_ts = [ts for it in self._items.values()
                      for ts in it.series.timestamps]
            if all_ts:
                x_max = max(all_ts)
                x_min = min(all_ts)
                if self._stacked_mode:
                    for p in self._stacked_plots:
                        p.setXRange(x_min, x_max, padding=0.02)
                else:
                    self.plot.setXRange(x_min, x_max, padding=0.02)
        except Exception:
            pass

    def table_column_widths(self) -> list[int]:
        """Return current widths of the 5 table columns (for config save)."""
        return [self.table.columnWidth(i) for i in range(5)]

    def set_table_column_widths(self, widths: list[int]) -> None:
        """Restore column widths saved in a configuration file."""
        for i, w in enumerate(widths):
            if i < 5 and isinstance(w, int) and w > 0:
                self.table.setColumnWidth(i, w)


    # ── Drag-and-drop onto the signal table ─────────────────────────────

    def _table_drag_enter(self, event) -> None:
        if event.mimeData().hasFormat(SignalTreeWidget.MIME_TYPE):
            event.acceptProposedAction()
        else:
            event.ignore()

    def _table_drop(self, event) -> None:
        if not event.mimeData().hasFormat(SignalTreeWidget.MIME_TYPE):
            event.ignore()
            return
        payload = bytes(event.mimeData().data(
            SignalTreeWidget.MIME_TYPE)).decode('utf-8')
        keys = [p.strip() for p in payload.splitlines() if p.strip()]
        if keys:
            self.signalDropped.emit(keys)
            event.acceptProposedAction()
        else:
            event.ignore()

    def _restore_selection(self, keys: list[str]) -> None:
        self.table.clearSelection()
        key_set = set(keys)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) in key_set:
                self.table.selectRow(row)

    # ── Context menus ─────────────────────────────────────────────────────

    _MENU_STYLE = """
        QMenu {
            background-color: #2b2b2b;
            color: #f0f0f0;
            border: 1px solid #555555;
        }
        QMenu::item {
            background-color: transparent;
            color: #f0f0f0;
            padding: 5px 24px 5px 12px;
        }
        QMenu::item:selected {
            background-color: #3d6a9e;
            color: #ffffff;
        }
        QMenu::item:disabled {
            color: #777777;
        }
        QMenu::separator {
            height: 1px;
            background: #555555;
            margin: 3px 0;
        }
    """

    def _make_menu(self, parent=None) -> QMenu:
        """Create a QMenu with a fixed dark style (Fix 3: immune to white background)."""
        menu = QMenu(parent or self)
        menu.setStyleSheet(self._MENU_STYLE)
        return menu

    # ── Group header rows ────────────────────────────────────────────────

    def _set_group_header_row(self, row_idx: int, group_name: str) -> None:
        """Render a collapsible group header row spanning all columns."""
        collapsed = self._collapsed_groups.get(group_name, False)
        arrow = '▶' if collapsed else '▼'

        # Group visibility state for checkbox (right side)
        keys_in_group = [k for k, p in self._items.items() if p.group == group_name]
        vis_states = [self._items[k].visible for k in keys_in_group]
        if all(vis_states):
            grp_cs = Qt.CheckState.Checked
        elif any(vis_states):
            grp_cs = Qt.CheckState.PartiallyChecked
        else:
            grp_cs = Qt.CheckState.Unchecked

        bg = QBrush(QColor('#1e2e40'))

        # Col 0: collapse arrow + group visibility checkbox
        arrow_item = QTableWidgetItem(arrow)
        arrow_item.setData(Qt.ItemDataRole.UserRole, f'__group__{group_name}')
        arrow_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        arrow_item.setFlags(
            Qt.ItemFlag.ItemIsEnabled |
            Qt.ItemFlag.ItemIsSelectable |
            Qt.ItemFlag.ItemIsUserCheckable
        )
        arrow_item.setCheckState(grp_cs)
        arrow_item.setBackground(bg)
        f = arrow_item.font()
        f.setBold(True)
        arrow_item.setFont(f)
        self.table.setItem(row_idx, 0, arrow_item)

        # Col 1: GROUP NAME in Signal column — bold, clearly visible
        name_item = QTableWidgetItem(f'  {group_name}')
        name_item.setData(Qt.ItemDataRole.UserRole, f'__group__{group_name}')
        name_item.setFlags(
            Qt.ItemFlag.ItemIsEnabled |
            Qt.ItemFlag.ItemIsSelectable |
            Qt.ItemFlag.ItemIsEditable   # allow double-click rename
        )
        name_item.setBackground(bg)
        f2 = name_item.font()
        f2.setBold(True)
        name_item.setFont(f2)
        name_item.setForeground(QBrush(QColor('#8ab8e0')))  # light blue
        self.table.setItem(row_idx, 1, name_item)

        # Cols 2-5: empty, same background
        for col in (2, 3, 4, 5):
            cell = QTableWidgetItem('')
            cell.setFlags(Qt.ItemFlag.ItemIsEnabled)
            cell.setBackground(bg)
            self.table.setItem(row_idx, col, cell)

    def _apply_collapse_state(self) -> None:
        """Show/hide signal rows according to _collapsed_groups state."""
        for row in range(self.table.rowCount()):
            item0 = self.table.item(row, 0)
            if not item0:
                continue
            key = item0.data(Qt.ItemDataRole.UserRole)
            if isinstance(key, str) and key.startswith('__group__'):
                self.table.setRowHidden(row, False)
            else:
                plotted = self._items.get(key)
                row_group = plotted.group if plotted else ''
                hidden = bool(row_group and self._collapsed_groups.get(row_group, False))
                self.table.setRowHidden(row, hidden)

    # ── Visibility toggle ─────────────────────────────────────────────────

    def _on_table_item_changed(self, item: QTableWidgetItem) -> None:
        """Handle checkbox changes in the signal table."""
        if not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable):
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(key, str):
            return

        # Col 5: Axis visibility toggle (multi-axis mode)
        if key.startswith('__axis__'):
            sig_key = key[len('__axis__'):]
            if sig_key in self._items:
                self._toggle_axis_visibility(
                    sig_key, item.checkState() == Qt.CheckState.Checked
                )
            return

        if key.startswith('__grpchk__'):
            # Legacy col-4 group checkbox (kept for safety)
            group_name = key[len('__grpchk__'):]
            checked    = item.checkState() == Qt.CheckState.Checked
            self._push_undo()
            for k, p in self._items.items():
                if p.group == group_name:
                    p.visible = checked
            self._apply_visibility()

        elif key.startswith('__group__'):
            # Col-0 group checkbox+arrow — handle visibility; flag suppresses collapse
            if not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                return
            group_name = key[len('__group__'):]
            checked    = item.checkState() == Qt.CheckState.Checked
            self._group_vis_changed = True
            self._push_undo()
            for k, p in self._items.items():
                if p.group == group_name:
                    p.visible = checked
            self._apply_visibility()

        elif key in self._items:
            # Individual signal checkbox
            checked = item.checkState() == Qt.CheckState.Checked
            if self._items[key].visible != checked:
                self._push_undo()
                self._items[key].visible = checked
                self._apply_visibility()

    def _apply_visibility(self) -> None:
        """
        Apply current .visible state to curves without a full rebuild.

        For normal / multi-axis mode: simply show/hide existing curves.
        For stacked mode: full rebuild is required (rows must be added/removed).
        The cursor lines are NOT touched — they persist regardless of signal visibility.
        """
        if self._stacked_mode:
            # Stacked needs full rebuild to add/remove rows
            self._rebuild_curves(preserve_selection=True)
        else:
            # Lightweight: just toggle curve visibility on existing PlotDataItems
            for key, plotted in self._items.items():
                if plotted.curve is not None:
                    plotted.curve.setVisible(plotted.visible)
            # Refresh table to update checkbox states and row colours
            self._refresh_table()
            self._refresh_highlight()
            # Repopulate cursor value columns — _refresh_table wipes them to ''
            self._update_table_values(self.v_line.value(), col=2)
            if self._cursor2_enabled:
                self._update_table_values(self.v_line2.value(), col=3)

    def _toggle_axis_visibility(self, key: str, visible: bool) -> None:
        """Show or hide the Y axis for a single signal (multi-axis mode only).

        Hiding an axis frees its layout slot so the plot area expands to fill
        the gap.  Showing it re-inserts it at the next available slot.
        """
        plotted = self._items.get(key)
        if plotted is None:
            return
        plotted.axis_visible = visible
        if plotted.axis is not None:
            plotted.axis.setVisible(visible)
        if self._multi_axis and self._extra_axes:
            # Resize the left margin to match the number of now-visible extra axes.
            # sigResized will fire once the layout settles and reposition everything;
            # the timer is a safety net for edge cases where sigResized doesn't fire.
            n_vis = sum(1 for axis, _ in self._extra_axes if axis.isVisible())
            new_w = self._MAIN_AXIS_W + n_vis * (self._EXTRA_AXIS_W + self._EXTRA_AXIS_GAP)
            self.plot.getAxis('left').setWidth(new_w)
            QTimer.singleShot(15, self._update_multi_axis_views)

    def _on_table_item_double_clicked(self, item: QTableWidgetItem) -> None:
        """Double-click on group header (col 0 arrow or col 1 name) → rename group."""
        key = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not isinstance(key, str) or not key.startswith('__group__'):
            return
        old_name = key[len('__group__'):]
        new_name, ok = QInputDialog.getText(
            self.parent(), 'Rename Group', 'Group name:', text=old_name
        )
        if not ok or not new_name.strip() or new_name.strip() == old_name:
            return
        new_name = new_name.strip()
        for p in self._items.values():
            if p.group == old_name:
                p.group = new_name
        if old_name in self._collapsed_groups:
            self._collapsed_groups[new_name] = self._collapsed_groups.pop(old_name)
        self._rebuild_curves(preserve_selection=True)

    def _on_table_cell_clicked(self, row: int, col: int) -> None:
        """Click on group header arrow col → toggle collapse.
        If the col-0 checkbox was what got clicked, itemChanged already fired
        and set _group_vis_changed — skip collapse in that case.
        """
        item0 = self.table.item(row, 0)
        if not item0:
            return
        key = item0.data(Qt.ItemDataRole.UserRole)
        if isinstance(key, str) and key.startswith('__group__'):
            if self._group_vis_changed:
                self._group_vis_changed = False  # consume the flag, skip collapse
                return
            group_name = key[len('__group__'):]
            self._collapsed_groups[group_name] = not self._collapsed_groups.get(group_name, False)
            self._apply_collapse_state()

    # ── Group operations (called from context menu) ───────────────────────

    def group_selected(self) -> None:
        """Group currently selected signals under a user-supplied name."""
        keys = [k for k in self.selected_keys() if k in self._items]
        if not keys:
            return
        name, ok = QInputDialog.getText(
            self.parent(), 'New Group', 'Group name:'
        )
        if not ok or not name.strip():
            return
        self._push_undo()
        for k in keys:
            self._items[k].group = name.strip()
        self._rebuild_curves(preserve_selection=True)

    def ungroup_selected(self, group_name: str) -> None:
        """Remove all signals from a group (they go back to ungrouped)."""
        self._push_undo()
        for p in self._items.values():
            if p.group == group_name:
                p.group = ''
        self._collapsed_groups.pop(group_name, None)
        self._rebuild_curves(preserve_selection=True)

    def _show_table_menu(self, position) -> None:
        selected_keys = self.selected_keys()
        row  = self.table.currentRow()
        item1 = self.table.item(row, 1) if row >= 0 else None
        item0 = self.table.item(row, 0) if row >= 0 else None
        item  = item1 or item0
        key   = item.data(Qt.ItemDataRole.UserRole) if item else None
        # Check if right-click is on a group header row
        if isinstance(key, str) and key.startswith('__group__'):
            group_name = key[len('__group__'):]
            self._show_group_header_menu(group_name, self.table.viewport().mapToGlobal(position))
            return
        if key and str(key) not in selected_keys and not str(key).startswith('__'):
            selected_keys = [str(key)]
        self._show_signal_menu(selected_keys, self.table.viewport().mapToGlobal(position))

    def _show_group_header_menu(self, group_name: str, global_pos) -> None:
        """Context menu shown when right-clicking a group header row."""
        menu = self._make_menu()
        rename_act = QAction(f'Rename "{group_name}"…', menu)
        rename_act.triggered.connect(lambda: self._rename_group_dialog(group_name))
        menu.addAction(rename_act)
        ungroup_act = QAction('Ungroup', menu)
        ungroup_act.triggered.connect(lambda: self.ungroup_selected(group_name))
        menu.addAction(ungroup_act)
        menu.exec(global_pos)

    def _rename_group_dialog(self, old_name: str) -> None:
        new_name, ok = QInputDialog.getText(
            self.parent(), 'Rename Group', 'Group name:', text=old_name
        )
        if not ok or not new_name.strip() or new_name.strip() == old_name:
            return
        new_name = new_name.strip()
        for p in self._items.values():
            if p.group == old_name:
                p.group = new_name
        if old_name in self._collapsed_groups:
            self._collapsed_groups[new_name] = self._collapsed_groups.pop(old_name)
        self._rebuild_curves(preserve_selection=True)

    def _show_signal_menu(self, selected_keys: list, global_pos) -> None:
        """Shared signal context menu used by table and stacked plot right-click."""
        menu = self._make_menu()
        has_sel   = bool(selected_keys)
        has_one   = len(selected_keys) == 1
        has_items = bool(self._items)   # any signal plotted at all

        # Change color — only when exactly one signal selected
        act_color = QAction('Change signal color...', menu)
        if has_one:
            act_color.triggered.connect(lambda: self._choose_color_for_key(str(selected_keys[0])))
        act_color.setEnabled(has_one)
        menu.addAction(act_color)
        menu.addSeparator()

        for label, slot in [
            ('Move selected up',   self.move_selected_up),
            ('Move selected down', self.move_selected_down),
        ]:
            a = QAction(label, menu)
            a.triggered.connect(slot)
            a.setEnabled(has_sel)
            menu.addAction(a)
        menu.addSeparator()
        rm_label = ('Remove selected signals' if len(selected_keys) > 1
                    else 'Remove selected signal')
        rm = QAction(rm_label, menu)
        rm.triggered.connect(self.remove_selected_series)
        rm.setEnabled(has_sel)
        menu.addAction(rm)
        menu.addSeparator()
        grp_act = QAction('Group selected…', menu)
        grp_act.triggered.connect(self.group_selected)
        grp_act.setEnabled(has_sel)
        menu.addAction(grp_act)
        # Signal display sub-menu
        menu.addSeparator()
        disp_menu = self._make_menu(menu)
        disp_menu.setTitle('Signal name display')
        ch_act = QAction('Show channel', disp_menu, checkable=True)
        ch_act.setChecked(self._name_show_channel)
        ch_act.triggered.connect(self._toggle_name_channel)
        disp_menu.addAction(ch_act)
        msg_act = QAction('Show message', disp_menu, checkable=True)
        msg_act.setChecked(self._name_show_message)
        msg_act.triggered.connect(self._toggle_name_message)
        disp_menu.addAction(msg_act)
        menu.addMenu(disp_menu)
        # Set plot background accessible from table right-click
        menu.addSeparator()
        bg_act = QAction('Set plot background color...', menu)
        bg_act.triggered.connect(self._choose_plot_background_color)
        menu.addAction(bg_act)
        menu.exec(global_pos)

    def _toggle_name_channel(self, checked: bool) -> None:
        self._name_show_channel = bool(checked)
        self._refresh_table()

    def _toggle_name_message(self, checked: bool) -> None:
        self._name_show_message = bool(checked)
        self._refresh_table()

    def _on_stacked_scene_click(self, event) -> None:
        """Left-click → clear selection. Right-click → signal context menu."""
        try:
            btn = event.button()
        except Exception:
            return
        if btn == Qt.MouseButton.LeftButton:
            self.table.blockSignals(True)
            self.table.clearSelection()
            self.table.setCurrentCell(-1, -1)
            self.table.blockSignals(False)
            self._refresh_highlight()
            return
        if btn != Qt.MouseButton.RightButton:
            return
        pos = event.scenePos()
        keys = list(self._items.keys())
        for i, p in enumerate(self._stacked_plots):
            if p.sceneBoundingRect().contains(pos):
                if i < len(keys):
                    key = keys[i]
                    self._restore_selection([key])
                    # QCursor.pos() is always reliable for screen coordinates
                    from PySide6.QtGui import QCursor
                    self._show_signal_menu([key], QCursor.pos())
                    try:
                        event.accept()
                    except Exception:
                        pass
                return

    def _install_plot_click_deselect(self) -> None:
        """Left-click anywhere in the plot area clears selection and resets highlight."""
        try:
            vb = self.plot.plotItem.vb
            vb.scene().sigMouseClicked.connect(self._on_plot_area_click)
        except Exception:
            pass

    def _on_plot_area_click(self, event) -> None:
        """Left-click in any non-stacked plot area → clear selection and reset thickness."""
        try:
            if event.button() != Qt.MouseButton.LeftButton:
                return
        except Exception:
            return
        try:
            vb = self.plot.plotItem.vb
            if not vb.sceneBoundingRect().contains(event.scenePos()):
                return
        except Exception:
            pass
        # Block itemSelectionChanged so _emit_selection can't re-highlight
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.blockSignals(False)
        self._refresh_highlight()

    def _install_plot_background_menu(self) -> None:
        pi   = getattr(self.plot, 'plotItem', None)
        menu = getattr(pi, 'ctrlMenu', None)
        if menu is not None:
            menu.addSeparator()
            act = QAction('Set Plot Background Color...', menu)
            act.triggered.connect(self._choose_plot_background_color)
            menu.addAction(act)
        else:
            self.plot.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self.plot.customContextMenuRequested.connect(self._show_plot_menu)

    def _choose_color_for_key(self, key: str) -> None:
        if key not in self._items:
            return
        chosen = QColorDialog.getColor(QColor(self._items[key].color), self, 'Choose signal color')
        if chosen.isValid():
            self.set_series_color(key, chosen.name())

    def _choose_plot_background_color(self) -> None:
        chosen = QColorDialog.getColor(QColor(self._background_color), self, 'Choose plot background color')
        if chosen.isValid():
            self.set_background_color(chosen.name())

    def _show_plot_menu(self, position) -> None:
        menu = self._make_menu()
        act  = QAction('Set Plot Background Color...', menu)
        act.triggered.connect(self._choose_plot_background_color)
        menu.addAction(act)
        menu.exec(self.plot.mapToGlobal(position))

    def _apply_panel_background(self) -> None:
        bg = self._background_color
        # Fix 3: header always grey/black — immune to plot background colour changes
        self.table_panel.setStyleSheet(f'''
            QWidget {{ background-color: {bg}; }}
            QLabel  {{ background-color: {bg}; color: white; }}
            QTableWidget {{
                background-color: {bg};
                alternate-background-color: {bg};
                gridline-color: #444444;
                color: white;
                selection-background-color: #2d4f7c;
            }}
            QHeaderView::section {{
                background-color: #4a4a4a;
                color: #000000;
                border: 1px solid #333333;
                font-weight: bold;
                padding: 3px;
            }}
        ''')

    # ── Nearest-sample lookup ─────────────────────────────────────────────

    @staticmethod
    def _nearest_index(values, target: float) -> int | None:
        n = len(values)
        if n == 0:
            return None
        # Zero-copy view; searchsorted runs in C (O(log n), no Python loop).
        arr = np.frombuffer(values, dtype=np.float64)
        idx = int(np.searchsorted(arr, target, side='left'))
        if idx >= n:
            return n - 1
        if idx == 0:
            return 0
        return idx if (arr[idx] - target) <= (target - arr[idx - 1]) else idx - 1
