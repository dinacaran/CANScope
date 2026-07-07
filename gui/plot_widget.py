from __future__ import annotations

from dataclasses import dataclass
from itertools import cycle
from typing import Any

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal, QRectF, QPointF, QSize, QByteArray, QMimeData
from PySide6.QtGui import QAction, QBrush, QColor, QDrag, QDragEnterEvent, QDropEvent, QPen, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QInputDialog,
    QGraphicsTextItem,
    QColorDialog,
    QFrame,
    QGridLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.signal_store import SignalSeries
from gui.signal_tree import SignalTreeWidget


class _LeftAxis(pg.AxisItem):
    """Left AxisItem that keeps its title adjacent to tick labels even when setWidth()
    expands the item far beyond its natural content width.

    Also routes Y-axis drag events to the correct floating ViewBox when in
    multi-axis mode.  The expanded setWidth() causes this item to cover the
    entire left margin, so without the override every drag would pan only the
    main ViewBox regardless of which floating axis the user clicked on.
    """

    def __init__(self) -> None:
        super().__init__('left')
        self._title_x: float | None = None  # if set, overrides pyqtgraph's default x=-5
        self._panel: Any = None             # set by PlotPanel after construction
        self._drag_vb: Any = None           # floating ViewBox claimed at drag start

    def mouseDragEvent(self, event) -> None:
        panel = self._panel
        if panel is not None and panel._extra_axes:
            if event.isStart():
                self._drag_vb = None
                scene_pos = event.buttonDownScenePos()
                for axis, vb in panel._extra_axes:
                    if axis.isVisible() and axis.sceneBoundingRect().contains(scene_pos):
                        self._drag_vb = vb
                        break
            if self._drag_vb is not None:
                self._drag_vb.mouseDragEvent(event, axis=1)
                if event.isFinish():
                    self._drag_vb = None
                return
        super().mouseDragEvent(event)

    def _apply_title_pos(self) -> None:
        """Move the axis label so it sits beside its own tick numbers.

        Called after every label rebuild and on resize so the position stays
        correct regardless of which event triggers a label update.
        """
        title_x = getattr(self, '_title_x', None)
        if title_x is not None and self.label is not None and self.label.isVisible():
            self.label.setX(title_x)

    def resizeEvent(self, ev=None) -> None:
        super().resizeEvent(ev)
        self._apply_title_pos()

    def setLabel(self, text='', units='', unitPrefix='', **args) -> None:
        super().setLabel(text=text, units=units, unitPrefix=unitPrefix, **args)
        # pyqtgraph repositions the label item after setLabel() returns, so
        # defer the anchor correction by one event-loop tick.
        if getattr(self, '_title_x', None) is not None:
            QTimer.singleShot(0, self._apply_title_pos)


@dataclass(slots=True)
class PlottedSignal:
    key: str
    series: SignalSeries
    curve: Any
    color: str
    axis: Any = None
    view_box: Any = None
    scatter: Any = None         # dedicated ScatterPlotItem for adaptive point display
    visible: bool = True        # checkbox state — False hides curve and stacked row
    group: str = ''             # '' = ungrouped; any string = group name
    axis_visible: bool = True   # multi-axis mode: show/hide the Y axis for this signal
    unit_group: str = ''        # multi-axis mode: normalized unit-group key
    own_axis: bool = False      # multi-axis mode: detach onto individual Y axis



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


# Manual checkbox paint — QSS ::indicator with SVG mark was unreliable across PySide6 versions.
class _CheckDelegate(QStyledItemDelegate):
    """Paints checkable table cells with theme-aware border, fill,
    and checkmark. Non-checkable cells fall through to default."""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self._panel = panel  # PlotPanel — read theme colours from it

    def paint(self, painter: QPainter, option, index):
        flags = index.flags()
        if not (flags & Qt.ItemFlag.ItemIsUserCheckable):
            super().paint(painter, option, index)
            return

        # Background — respect per-item background brush (group header rows use one)
        bg_brush = index.data(Qt.ItemDataRole.BackgroundRole)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        elif bg_brush is not None:
            painter.fillRect(option.rect, bg_brush)
        else:
            painter.fillRect(option.rect, option.palette.base())

        t = self._panel._theme_colors()
        state = index.data(Qt.ItemDataRole.CheckStateRole)
        checked = (state == Qt.CheckState.Checked.value or
                   state == Qt.CheckState.Checked)
        partial = (state == Qt.CheckState.PartiallyChecked.value or
                   state == Qt.CheckState.PartiallyChecked)

        # Text (group header arrow) — left-aligned; reserve right edge for checkbox
        text = index.data(Qt.ItemDataRole.DisplayRole)
        if text:
            fg_brush = index.data(Qt.ItemDataRole.ForegroundRole)
            if option.state & QStyle.StateFlag.State_Selected:
                painter.setPen(option.palette.highlightedText().color())
            elif fg_brush is not None:
                painter.setPen(fg_brush.color())
            else:
                painter.setPen(option.palette.text().color())
            font = index.data(Qt.ItemDataRole.FontRole)
            if font is not None:
                painter.setFont(font)
            text_rect = option.rect.adjusted(2, 0, -18, 0)
            painter.drawText(text_rect,
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             str(text))
            # Checkbox on the right side when text is present
            box = 14
            cx = option.rect.right() - box - 2
            cy = option.rect.center().y() - box // 2
        else:
            # No text — centre the checkbox
            box = 14
            cx = option.rect.center().x() - box // 2
            cy = option.rect.center().y() - box // 2

        rect = QRectF(cx, cy, box, box)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Border + fill
        painter.setPen(QPen(QColor(t['check_border']), 1.2))
        painter.setBrush(QBrush(QColor(t['check_bg'])))
        painter.drawRoundedRect(rect, 2, 2)
        # Checkmark
        if checked:
            pen = QPen(QColor(t['check_mark']), 2.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            x, y = rect.x(), rect.y()
            painter.drawPolyline([
                QPointF(x + 3,        y + box * 0.55),
                QPointF(x + box*0.42, y + box * 0.78),
                QPointF(x + box - 3,  y + box * 0.28),
            ])
        elif partial:
            # Dash for partially-checked group header rows
            pen = QPen(QColor(t['check_mark']), 2.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            mid_y = rect.center().y()
            painter.drawLine(
                QPointF(rect.x() + 3, mid_y),
                QPointF(rect.right() - 3, mid_y),
            )
        painter.restore()

    def sizeHint(self, option, index):
        return QSize(22, 22)

    def editorEvent(self, event, model, option, index):
        # Preserve native click-to-toggle behaviour.
        return super().editorEvent(event, model, option, index)


class _ReorderTable(QTableWidget):
    """QTableWidget that fires a custom internal MIME drag so row-reorder drops
    are distinguishable from external SignalTree drops."""

    _ROW_REORDER_MIME = 'application/x-canscope-row-reorder'

    def __init__(self, rows: int, cols: int, parent=None) -> None:
        super().__init__(rows, cols, parent)
        self._panel: Any = None  # set to PlotPanel immediately after creation

    def startDrag(self, supported_actions: Qt.DropActions) -> None:  # type: ignore[override]
        panel = self._panel
        if panel is None:
            super().startDrag(supported_actions)
            return
        # Collect selected signal keys in their current _items order so that
        # multi-row drags preserve the existing relative ordering.
        sel_set = set(panel.selected_keys())
        keys = [k for k in panel._items if k in sel_set]
        if not keys:
            return
        mime = QMimeData()
        mime.setData(self._ROW_REORDER_MIME,
                     QByteArray('\n'.join(keys).encode('utf-8')))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.MoveAction)


class PlotPanel(QWidget):
    selectionChanged = Signal(str)
    signalDropped = Signal(list)
    backgroundColorChanged = Signal(str)
    signalColorChanged = Signal(str, str)

    # Adaptive data-point display: symbols are drawn only when the number of
    # samples visible in the current X viewport is at or below this cap (per
    # curve). Above it, the curve renders as a line only — this keeps huge
    # files fast while still revealing individual samples when zoomed in.
    _POINTS_VISIBLE_THRESHOLD: int = 2000
    # Debounce for recomputing the visible point slice on X-range changes, so
    # nothing is recomputed during an interactive drag-zoom (ms).
    _POINTS_DEBOUNCE_MS: int = 120

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: dict[str, PlottedSignal] = {}
        self._current_key: str | None = None
        self._cursor_label_base = 'Cursor: move mouse over plot'
        self._show_points = False
        # Adaptive-point machinery: a single-shot debounce timer coalesces the
        # bursts of sigXRangeChanged emitted during a drag-zoom into one
        # recompute, and _xrange_vb tracks which ViewBox we're currently
        # listening to (recreated on every rebuild / mode switch).
        self._points_timer = QTimer(self)
        self._points_timer.setSingleShot(True)
        self._points_timer.timeout.connect(self._update_adaptive_points)
        self._xrange_vb: Any = None
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
        self._rebuild_seq: int = 0              # bumped each rebuild and by fit_to_window(); lets deferred restores detect staleness
        self.setAcceptDrops(True)

        # ── Normal / multi-axis plot ──────────────────────────────────────
        _left_axis = _LeftAxis()
        self.plot = pg.PlotWidget(axisItems={'left': _left_axis})
        _left_axis._panel = self  # enables multi-axis drag routing in mouseDragEvent
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
        self.table = _ReorderTable(0, 7)
        self.table.setHorizontalHeaderLabels(
            ['☑', 'Signal', 'Cursor 1', 'Cursor 2', 'Unit', 'Ax', 'Axis'])
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
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)      # Ax
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)      # Axis
        self.table.setColumnWidth(0, 36)    # ☑ checkbox + group arrow
        self.table.setColumnWidth(1, 190)   # Signal
        self.table.setColumnWidth(2, 90)    # Cursor 1
        self.table.setColumnWidth(3, 90)    # Cursor 2
        self.table.setColumnWidth(4, 55)    # Unit
        self.table.setColumnWidth(5, 16)    # Ax swatch (multi-axis mode only)
        self.table.setColumnWidth(6, 36)    # Axis checkbox (multi-axis mode only)
        self.table.setColumnHidden(3, True) # hidden until cursor 2 enabled
        self.table.setColumnHidden(5, True) # hidden until multi-axis mode enabled
        self.table.setColumnHidden(6, True) # hidden until multi-axis mode enabled

        self.table_panel = QWidget()
        _tbl_layout = QVBoxLayout(self.table_panel)
        _tbl_layout.setContentsMargins(0, 0, 0, 0)
        _tbl_layout.addWidget(self.table, stretch=1)
        # Drag-and-drop onto the signal table:
        #   • Internal drags (row reorder) use _ReorderTable.startDrag → custom MIME
        #   • External drags (from SignalTreeWidget) use SignalTreeWidget.MIME_TYPE
        # Both paths share the same dragEnter / drop handlers below.
        self.table.setAcceptDrops(True)
        self.table.viewport().setAcceptDrops(True)
        self.table.setDragEnabled(True)
        self.table.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.table.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.table._panel = self  # back-reference used by _ReorderTable.startDrag
        self.table.dragEnterEvent  = self._table_drag_enter
        self.table.dragMoveEvent   = self._table_drag_move
        self.table.dragLeaveEvent  = self._table_drag_leave
        self.table.dropEvent       = self._table_drop

        # Thin horizontal line shown during internal row-reorder drags to mark
        # exactly where the dragged signal(s) will land.  It is a plain child
        # widget of the viewport so it floats above table content without any
        # paintEvent override.
        self._drop_indicator = QFrame(self.table.viewport())
        self._drop_indicator.setFixedHeight(2)
        self._drop_indicator.setStyleSheet('background-color: #3d9ef0;')
        self._drop_indicator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._drop_indicator.hide()
        self._apply_panel_background()
        self._check_delegate = _CheckDelegate(self, self.table)
        self.table.setItemDelegateForColumn(0, self._check_delegate)
        self.table.setItemDelegateForColumn(6, self._check_delegate)

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
        """Toggle adaptive data-point display.

        Points live on a per-curve ScatterPlotItem that is only ever fed the
        visible slice (see :meth:`_update_adaptive_points`), so toggling is
        instant even on multi-million-sample files — no data is re-pushed into
        the line.
        """
        self._show_points = bool(show)
        # In stacked mode each PlotItem triggers its own repaint when its scene
        # changes.  Suppress individual paint events so all N rows coalesce into
        # one repaint pass when setUpdatesEnabled(True) is restored.
        if self._stacked_mode:
            self.glw.setUpdatesEnabled(False)
        try:
            if self._show_points:
                # Create a scatter for each visible curve, then fill the
                # currently-visible slice in one pass.
                self._ensure_scatters()
                self._update_adaptive_points()
            else:
                # Tear the scatters down entirely so nothing lingers to repaint.
                for plotted in self._items.values():
                    self._remove_scatter(plotted)
        finally:
            if self._stacked_mode:
                self.glw.setUpdatesEnabled(True)

    def set_multi_axis(self, enabled: bool) -> None:
        self._multi_axis = bool(enabled)
        self.table.setColumnHidden(5, not self._multi_axis)  # Ax swatch
        self.table.setColumnHidden(6, not self._multi_axis)  # Axis checkbox
        self._rebuild_curves(preserve_selection=True)
        self.fit_to_window()

    def _unit_key(self, unit: str | None, signal_key: str, own_axis: bool = False) -> str:
        """Normalize a unit string to a group key.

        Signals with the same normalized unit share one Y axis in multi-axis mode.
        Empty/None units are never merged — each gets its own unique key.
        own_axis=True forces a per-signal key regardless of unit.
        """
        if own_axis:
            return f'__own__{signal_key}'
        if unit and unit.strip():
            return unit.strip().lower()
        return f'__no_unit__{signal_key}'

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
        was_empty = not self._items
        self._items[key] = PlottedSignal(key=key, series=series, curve=None, color=color)
        if self._current_key is None:
            self._current_key = key
        if not self._batch_mode:
            self._rebuild_curves(preserve_selection=True)
            self._update_empty_state_ui()
            # Only auto-fit when adding the very first signal; otherwise
            # _rebuild_curves has already restored the saved view range.
            if was_empty:
                self.fit_to_window()

    def begin_batch_add(self) -> None:
        """Start a batch-add session. Suppresses per-signal rebuilds; call end_batch_add() when done."""
        self._push_undo()
        self._batch_mode = True
        self._batch_started_empty = not bool(self._items)

    def end_batch_add(self) -> None:
        """Finish a batch-add session. Triggers a single rebuild + fit for all queued signals."""
        self._batch_mode = False
        if self._items:
            self._rebuild_curves(preserve_selection=True)
            self._update_empty_state_ui()
            # Auto-fit only when the plot was empty before the batch (e.g. loading
            # a config into an empty plot); otherwise preserve the existing view.
            if self._batch_started_empty:
                self.fit_to_window()

    # ── Internal: clear all rendered items ────────────────────────────────

    def _clear_rendered_items(self) -> None:
        # Disconnect resize hook before clearing
        try:
            self.plot.plotItem.vb.sigResized.disconnect(self._update_multi_axis_views)
        except Exception:
            pass

        # Stop listening for X-range changes and cancel any pending point
        # recompute — the ViewBoxes and scatters are about to be destroyed.
        self._points_timer.stop()
        if self._xrange_vb is not None:
            try:
                self._xrange_vb.sigXRangeChanged.disconnect(self._on_xrange_changed)
            except Exception:
                pass
            self._xrange_vb = None

        try:
            self.plot.plotItem.clear()
        except Exception:
            pass

        # Scatters are children of the main plot / extra ViewBoxes / stacked
        # plots, so plotItem.clear(), the extra-axis scene removal below, and
        # glw.clear() all destroy them — just drop our references.
        for plotted in self._items.values():
            plotted.curve = None
            plotted.scatter = None
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

        # Save X and Y view ranges before destroying rendered items so that
        # add/remove operations don't snap the viewport back to full extent.
        _saved_xr: list | None = None
        _saved_yr: list | None = None           # standard overlay: one shared Y range
        _saved_yr_by_key: dict[str, list] = {}  # stacked: per-signal key
        _saved_yr_by_unit: dict[str, list] = {} # multi-axis: per-unit-group key

        if self._stacked_mode and self._stacked_plots:
            try:
                _saved_xr = list(self._stacked_plots[0].vb.viewRange()[0])
            except Exception:
                pass
            order = [k for k, p in self._items.items() if p.visible]
            for i, key in enumerate(order):
                if i < len(self._stacked_plots):
                    try:
                        _saved_yr_by_key[key] = list(self._stacked_plots[i].vb.viewRange()[1])
                    except Exception:
                        pass
        elif not self._stacked_mode:
            try:
                _saved_xr = list(self.plot.plotItem.vb.viewRange()[0])
            except Exception:
                pass
            if self._multi_axis:
                # Save one Y range per unit group (keyed by normalized unit).
                # Multiple signals sharing a ViewBox get the same range entry.
                _seen_ukeys: set[str] = set()
                for key, plotted in self._items.items():
                    if not plotted.visible:
                        continue
                    ukey = self._unit_key(plotted.series.unit, key, plotted.own_axis)
                    if ukey in _seen_ukeys:
                        continue
                    _seen_ukeys.add(ukey)
                    try:
                        if plotted.view_box is None:
                            _saved_yr_by_unit[ukey] = list(
                                self.plot.plotItem.vb.viewRange()[1])
                        else:
                            _saved_yr_by_unit[ukey] = list(
                                plotted.view_box.viewRange()[1])
                    except Exception:
                        pass
            else:
                try:
                    _saved_yr = list(self.plot.plotItem.vb.viewRange()[1])
                except Exception:
                    pass

        self._clear_rendered_items()

        if not self._items:
            self._refresh_table()
            self._update_empty_state_ui()
            return

        # Each rebuild gets a unique sequence number.  The closures below
        # capture it so a deferred restore can bail out if fit_to_window()
        # or a newer rebuild has run in the meantime.
        self._rebuild_seq += 1
        _seq = self._rebuild_seq

        if self._stacked_mode:
            self.view_stack.setCurrentIndex(1)
            self._rebuild_stacked()

            def _restore_stacked() -> None:
                # Bail if a newer rebuild or fit_to_window() supersedes us.
                if self._rebuild_seq != _seq:
                    return
                if _saved_xr and self._stacked_plots:
                    try:
                        self._stacked_plots[0].setXRange(_saved_xr[0], _saved_xr[1], padding=0)
                    except Exception:
                        pass
                if _saved_yr_by_key and self._stacked_plots:
                    order_after = [k for k, p in self._items.items() if p.visible]
                    for i, key in enumerate(order_after):
                        if i < len(self._stacked_plots) and key in _saved_yr_by_key:
                            try:
                                yr = _saved_yr_by_key[key]
                                self._stacked_plots[i].setYRange(yr[0], yr[1], padding=0)
                            except Exception:
                                pass
                # Disable auto-range so pyqtgraph's deferred updateAutoRange()
                # (queued by curve.setData inside _rebuild_stacked) cannot
                # override the restored ranges. fit_to_window() re-enables it.
                for p in self._stacked_plots:
                    try:
                        p.vb.enableAutoRange(x=False, y=False)
                        p.vb.setAutoVisible(x=False, y=False)
                    except Exception:
                        pass

            # Synchronous pass locks in the range before Qt paints the frame.
            # Deferred pass (singleShot 0) runs after pyqtgraph's own
            # singleShot-0 auto-range update that setData() queues.
            _restore_stacked()
            QTimer.singleShot(0, _restore_stacked)
        else:
            self.view_stack.setCurrentIndex(0)
            self._rebuild_overlay()

            def _restore_overlay() -> None:
                # Bail if a newer rebuild or fit_to_window() supersedes us.
                if self._rebuild_seq != _seq:
                    return
                if _saved_xr:
                    try:
                        self.plot.setXRange(_saved_xr[0], _saved_xr[1], padding=0)
                    except Exception:
                        pass
                if self._multi_axis and _saved_yr_by_unit:
                    # Restore one Y range per unit group (first visible signal
                    # in the group gives us the right ViewBox reference).
                    _restored_groups: set[str] = set()
                    for key, plotted in self._items.items():
                        if not plotted.visible:
                            continue
                        ukey = plotted.unit_group
                        if ukey in _restored_groups or ukey not in _saved_yr_by_unit:
                            continue
                        _restored_groups.add(ukey)
                        yr = _saved_yr_by_unit[ukey]
                        try:
                            vb = (self.plot.plotItem.vb
                                  if plotted.view_box is None else plotted.view_box)
                            vb.setYRange(yr[0], yr[1], padding=0)
                        except Exception:
                            pass
                elif _saved_yr:
                    try:
                        self.plot.setYRange(_saved_yr[0], _saved_yr[1], padding=0)
                    except Exception:
                        pass
                # Auto-fit Y for unit groups that have no saved range (newly added).
                if self._multi_axis:
                    # Collect union Y range per ViewBox for unsaved groups.
                    _new_vb_ranges: dict[int, list[float]] = {}
                    _new_vb_objs:   dict[int, Any]         = {}
                    for key, plotted in self._items.items():
                        if not plotted.visible:
                            continue
                        if plotted.unit_group in _saved_yr_by_unit:
                            continue
                        vb = (self.plot.plotItem.vb
                              if plotted.view_box is None else plotted.view_box)
                        vb_id = id(vb)
                        _new_vb_objs[vb_id] = vb
                        try:
                            y = np.asarray(plotted.series.values, dtype=np.float64)
                            finite = y[np.isfinite(y)]
                            if len(finite):
                                ymin, ymax = float(finite.min()), float(finite.max())
                                if vb_id in _new_vb_ranges:
                                    _new_vb_ranges[vb_id][0] = min(
                                        _new_vb_ranges[vb_id][0], ymin)
                                    _new_vb_ranges[vb_id][1] = max(
                                        _new_vb_ranges[vb_id][1], ymax)
                                else:
                                    _new_vb_ranges[vb_id] = [ymin, ymax]
                        except Exception:
                            pass
                    for vb_id, (ymin, ymax) in _new_vb_ranges.items():
                        if ymin == ymax:
                            ymin, ymax = ymin - 0.5, ymax + 0.5
                        pad = (ymax - ymin) * 0.05
                        try:
                            _new_vb_objs[vb_id].setYRange(ymin - pad, ymax + pad, padding=0)
                        except Exception:
                            pass
                # Disable auto-range on every affected ViewBox so pyqtgraph's
                # deferred updateAutoRange() (queued by curve.setData inside
                # _rebuild_overlay) cannot override the restored ranges.
                # fit_to_window() re-enables it explicitly when needed.
                try:
                    vb = self.plot.plotItem.vb
                    vb.enableAutoRange(x=False, y=False)
                    vb.setAutoVisible(x=False, y=False)
                except Exception:
                    pass
                if self._multi_axis:
                    _seen_vbs: set[int] = set()
                    for plotted in self._items.values():
                        if plotted.view_box is not None:
                            vb_id = id(plotted.view_box)
                            if vb_id not in _seen_vbs:
                                _seen_vbs.add(vb_id)
                                try:
                                    plotted.view_box.enableAutoRange(x=False, y=False)
                                    plotted.view_box.setAutoVisible(x=False, y=False)
                                except Exception:
                                    pass

            # Synchronous pass locks in the range before Qt paints the frame.
            # Deferred pass (singleShot 0) runs after pyqtgraph's own
            # singleShot-0 auto-range update that setData() queues.
            _restore_overlay()
            QTimer.singleShot(0, _restore_overlay)

        self._set_axis_label(self._current_key)
        self._refresh_table()
        self._update_empty_state_ui()
        if selected:
            self._restore_selection(selected)
        self._refresh_highlight()
        self._setup_mouse_proxy()
        # Points: (re)attach the X-range listener for the new ViewBoxes, then —
        # if points are on — create a fresh scatter per curve and fill in the
        # currently-visible slice. Deferred so it runs after the range-restore
        # above has settled the viewport.
        self._connect_xrange_signals()
        if self._show_points:
            self._ensure_scatters()
            QTimer.singleShot(0, self._update_adaptive_points)
        # Repopulate cursor value columns — _refresh_table wipes them to ''
        self._update_table_values(self.v_line.value(), col=2)
        if self._cursor2_enabled:
            self._update_table_values(self.v_line2.value(), col=3)

    def _rebuild_overlay(self) -> None:
        """Normal or multi-axis: all signals on one PlotWidget, extra axes to the left."""
        main_axis = self.plot.getAxis('left')

        if not self._multi_axis:
            # ── Single-axis mode ─────────────────────────────────────────────
            for key, plotted in self._items.items():
                if not plotted.visible:
                    plotted.curve      = None
                    plotted.unit_group = ''
                    continue
                curve = self.plot.plot([], [], name=key)
                self._configure_curve(curve)
                plotted.curve      = curve
                plotted.axis       = main_axis
                plotted.view_box   = None
                plotted.unit_group = ''
                self._apply_curve_style(plotted)
                try:
                    self._legend.addItem(plotted.curve, key)
                except Exception:
                    pass
            if isinstance(main_axis, _LeftAxis):
                main_axis._title_x = None
            return

        # ── Multi-axis mode: group visible signals by unit ───────────────────
        # Assign unit_group for invisible signals first (needed for table swatch
        # and axis-visibility sync even when the signal has no curve).
        for key, plotted in self._items.items():
            if not plotted.visible:
                plotted.curve      = None
                plotted.unit_group = self._unit_key(plotted.series.unit, key, plotted.own_axis)

        # Build ordered unit groups (insertion-order of first visible signal per unit)
        unit_groups: dict[str, list[str]] = {}
        for key, plotted in self._items.items():
            if not plotted.visible:
                continue
            ukey = self._unit_key(plotted.series.unit, key, plotted.own_axis)
            if ukey not in unit_groups:
                unit_groups[ukey] = []
            unit_groups[ukey].append(key)

        if not unit_groups:
            if isinstance(main_axis, _LeftAxis):
                main_axis._title_x = None
            return

        for group_idx, (ukey, keys) in enumerate(unit_groups.items()):
            first         = self._items[keys[0]]
            axis_color    = first.color
            unit_label    = self._axis_group_label(keys)
            ax_vis        = first.axis_visible   # first signal's state drives the group

            if group_idx == 0:
                # First group → main left axis + main ViewBox
                group_axis = main_axis
                group_vb   = None
                if ax_vis:
                    self.plot.setLabel('left', unit_label, color=axis_color)
                    main_axis.setTextPen(pg.mkPen(axis_color))
                    main_axis.setStyle(showValues=True)
                    main_axis.setVisible(True)
                else:
                    # Fake-hide: blanking content preserves layout space that
                    # floating extra axes depend on (setVisible(False) collapses it).
                    self.plot.setLabel('left', '')
                    main_axis.setTextPen(pg.mkPen(None))
                    main_axis.setStyle(showValues=False)
            else:
                # Extra group → new floating AxisItem + ViewBox
                axis = pg.AxisItem('left')
                vb   = pg.ViewBox()
                # Disable auto-range before X-linking so the first setData()
                # doesn't propagate an auto-range reset to the main plot.
                vb.enableAutoRange(x=False, y=False)
                vb.setAutoVisible(x=False, y=False)
                self.plot.plotItem.scene().addItem(vb)
                self.plot.plotItem.scene().addItem(axis)
                axis.linkToView(vb)
                vb.setXLink(self.plot.plotItem.vb)
                axis.setLabel(unit_label, color=axis_color)
                axis.setTextPen(pg.mkPen(axis_color))
                axis.setWidth(self._EXTRA_AXIS_W)
                axis.setStyle(autoExpandTextSpace=False, tickTextOffset=2)
                axis.setVisible(ax_vis)
                group_axis = axis
                group_vb   = vb
                self._extra_axes.append((axis, vb))

            # Add one curve per signal in this group — all share the same ViewBox.
            for key in keys:
                plotted              = self._items[key]
                plotted.unit_group   = ukey
                plotted.axis         = group_axis
                plotted.view_box     = group_vb
                plotted.axis_visible = ax_vis   # keep all group members in sync
                if group_idx == 0:
                    curve = self.plot.plot([], [], name=key)
                else:
                    curve = pg.PlotDataItem()
                    group_vb.addItem(curve)
                self._configure_curve(curve)
                plotted.curve = curve
                self._apply_curve_style(plotted)
                try:
                    self._legend.addItem(plotted.curve, key)
                except Exception:
                    pass

        if self._extra_axes:
            n_vis        = sum(1 for axis, _ in self._extra_axes if axis.isVisible())
            total_left_w = self._compute_main_axis_width(n_vis)
            main_axis.setWidth(total_left_w)
            main_axis.setStyle(autoExpandTextSpace=False, tickTextOffset=3)
            # Anchor the main axis title adjacent to its tick labels rather than at
            # the far-left of the expanded axis item (pyqtgraph default is x = -5).
            if isinstance(main_axis, _LeftAxis):
                main_axis._title_x = total_left_w - self._MAIN_AXIS_W - 5
                # setLabel() was called above before _title_x was known (group_idx==0
                # branch), so schedule the anchor now in case setWidth() doesn't fire
                # a resizeEvent (e.g. same width after signal reorder).
                QTimer.singleShot(0, main_axis._apply_title_pos)
            self.plot.plotItem.vb.sigResized.connect(self._update_multi_axis_views)
            QTimer.singleShot(10, self._update_multi_axis_views)
        else:
            if isinstance(main_axis, _LeftAxis):
                main_axis._title_x = None

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
            p.addItem(curve)
            # NB: _configure_curve MUST run after addItem — PlotItem.addItem
            # overwrites the item's clipToView / autoDownsample with the
            # PlotItem's own (off-by-default) modes, so configuring beforehand
            # would be silently wiped and the row would render every sample.
            self._configure_curve(curve)
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
        """Apply the line pen.  Selected curves are drawn thicker.

        The PlotDataItem is **line only** — data points are never drawn via its
        built-in symbol.  Feeding the full multi-million-sample array to the
        symbol path costs ~7 s per add/rebuild on a 5 M-sample file, because the
        internal ScatterPlotItem processes every sample.  Instead points live on
        a separate ScatterPlotItem (``plotted.scatter``) that is fed only the
        searchsorted-sliced visible window, capped at
        ``_POINTS_VISIBLE_THRESHOLD`` — see :meth:`_update_adaptive_points`.

        (Historical note: the old code toggled ``curve.setSymbol('o')``.  The
        subtlety there was that ``updateItems(styleUpdate=True)`` only restyles
        existing spots and never populates an empty scatter, so symbol=None → 'o'
        needed a full setData.  That whole class of problem is gone now that the
        scatter is managed explicitly.)

        set_data=True  (default, called on initial add): pushes the full line
            buffer into pyqtgraph via setData().
        set_data=False (highlight change): uses the cheap setPen() setter so the
            data arrays are never re-read.
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
            plotted.curve.setData(ts, vs, pen=pen, connect='finite')
        else:
            plotted.curve.setPen(pen)

    def _refresh_highlight(self) -> None:
        """Update curve thickness to reflect current table selection."""
        sel = set(self.selected_keys())
        for key, plotted in self._items.items():
            if plotted.curve is not None and plotted.visible:
                self._apply_curve_style(plotted, selected=(key in sel),
                                        set_data=False)

    # ── Adaptive data-point display ───────────────────────────────────────

    def _scatter_container(self, plotted: PlottedSignal) -> Any:
        """The graphics container a curve's scatter must live in.

        Mirrors the rule used for curves: ``view_box is None`` means the main
        PlotWidget (normal mode, or the first unit group in multi-axis); any
        other value is a floating multi-axis ViewBox or a stacked-row ViewBox.
        """
        return self.plot if plotted.view_box is None else plotted.view_box

    def _make_scatter(self, plotted: PlottedSignal) -> Any:
        """Create + attach an empty ScatterPlotItem for *plotted* and return it.

        Kept deliberately cheap so pyqtgraph reuses one cached spot pixmap for
        every dot: uniform size, a single shared brush, and ``pen=None`` (no
        per-spot outline).  Started empty; data is pushed by
        :meth:`_update_adaptive_points`.
        """
        sc = pg.ScatterPlotItem(
            size=5, pen=None, brush=pg.mkBrush(plotted.color), pxMode=True,
        )
        sc.setData(x=[], y=[])
        try:
            self._scatter_container(plotted).addItem(sc)
        except Exception:
            pass
        plotted.scatter = sc
        return sc

    def _ensure_scatters(self) -> None:
        """Create a scatter for every visible, rendered curve that lacks one.

        Runs after a rebuild (curves already exist) so the scatters are added
        on top of the lines. No-op when points are off.
        """
        if not self._show_points:
            return
        for plotted in self._items.values():
            if (plotted.visible and plotted.curve is not None
                    and plotted.scatter is None):
                self._make_scatter(plotted)

    def _remove_scatter(self, plotted: PlottedSignal) -> None:
        """Detach and drop *plotted*'s scatter, if any."""
        sc = plotted.scatter
        if sc is None:
            return
        try:
            self._scatter_container(plotted).removeItem(sc)
        except Exception:
            pass
        plotted.scatter = None

    def _driving_vb(self) -> Any:
        """ViewBox whose X range governs what's visible in every curve.

        Floating (multi-axis) and stacked ViewBoxes are all X-linked, so a
        single X range applies to the whole plot.
        """
        if self._stacked_mode and self._stacked_plots:
            return self._stacked_plots[0].vb
        return self.plot.plotItem.vb

    def _on_xrange_changed(self, *args) -> None:
        """sigXRangeChanged slot — (re)arm the debounce so recompute happens
        once the view stops moving, never during an interactive drag-zoom."""
        if self._show_points:
            self._points_timer.start(self._POINTS_DEBOUNCE_MS)

    def _connect_xrange_signals(self) -> None:
        """Listen to the current driving ViewBox's sigXRangeChanged.

        Called after each rebuild because the driving ViewBox changes with the
        mode (stacked rows are recreated every rebuild; the overlay vb is
        stable but reconnecting is harmless and keeps the logic uniform).
        """
        vb = self._driving_vb()
        if self._xrange_vb is vb:
            return
        if self._xrange_vb is not None:
            try:
                self._xrange_vb.sigXRangeChanged.disconnect(self._on_xrange_changed)
            except Exception:
                pass
        self._xrange_vb = vb
        if vb is not None:
            try:
                vb.sigXRangeChanged.connect(self._on_xrange_changed)
            except Exception:
                pass

    def _update_adaptive_points(self) -> None:
        """Refresh every scatter to hold only the visible slice of its series.

        For each visible curve we searchsorted the (monotonic) timestamp array
        for the current X range and, if the number of samples in view is at or
        below the threshold, push just that slice into the scatter; otherwise
        the scatter is emptied and the curve reads as a line.  The scatter is
        therefore never handed more than ``_POINTS_VISIBLE_THRESHOLD`` points,
        regardless of file size.
        """
        if not self._show_points or not self._items:
            for plotted in self._items.values():
                if plotted.scatter is not None:
                    plotted.scatter.setData(x=[], y=[])
            return
        try:
            xr = self._driving_vb().viewRange()[0]
        except Exception:
            return
        x0, x1 = xr[0], xr[1]
        thr = self._POINTS_VISIBLE_THRESHOLD
        for plotted in self._items.values():
            sc = plotted.scatter
            if sc is None or not plotted.visible:
                continue
            ts = np.asarray(plotted.series.timestamps, dtype=np.float64)
            if ts.size == 0:
                sc.setData(x=[], y=[])
                continue
            lo = int(np.searchsorted(ts, x0, side='left'))
            hi = int(np.searchsorted(ts, x1, side='right'))
            count = hi - lo
            if 0 < count <= thr:
                vs = np.asarray(plotted.series.values, dtype=np.float64)
                sc.setData(x=ts[lo:hi], y=vs[lo:hi])
            else:
                # Zoomed out past the threshold (or nothing in view) → line only.
                sc.setData(x=[], y=[])

    # ── Multi-axis geometry (called via sigResized) ───────────────────────

    # Width constants for multi-axis layout
    _MAIN_AXIS_W: int = 50        # width of the main (first) left axis ticks+labels
    _MAIN_AXIS_TITLE_W: int = 5  # extra reserve for the rotated axis title (renders at far-left of setWidth area)
    _EXTRA_AXIS_W: int = 50       # width of each additional floating axis (60px fits 5-digit tick text)
    _EXTRA_AXIS_GAP: int = 4      # gap between adjacent axis panels

    def _compute_main_axis_width(self, n_vis: int) -> int:
        """Total left-margin width: main axis + title reserve + visible floating axes."""
        return (self._MAIN_AXIS_W + self._MAIN_AXIS_TITLE_W
                + n_vis * (self._EXTRA_AXIS_W + self._EXTRA_AXIS_GAP))

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
        # Update ViewBox geometry for all axes (visible and hidden).
        for axis, vb in self._extra_axes:
            vb.setGeometry(rect)
            vb.linkedViewChanged(self.plot.plotItem.vb, vb.XAxis)

        slot = 0
        for axis, vb in reversed(self._extra_axes):
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
        # ── Re-entry guard ──────────────────────────────────────────────────
        # setPos() on each non-sender line re-fires sigPositionChanged, which
        # would call this handler again for every line — O(n²) updates per
        # drag event.  The flag is set BEFORE the loop and checked HERE so the
        # recursive call returns immediately.
        if getattr(self, '_syncing_c1', False):
            return
        if not self._stacked_c1_lines:
            return
        x = self.sender().value() if self.sender() else self._stacked_c1_lines[0].value()
        # Sync v_line silently — blockSignals prevents _on_cursor1_moved from
        # firing and doing a redundant _update_table_values pass.
        self.v_line.blockSignals(True)
        self.v_line.setPos(x)
        self.v_line.blockSignals(False)
        # Batch all per-row line moves into one scene repaint.
        self._syncing_c1 = True
        self.glw.setUpdatesEnabled(False)
        try:
            for line in self._stacked_c1_lines:
                if line is not self.sender():
                    line.setPos(x)
        finally:
            self._syncing_c1 = False
            self.glw.setUpdatesEnabled(True)
        self._update_table_values(x, col=2)
        self._update_cursor_labels()

    def _on_stacked_c2_moved(self) -> None:
        """Sync all stacked C2 lines when any one is dragged."""
        if getattr(self, '_syncing_c2', False):
            return
        if not self._stacked_c2_lines:
            return
        x = self.sender().value() if self.sender() else self._stacked_c2_lines[0].value()
        self.v_line2.blockSignals(True)
        self.v_line2.setPos(x)
        self.v_line2.blockSignals(False)
        self._syncing_c2 = True
        self.glw.setUpdatesEnabled(False)
        try:
            for line in self._stacked_c2_lines:
                if line is not self.sender():
                    line.setPos(x)
        finally:
            self._syncing_c2 = False
            self.glw.setUpdatesEnabled(True)
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
        # Cancel any deferred range-restore queued by _rebuild_curves so that
        # an explicit fit is not silently overwritten on the next event-loop tick.
        self._rebuild_seq += 1
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
            # Union Y range across all signals that share the same ViewBox.
            _vb_y: dict[int, list[float]] = {}
            _vb_obj: dict[int, Any] = {}
            for key, plotted in self._items.items():
                if not plotted.visible:
                    continue
                vals = [v for v in plotted.series.values if v == v]
                if not vals:
                    continue
                y_min, y_max = min(vals), max(vals)
                vb    = self.plot.plotItem.vb if plotted.view_box is None else plotted.view_box
                vb_id = id(vb)
                _vb_obj[vb_id] = vb
                if vb_id in _vb_y:
                    _vb_y[vb_id][0] = min(_vb_y[vb_id][0], y_min)
                    _vb_y[vb_id][1] = max(_vb_y[vb_id][1], y_max)
                else:
                    _vb_y[vb_id] = [y_min, y_max]
            for vb_id, (y_min, y_max) in _vb_y.items():
                pad = (y_max - y_min) * 0.05 if y_min != y_max else (
                    1.0 if y_min == 0 else abs(y_min) * 0.05)
                _vb_obj[vb_id].disableAutoRange()
                _vb_obj[vb_id].setYRange(y_min - pad, y_max + pad, padding=0)
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
            # Union Y range for the visible data within each ViewBox's X range.
            _fv_y: dict[int, list[float]] = {}
            _fv_obj: dict[int, Any] = {}
            for key, plotted in self._items.items():
                if not plotted.visible:
                    continue
                vb    = self.plot.plotItem.vb if plotted.view_box is None else plotted.view_box
                vb_id = id(vb)
                _fv_obj[vb_id] = vb
                try:
                    xr = vb.viewRange()[0]
                except Exception:
                    continue
                result = _y_range_for_visible(plotted.series, xr[0], xr[1])
                if result is None:
                    continue
                if vb_id in _fv_y:
                    _fv_y[vb_id][0] = min(_fv_y[vb_id][0], result[0])
                    _fv_y[vb_id][1] = max(_fv_y[vb_id][1], result[1])
                else:
                    _fv_y[vb_id] = list(result)
            for vb_id, (y_min, y_max) in _fv_y.items():
                _fv_obj[vb_id].disableAutoRange()
                _fv_obj[vb_id].setYRange(y_min, y_max, padding=0)

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
        plotted = self._items[key]
        plotted.color = color
        self._apply_curve_style(plotted)
        # Recolor the point scatter too (cheap: restyles existing spots only).
        if plotted.scatter is not None:
            plotted.scatter.setBrush(pg.mkBrush(color))
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
        self.table.viewport().update()
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

    def _axis_group_label(self, keys: list[str]) -> str:
        """Compute the Y-axis label for a unit group.

        Single-member group: 'unit (Name)' when unit is present, else just 'Name'.
        Multi-member group:  unit string only (all members share the same unit).
        Signal names longer than 20 chars are truncated with an ellipsis.
        """
        first = self._items[keys[0]]
        unit = (first.series.unit or '').strip()
        if len(keys) == 1:
            name = first.series.signal_name
            if len(name) > 20:
                name = name[:19] + '…'
            return f'{unit} ({name})' if unit else name
        return unit

    def _set_axis_label(self, key: str | None) -> None:
        if self._stacked_mode:
            return   # each row already has its own label
        if not key or key not in self._items:
            self.plot.setLabel('left', 'Value')
            return
        if self._multi_axis:
            # Build axis_id → keys map (visible signals only) so _axis_group_label
            # can distinguish single- from multi-member groups.
            axis_keys: dict[int, list[str]] = {}
            for k, p in self._items.items():
                if p.visible and p.axis is not None:
                    axis_keys.setdefault(id(p.axis), []).append(k)
            _seen_axes: set[int] = set()
            for k, p in self._items.items():
                if not p.visible or p.axis is None:
                    continue
                ax_id = id(p.axis)
                if ax_id in _seen_axes:
                    continue
                _seen_axes.add(ax_id)
                unit_lbl = self._axis_group_label(axis_keys.get(ax_id, [k]))
                if p.view_box is None:
                    # Main left axis
                    if p.axis_visible:
                        self.plot.setLabel('left', unit_lbl, color=p.color)
                        self.plot.getAxis('left').setTextPen(pg.mkPen(p.color))
                        self.plot.getAxis('left').setStyle(showValues=True)
                else:
                    if p.axis_visible:
                        p.axis.setLabel(unit_lbl, color=p.color)
                        p.axis.setTextPen(pg.mkPen(p.color))
        else:
            series = self._items[key].series
            label  = series.signal_name + (f' ({series.unit})' if series.unit else '')
            self.plot.setLabel('left', label, color=self._items[key].color)
            self.plot.getAxis('left').setTextPen(pg.mkPen(self._items[key].color))

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

        # Pre-compute unit-group axis colors (first visible signal per group).
        # Used for the Ax swatch column in multi-axis mode.
        _unit_axis_colors: dict[str, str] = {}
        for key, plotted in self._items.items():
            if not plotted.visible:
                continue
            ukey = self._unit_key(plotted.series.unit, key, plotted.own_axis)
            if ukey not in _unit_axis_colors:
                _unit_axis_colors[ukey] = plotted.color

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
                # Col 5: Ax — colored swatch showing the unit-group axis color
                ukey         = self._unit_key(plotted.series.unit, key, plotted.own_axis)
                swatch_color = _unit_axis_colors.get(ukey, '#606060')
                sw_item = QTableWidgetItem('▮')
                sw_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                sw_item.setForeground(QBrush(QColor(swatch_color)))
                sw_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                # Col 6: Axis visibility checkbox (multi-axis mode only)
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
                self.table.setItem(row_idx, 5, sw_item)
                self.table.setItem(row_idx, 6, ax_item)
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
                own_axis=v.own_axis,
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

    def remove_selected_series(self) -> None:
        self._push_undo()
        for key in list(self.selected_keys()):
            self._items.pop(str(key), None)
        self._current_key = next(iter(self._items), None)
        self._rebuild_curves(preserve_selection=False)
        self._set_axis_label(self._current_key)
        self._update_empty_state_ui()

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

    # ── Shared helper: insertion-point from a viewport y coordinate ───────

    def _reorder_insert_row(self, vp_y: int) -> int:
        """Convert a y position in VIEWPORT coordinates to a logical insert row.

        Returns a value in [0, rowCount()] where rowCount() means "append at
        end".  Correctly handles the edge cases where rowAt() returns -1:
          • y above the first visible row → insert before that row (= first row)
          • y below the last row's bottom  → append (= rowCount())
        """
        n = self.table.rowCount()
        if n == 0:
            return 0

        drop_row = self.table.rowAt(vp_y)
        if drop_row == -1:
            # rowAt returns -1 for y above the content area OR below the last row.
            # Distinguish by comparing against the first visible row's top edge.
            for r in range(n):
                if not self.table.isRowHidden(r):
                    top = self.table.visualRect(
                        self.table.model().index(r, 0)
                    ).top()
                    return r if vp_y < top else n
            return n  # all rows hidden

        rect = self.table.visualRect(self.table.model().index(drop_row, 0))
        if vp_y < rect.top() + rect.height() / 2:
            return drop_row      # top half → insert before this row
        else:
            return drop_row + 1  # bottom half → insert after this row

    # ── Indicator show / hide ─────────────────────────────────────────────

    def _show_drop_indicator(self, insert_row: int) -> None:
        """Position and show the blue drop-indicator line inside the viewport."""
        n = self.table.rowCount()
        vp_w = self.table.viewport().width()

        if insert_row >= n:
            # Append: line below the last visible row
            y = 0
            for r in range(n - 1, -1, -1):
                if not self.table.isRowHidden(r):
                    y = self.table.visualRect(
                        self.table.model().index(r, 0)
                    ).bottom()
                    break
        else:
            # Find the first non-hidden row at or after insert_row and use its top
            y = 0
            for r in range(insert_row, n):
                if not self.table.isRowHidden(r):
                    y = self.table.visualRect(
                        self.table.model().index(r, 0)
                    ).top()
                    break
            else:
                # All rows from insert_row onward are hidden (collapsed) →
                # fall back to the bottom of the last visible row before insert_row
                for r in range(insert_row - 1, -1, -1):
                    if not self.table.isRowHidden(r):
                        y = self.table.visualRect(
                            self.table.model().index(r, 0)
                        ).bottom()
                        break

        # Clamp so the indicator stays inside the viewport
        y = max(0, min(y, self.table.viewport().height() - 2))
        self._drop_indicator.setGeometry(0, y, vp_w, 2)
        self._drop_indicator.show()
        self._drop_indicator.raise_()

    def _hide_drop_indicator(self) -> None:
        self._drop_indicator.hide()

    # ── Table drag / drop event handlers ─────────────────────────────────

    def _table_drag_enter(self, event) -> None:
        if (event.mimeData().hasFormat(SignalTreeWidget.MIME_TYPE) or
                event.mimeData().hasFormat(_ReorderTable._ROW_REORDER_MIME)):
            event.acceptProposedAction()
        else:
            event.ignore()

    def _table_drag_move(self, event) -> None:
        """Accept the move event; show the indicator only for internal reorder drags."""
        mime = event.mimeData()
        if mime.hasFormat(_ReorderTable._ROW_REORDER_MIME):
            event.acceptProposedAction()
            # event.position() is already in viewport coordinates (the
            # monkey-patch receives events forwarded from the viewport).
            vp_y = event.position().toPoint().y()
            self._show_drop_indicator(self._reorder_insert_row(vp_y))
        elif mime.hasFormat(SignalTreeWidget.MIME_TYPE):
            event.acceptProposedAction()
            self._hide_drop_indicator()
        else:
            event.ignore()
            self._hide_drop_indicator()

    def _table_drag_leave(self, event) -> None:
        self._hide_drop_indicator()

    def _table_drop(self, event) -> None:
        self._hide_drop_indicator()
        mime = event.mimeData()
        if mime.hasFormat(_ReorderTable._ROW_REORDER_MIME):
            self._handle_row_reorder_drop(event)
        elif mime.hasFormat(SignalTreeWidget.MIME_TYPE):
            payload = bytes(mime.data(SignalTreeWidget.MIME_TYPE)).decode('utf-8')
            keys = [p.strip() for p in payload.splitlines() if p.strip()]
            if keys:
                self.signalDropped.emit(keys)
                event.acceptProposedAction()
            else:
                event.ignore()
        else:
            event.ignore()

    def _handle_row_reorder_drop(self, event) -> None:
        """Reorder self._items when the user drags and drops signal rows internally."""
        payload = bytes(
            event.mimeData().data(_ReorderTable._ROW_REORDER_MIME)
        ).decode('utf-8')
        dragged_keys = [
            k.strip() for k in payload.splitlines()
            if k.strip() and k.strip() in self._items
        ]
        if not dragged_keys:
            event.ignore()
            return
        event.acceptProposedAction()

        # event.position() is already in viewport coordinates — no mapping needed.
        # (The monkey-patch receives the event that Qt forwards from the viewport,
        # so the position is already relative to the viewport widget.)
        vp_y = event.position().toPoint().y()
        insert_row = self._reorder_insert_row(vp_y)

        # ── Build list of (row, type, key) for visible rows ───────────────
        rows_info: list[tuple[int, str, str]] = []
        for row in range(self.table.rowCount()):
            if self.table.isRowHidden(row):
                continue
            item0 = self.table.item(row, 0)
            if not item0:
                continue
            k = item0.data(Qt.ItemDataRole.UserRole)
            if isinstance(k, str) and k.startswith('__group__'):
                rows_info.append((row, 'group', k[len('__group__'):]))
            elif isinstance(k, str) and k in self._items:
                rows_info.append((row, 'signal', k))

        # ── Determine target group at the insertion point ──────────────────
        # Scan forward through visible rows; track "current group" as each
        # group header is passed.  An ungrouped signal resets the group context
        # (so dropping after an ungrouped signal yields no group).
        target_group = ''
        for row, rtype, rkey in rows_info:
            if row >= insert_row:
                break
            if rtype == 'group':
                target_group = rkey
            elif rtype == 'signal':
                sig_group = self._items[rkey].group if rkey in self._items else ''
                if not sig_group:
                    target_group = ''  # ungrouped signal resets group context

        # ── Find the signal key the dragged block should be inserted before ─
        # Uses _row_lookup (key→row, rebuilt by _refresh_table); correctly
        # handles hidden rows in collapsed groups (their real row indices are
        # stored in _row_lookup even when they are not visible).
        dragged_set = set(dragged_keys)
        insert_before_key: str | None = None
        for key in self._items:
            if key in dragged_set:
                continue
            row = self._row_lookup.get(key)
            if row is not None and row >= insert_row:
                insert_before_key = key
                break

        # ── Compute new key order ──────────────────────────────────────────
        remaining = [k for k in self._items if k not in dragged_set]
        if insert_before_key is None:
            new_order = remaining + dragged_keys
        else:
            idx = remaining.index(insert_before_key)
            new_order = remaining[:idx] + dragged_keys + remaining[idx:]

        # ── Snapshot state BEFORE mutation (consistent with existing convention)
        self._push_undo()

        # ── Apply group assignment ─────────────────────────────────────────
        for k in dragged_keys:
            self._items[k].group = target_group

        # ── Reorder _items (dict order = display order) ────────────────────
        self._items = {k: self._items[k] for k in new_order}

        # ── Rebuild all views; view ranges are preserved by _rebuild_curves ─
        self._rebuild_curves(preserve_selection=True)

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

        # Cols 2-6: empty, same background
        for col in (2, 3, 4, 5, 6):
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
        # Newly-enabled signals have no curve yet — _rebuild_overlay skipped them
        # last pass. The lightweight setVisible toggle can't materialise a curve,
        # so fall through to a full rebuild.
        # Multi-axis mode also always rebuilds: axis colors depend on the first
        # visible signal per unit group, which can change on any visibility toggle.
        needs_rebuild = self._stacked_mode or self._multi_axis or any(
            p.visible and p.curve is None for p in self._items.values()
        )
        if needs_rebuild:
            self._rebuild_curves(preserve_selection=True)
            return
        # Lightweight path — every visible signal already has a curve,
        # just toggle setVisible on existing PlotDataItems.
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
        """Show or hide the Y axis for the unit group of the given signal.

        All signals sharing the same unit group get their axis_visible synced.
        Hiding an axis frees its layout slot so the plot area expands to fill
        the gap.  Showing it re-inserts it at the next available slot.
        """
        plotted = self._items.get(key)
        if plotted is None:
            return
        # Determine this signal's unit group and sync axis_visible for the whole group.
        target_ukey = self._unit_key(plotted.series.unit, key, plotted.own_axis)
        group_keys = [k for k, p in self._items.items()
                      if self._unit_key(p.series.unit, k, p.own_axis) == target_ukey]
        for k, p in self._items.items():
            if self._unit_key(p.series.unit, k, p.own_axis) == target_ukey:
                p.axis_visible = visible
        # Apply to the shared axis object (only needs to happen once).
        if plotted.axis is not None:
            if plotted.view_box is None:
                # Main left axis: setVisible(False) collapses the entire left
                # margin, destroying the layout space that floating extra axes
                # depend on.  Fake "hidden" by blanking the visual content instead.
                if visible:
                    lbl = self._axis_group_label(group_keys)
                    plotted.axis.setLabel(lbl, color=plotted.color)
                    plotted.axis.setTextPen(pg.mkPen(plotted.color))
                    plotted.axis.setStyle(showValues=True)
                else:
                    plotted.axis.setLabel('')
                    plotted.axis.setTextPen(pg.mkPen(None))
                    plotted.axis.setStyle(showValues=False)
            else:
                plotted.axis.setVisible(visible)
        if self._multi_axis and self._extra_axes:
            # Resize the left margin to match the number of now-visible extra axes.
            n_vis = sum(1 for axis, _ in self._extra_axes if axis.isVisible())
            new_w = self._compute_main_axis_width(n_vis)
            main_axis = self.plot.getAxis('left')
            main_axis.setWidth(new_w)
            if isinstance(main_axis, _LeftAxis):
                main_axis._title_x = new_w - self._MAIN_AXIS_W - 5
                QTimer.singleShot(0, main_axis._apply_title_pos)
            QTimer.singleShot(15, self._update_multi_axis_views)
        # Refresh table so all rows in the group show the updated checkbox state.
        self._refresh_table()

    def _set_own_axis(self, keys: list[str], enabled: bool) -> None:
        """Detach or rejoin signals onto their own individual Y axis in multi-axis mode."""
        self._push_undo()
        for key in keys:
            if key in self._items:
                self._items[key].own_axis = enabled
        self._rebuild_curves(preserve_selection=True)

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
        # Multi-axis: individual axis option
        if self._multi_axis:
            menu.addSeparator()
            sel_signals = [k for k in selected_keys if k in self._items]
            all_own = bool(sel_signals) and all(self._items[k].own_axis for k in sel_signals)
            own_act = QAction('Individual axis', menu)
            own_act.setCheckable(True)
            own_act.setChecked(all_own)
            own_act.setEnabled(has_sel)
            _keys_snap = list(selected_keys)
            own_act.triggered.connect(
                lambda checked, ks=_keys_snap: self._set_own_axis(ks, checked)
            )
            menu.addAction(own_act)
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

    def _theme_colors(self) -> dict:
        """Return palette dict used by _CheckDelegate for theme-aware checkbox colours."""
        bg = self._background_color.lstrip('#')
        try:
            r, g, b = int(bg[0:2], 16), int(bg[2:4], 16), int(bg[4:6], 16)
            lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
        except (ValueError, IndexError):
            lum = 0.0
        if lum < 0.5:
            return {'check_border': '#808080', 'check_bg': '#2a2a2a', 'check_mark': '#ffffff'}
        return {'check_border': '#606060', 'check_bg': '#ffffff', 'check_mark': '#101010'}

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