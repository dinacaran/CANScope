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
        self._proxy = None
        self._color_cycle = cycle([
            '#e41a1c', '#377eb8', '#4daf4a', '#984ea3', '#ff7f00',
            '#a65628', '#f781bf', '#17becf', '#bcbd22', '#1f77b4',
        ])
        self._background_color = '#000000'
        self._cursor1_enabled: bool = True   # mirrors button default
        self._cursor2_enabled: bool = False
        self.setAcceptDrops(True)

        # ── Normal / multi-axis plot ──────────────────────────────────────
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self._legend = self.plot.addLegend()
        self.plot.setLabel('bottom', 'Time (seconds)')
        self.plot.setBackground(self._background_color)
        self._install_plot_background_menu()

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
            pen=pg.mkPen(color='#f0e040', width=1.5),
            label='C1', labelOpts={'color': '#f0e040', 'position': 0.95}
        )
        self.h_line = pg.InfiniteLine(angle=0, movable=False,
                                      pen=pg.mkPen(color='#555', width=1))
        self.v_line.sigPositionChanged.connect(self._on_cursor1_moved)
        self.plot.addItem(self.v_line, ignoreBounds=True)
        self.plot.addItem(self.h_line, ignoreBounds=True)

        # ── Cursor 2: draggable, off by default ──────────────────────────
        self.v_line2 = pg.InfiniteLine(
            angle=90, movable=True,
            pen=pg.mkPen(color='#40c0f0', width=1.5, style=Qt.PenStyle.DashLine),
            label='C2', labelOpts={'color': '#40c0f0', 'position': 0.85}
        )
        self.v_line2.sigPositionChanged.connect(self._on_cursor2_moved)
        # v_line2 not added to plot until cursor 2 is enabled

        # ── Signal table ──────────────────────────────────────────────────
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ['Signal', 'Cursor 1', 'Cursor 2', 'Unit'])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.itemSelectionChanged.connect(self._emit_selection)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_table_menu)
        self.table.verticalHeader().setVisible(False)
        hdr = self.table.horizontalHeader()
        hdr.setStretchLastSection(False)
        hdr.setStretchLastSection(False)
        # Fix 1: all three columns user-resizable
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 200)   # Signal
        self.table.setColumnWidth(1, 90)    # Cursor 1
        self.table.setColumnWidth(2, 90)    # Cursor 2
        self.table.setColumnWidth(3, 55)    # Unit
        self.table.setColumnHidden(2, True) # hidden until cursor 2 enabled

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
        # Bug 2 fix: applies to ALL plotted signals (PlotDataItem supports symbols)
        for plotted in self._items.values():
            self._apply_curve_style(plotted)

    def set_multi_axis(self, enabled: bool) -> None:
        self._multi_axis = bool(enabled)
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
                # Stacked: show/move per-row lines (already in plots from _rebuild_stacked)
                for line in self._stacked_c1_lines:
                    line.setPos(cx)
                    line.setPen(pg.mkPen(color='#f0e040', width=1.5))
                    if hasattr(line, 'label') and line.label is not None:
                        line.label.setVisible(True)
            else:
                # Normal/multi-axis: v_line lives in self.plot only
                try: self.plot.addItem(self.v_line, ignoreBounds=True)
                except Exception: pass
            if self._items:
                self._update_table_values(cx, col=1)
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
                    for line in self._stacked_c2_lines:
                        line.setPos(cx2)
                        line.setPen(pg.mkPen(color='#40c0f0', width=1.5,
                                             style=Qt.PenStyle.DashLine))
                        if hasattr(line, 'label') and line.label is not None:
                            line.label.setVisible(True)
                else:
                    # Rebuild to add C2 lines to all rows
                    self._rebuild_curves(preserve_selection=True)
            else:
                try: self.plot.addItem(self.v_line2, ignoreBounds=True)
                except Exception: pass
            self.table.setColumnHidden(2, False)
            if self._items:
                self._update_table_values(cx2, col=2)
        else:
            if self._stacked_mode:
                for line in self._stacked_c2_lines:
                    line.setPen(pg.mkPen(color='#00000000', width=0))
                    if hasattr(line, 'label') and line.label is not None:
                        line.label.setVisible(False)
            else:
                try: self.plot.removeItem(self.v_line2)
                except Exception: pass
            self.table.setColumnHidden(2, True)
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
        color = color or next(self._color_cycle)
        self._items[key] = PlottedSignal(key=key, series=series, curve=None, color=color)
        if self._current_key is None:
            self._current_key = key
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

        # Recreate draggable cursor lines preserving movable=True
        self.v_line = pg.InfiniteLine(
            angle=90, movable=True,
            pen=pg.mkPen(color='#f0e040', width=1.5),
            label='C1', labelOpts={'color': '#f0e040', 'position': 0.95}
        )
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
                pen=pg.mkPen(color='#40c0f0', width=1.5, style=Qt.PenStyle.DashLine),
                label='C2', labelOpts={'color': '#40c0f0', 'position': 0.85}
            )
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
        self._clear_rendered_items()

        if not self._items:
            self._refresh_table()
            self._update_empty_state_ui()
            return

        if self._stacked_mode:
            self.view_stack.setCurrentIndex(1)
            self._rebuild_stacked()
        else:
            self.view_stack.setCurrentIndex(0)
            self._rebuild_overlay()

        self._set_axis_label(self._current_key)
        self._refresh_table()
        self._update_empty_state_ui()
        if selected:
            self._restore_selection(selected)
        self._setup_mouse_proxy()

    def _rebuild_overlay(self) -> None:
        """Normal or multi-axis: all signals on one PlotWidget, extra axes to the left."""
        for idx, key in enumerate(self._items):
            plotted = self._items[key]

            if self._multi_axis and idx > 0:
                # ── BUG 1 FIX ──
                # Use PlotDataItem (not PlotCurveItem) so symbol kwargs work.
                # sigResized is connected below so geometry is maintained on resize.
                axis = pg.AxisItem('left')
                vb   = pg.ViewBox()
                self.plot.plotItem.scene().addItem(vb)
                self.plot.plotItem.scene().addItem(axis)
                axis.linkToView(vb)
                vb.setXLink(self.plot.plotItem.vb)
                curve = pg.PlotDataItem()       # <-- was PlotCurveItem
                vb.addItem(curve)
                plotted.curve     = curve
                plotted.axis      = axis
                plotted.view_box  = vb
                self._extra_axes.append((axis, vb))
            else:
                curve = self.plot.plot([], [], name=key)
                plotted.curve    = curve
                plotted.axis     = self.plot.getAxis('left')
                plotted.view_box = None

            self._apply_curve_style(plotted)
            try:
                self._legend.addItem(plotted.curve, key)
            except Exception:
                pass

        if self._extra_axes:
            # Connect once; disconnect happens in _clear_rendered_items
            self.plot.plotItem.vb.sigResized.connect(self._update_multi_axis_views)
            # Defer initial geometry until widget is painted
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
        order = list(self._items.keys())
        n = len(order)
        ref_plot: pg.PlotItem | None = None

        for idx, key in enumerate(order):
            plotted = self._items[key]
            series  = plotted.series

            p: pg.PlotItem = self.glw.addPlot(row=idx, col=0)
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
            p.setLabel('left', ylabel, color=plotted.color,
                        **{'vertical-align': 'middle'})
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
            p.addItem(curve)
            plotted.curve    = curve
            plotted.axis     = p.getAxis('left')
            plotted.view_box = p.vb

            self._apply_curve_style(plotted)

            # Each row gets its own InfiniteLine instance.
            # A QGraphicsItem can only belong to ONE scene — sharing across
            # GLW rows causes crashes. Sync is done in _on_stacked_c1/c2_moved.
            c1 = pg.InfiniteLine(
                angle=90, movable=True,
                pen=pg.mkPen(color='#f0e040', width=1.5),
                label='C1', labelOpts={'color': '#f0e040', 'position': 0.95}
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
                c2 = pg.InfiniteLine(
                    angle=90, movable=True,
                    pen=pg.mkPen(color='#40c0f0', width=1.5,
                                 style=Qt.PenStyle.DashLine),
                    label='C2', labelOpts={'color': '#40c0f0', 'position': 0.85}
                )
                c2.setPos(self.v_line2.value())
                c2.sigPositionChanged.connect(self._on_stacked_c2_moved)
                p.addItem(c2, ignoreBounds=True)
                self._stacked_c2_lines.append(c2)

            self._stacked_plots.append(p)

    # ── Curve style ───────────────────────────────────────────────────────

    def _apply_curve_style(self, plotted: PlottedSignal) -> None:
        """
        Bug 2 fix: PlotDataItem.setData accepts all kwargs including symbol.
        This works identically for the main plot, extra ViewBoxes, and stacked rows.
        """
        if plotted.curve is None:
            return
        ts = np.asarray(plotted.series.timestamps, dtype=np.float64)
        vs = np.asarray(plotted.series.values,     dtype=np.float64)
        kwargs: dict = {
            'pen':     pg.mkPen(color=plotted.color, width=2.8),
            'connect': 'finite',
        }
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

    # ── Multi-axis geometry (called via sigResized) ───────────────────────

    def _update_multi_axis_views(self) -> None:
        """
        Bug 1 fix: position floating ViewBoxes + axes to the LEFT of main plot.
        Called on sigResized so geometry stays correct after window resize.
        """
        if not self._extra_axes:
            return
        rect = self.plot.plotItem.vb.sceneBoundingRect()
        if rect.width() < 10:                       # widget not yet painted
            QTimer.singleShot(20, self._update_multi_axis_views)
            return
        axis_width = 55
        for idx, (axis, vb) in enumerate(self._extra_axes, start=1):
            vb.setGeometry(rect)
            vb.linkedViewChanged(self.plot.plotItem.vb, vb.XAxis)
            x = rect.left() - axis_width * idx
            axis.setGeometry(QRectF(x, rect.top(), axis_width, rect.height()))

    # ── Mouse proxy management ────────────────────────────────────────────

    def _setup_mouse_proxy(self) -> None:
        if self._proxy is not None:
            try:
                self._proxy.disconnect()
            except Exception:
                pass
            self._proxy = None

        scene = (
            self.glw.scene()
            if (self._stacked_mode and self._stacked_plots)
            else self.plot.scene()
        )
        self._proxy = pg.SignalProxy(
            scene.sigMouseMoved, rateLimit=60, slot=self._mouse_moved
        )
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
        self._update_table_values(x, col=1)
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
        self._update_table_values(x, col=2)
        self._update_cursor_labels()

    def _on_cursor1_moved(self) -> None:
        """Called when Cursor 1 InfiniteLine is dragged."""
        x = self.v_line.value()
        self._update_table_values(x, col=1)
        self._update_cursor_labels()

    def _on_cursor2_moved(self) -> None:
        """Called when Cursor 2 InfiniteLine is dragged."""
        if not self._cursor2_enabled:
            return
        self._update_table_values(self.v_line2.value(), col=2)
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

    def _mouse_moved(self, event: tuple) -> None:
        """Track mouse for horizontal reference line only.
        Vertical position is now controlled by draggable InfiniteLines."""
        if not self._items:
            return
        pos = event[0]
        if self._stacked_mode:
            return   # stacked rows handle cursors via sigPositionChanged
        if not self.plot.sceneBoundingRect().contains(pos):
            return
        mp = self.plot.plotItem.vb.mapSceneToView(pos)
        self.h_line.setPos(mp.y())

    def _update_table_values(self, x: float, col: int = 1) -> None:
        """Update Cursor 1 (col=1) or Cursor 2 (col=2) value column."""
        row_lookup: dict[str, int] = {}
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                row_lookup[str(item.data(Qt.ItemDataRole.UserRole))] = row
        for key, plotted in self._items.items():
            idx = self._nearest_index(plotted.series.timestamps, x)
            if idx is None:
                continue
            value = plotted.series.raw_values[idx]
            row   = row_lookup.get(key)
            if row is not None:
                cell = self.table.item(row, col)
                if cell is not None:
                    if isinstance(value, str):
                        cell.setText(value)
                    else:
                        try:
                            cell.setText(f"{float(value):.3f}")
                        except (TypeError, ValueError):
                            cell.setText(str(value))

    # ── Fit to window ─────────────────────────────────────────────────────

    def fit_to_window(self) -> None:
        if not self._items:
            if not self._stacked_mode:
                self.plot.enableAutoRange()
                self.plot.autoRange()
            return

        all_ts = [ts for it in self._items.values() for ts in it.series.timestamps]
        if not all_ts:
            return
        x_min, x_max = min(all_ts), max(all_ts)
        if x_min == x_max:
            x_max += 1.0

        if self._stacked_mode:
            for i, (key, plotted) in enumerate(self._items.items()):
                if i >= len(self._stacked_plots):
                    break
                p = self._stacked_plots[i]
                p.setXRange(x_min, x_max, padding=0.02)
                vals = [v for v in plotted.series.values if v == v]
                if vals:
                    y_min, y_max = min(vals), max(vals)
                    pad = (y_max - y_min) * 0.05 if y_min != y_max else (1.0 if y_min == 0 else abs(y_min) * 0.05)
                    p.setYRange(y_min - pad, y_max + pad, padding=0)
        elif self._multi_axis:
            self.plot.setXRange(x_min, x_max, padding=0.02)
            for idx, (key, plotted) in enumerate(self._items.items()):
                vals = [v for v in plotted.series.values if v == v]
                if not vals:
                    continue
                y_min, y_max = min(vals), max(vals)
                pad = (y_max - y_min) * 0.05 if y_min != y_max else (1.0 if y_min == 0 else abs(y_min) * 0.05)
                target = self.plot.plotItem.vb if (idx == 0 or plotted.view_box is None) else plotted.view_box
                target.setYRange(y_min - pad, y_max + pad, padding=0)
            self._update_multi_axis_views()
        else:
            self.plot.setXRange(x_min, x_max, padding=0.02)
            numeric = [v for it in self._items.values() for v in it.series.values if v == v]
            if numeric:
                y_min, y_max = min(numeric), max(numeric)
                pad = (y_max - y_min) * 0.05 if y_min != y_max else (1.0 if y_min == 0 else abs(y_min) * 0.05)
                self.plot.setYRange(y_min - pad, y_max + pad, padding=0)

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
            for i, (key, plotted) in enumerate(self._items.items()):
                if i >= len(self._stacked_plots):
                    break
                p = self._stacked_plots[i]
                try:
                    xr = p.vb.viewRange()[0]
                except Exception:
                    continue
                result = _y_range_for_visible(plotted.series, xr[0], xr[1])
                if result:
                    p.setYRange(result[0], result[1], padding=0)

        elif self._multi_axis:
            for idx, (key, plotted) in enumerate(self._items.items()):
                vb = self.plot.plotItem.vb if (idx == 0 or plotted.view_box is None)                      else plotted.view_box
                try:
                    xr = vb.viewRange()[0]
                except Exception:
                    continue
                result = _y_range_for_visible(plotted.series, xr[0], xr[1])
                if result:
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

    def _refresh_table(self) -> None:
        self.table.setRowCount(len(self._items))
        for row, (key, plotted) in enumerate(self._items.items()):
            signal_item  = QTableWidgetItem(key)
            signal_item.setData(Qt.ItemDataRole.UserRole, key)
            cursor1_item = QTableWidgetItem('')
            cursor2_item = QTableWidgetItem('')
            unit_item    = QTableWidgetItem(plotted.series.unit)
            brush = QBrush(QColor(plotted.color))
            for item in (signal_item, cursor1_item, cursor2_item, unit_item):
                item.setForeground(brush)
            self.table.setItem(row, 0, signal_item)
            self.table.setItem(row, 1, cursor1_item)
            self.table.setItem(row, 2, cursor2_item)
            self.table.setItem(row, 3, unit_item)
        # widths are user-controlled — no resizeColumnsToContents

    def _emit_selection(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        item = self.table.item(row, 0)
        if not item:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        if key:
            self._current_key = str(key)
            self._set_axis_label(self._current_key)
            self.selectionChanged.emit(str(key))

    # ── Series management ─────────────────────────────────────────────────

    def remove_series(self, key: str) -> None:
        if key not in self._items:
            return
        self._items.pop(key)
        if self._current_key == key:
            self._current_key = next(iter(self._items), None)
        self._rebuild_curves(preserve_selection=False)
        self._set_axis_label(self._current_key)
        self._update_empty_state_ui()
        self.fit_to_window()

    def remove_selected_series(self) -> None:
        for key in list(self.selected_keys()):
            self._items.pop(str(key), None)
        self._current_key = next(iter(self._items), None)
        self._rebuild_curves(preserve_selection=False)
        self._set_axis_label(self._current_key)
        self._update_empty_state_ui()
        self.fit_to_window()

    def clear_all(self) -> None:
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
        order = list(self._items.keys())
        if direction < 0:
            for key in keys:
                idx = order.index(key)
                if idx > 0 and order[idx - 1] not in keys:
                    order[idx - 1], order[idx] = order[idx], order[idx - 1]
        else:
            for key in reversed(keys):
                idx = order.index(key)
                if idx < len(order) - 1 and order[idx + 1] not in keys:
                    order[idx + 1], order[idx] = order[idx], order[idx + 1]
        self._items = {k: self._items[k] for k in order}
        self._rebuild_curves(preserve_selection=True)
        self._restore_selection(keys)

    def selected_keys(self) -> list[str]:
        keys: list[str] = []
        seen: set[str]  = set()
        for index in self.table.selectionModel().selectedRows():
            item = self.table.item(index.row(), 0)
            if not item:
                continue
            key = item.data(Qt.ItemDataRole.UserRole)
            if key and str(key) not in seen:
                seen.add(str(key))
                keys.append(str(key))
        if not keys:
            row = self.table.currentRow()
            if row >= 0:
                item = self.table.item(row, 0)
                if item:
                    key = item.data(Qt.ItemDataRole.UserRole)
                    if key:
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
        """Return current widths of the 4 table columns (for config save)."""
        return [self.table.columnWidth(i) for i in range(4)]

    def set_table_column_widths(self, widths: list[int]) -> None:
        """Restore column widths saved in a configuration file."""
        for i, w in enumerate(widths):
            if i < 4 and isinstance(w, int) and w > 0:
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

    def _show_table_menu(self, position) -> None:
        selected_keys = self.selected_keys()
        row  = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        key  = item.data(Qt.ItemDataRole.UserRole) if item else None
        if key and str(key) not in selected_keys:
            selected_keys = [str(key)]
        if not selected_keys:
            return
        self._show_signal_menu(selected_keys, self.table.viewport().mapToGlobal(position))

    def _show_signal_menu(self, selected_keys: list, global_pos) -> None:
        """Shared signal context menu used by table and stacked plot right-click."""
        menu = self._make_menu()
        if len(selected_keys) == 1:
            act = QAction('Change signal color...', menu)
            act.triggered.connect(lambda: self._choose_color_for_key(str(selected_keys[0])))
            menu.addAction(act)
            menu.addSeparator()
        for label, slot in [
            ('Move selected up',   self.move_selected_up),
            ('Move selected down', self.move_selected_down),
        ]:
            a = QAction(label, menu)
            a.triggered.connect(slot)
            menu.addAction(a)
        menu.addSeparator()
        rm_label = ('Remove selected signals' if len(selected_keys) > 1
                    else 'Remove selected signal')
        rm = QAction(rm_label, menu)
        rm.triggered.connect(self.remove_selected_series)
        menu.addAction(rm)
        # Fix 1: Set plot background accessible from table right-click
        menu.addSeparator()
        bg_act = QAction('Set plot background color...', menu)
        bg_act.triggered.connect(self._choose_plot_background_color)
        menu.addAction(bg_act)
        menu.exec(global_pos)

    def _on_stacked_scene_click(self, event) -> None:
        """Right-click in stacked plot — detect row and show signal menu."""
        try:
            btn = event.button()
        except Exception:
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
        if not len(values):
            return None
        arr  = np.asarray(values, dtype=np.float64)
        lo, hi = 0, len(arr) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if arr[mid] < target:
                lo = mid + 1
            else:
                hi = mid
        if lo == 0:
            return 0
        before = lo - 1
        return lo if abs(arr[lo] - target) < abs(arr[before] - target) else before
