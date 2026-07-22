from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QThread, Qt
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QButtonGroup,
    QSizePolicy,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QDockWidget,
    QSplitter,
    QHeaderView,
    QAbstractItemView,
)

from core.export import ExportService
from core.calculated_signals import (
    LARGE_OUTPUT_WARNING_POINTS,
    CalculatedSignalDefinition,
    CalculatedSignalError,
    CalculatedSignalManager,
    parse_formula,
)
from core.load_worker import LoadWorker
from core.readers import dbc_required_for, prescan_measurement, ALL_SUFFIXES
from core.channel_config import ChannelConfig, ALL_CHANNELS_KEY
from gui.dbc_manager import DBCManagerDialog
from core.signal_store import SignalStore
from gui.plot_widget import PlotPanel
from gui.signal_tree import SignalTreeWidget
from gui.calculated_signal_dialog import CalculatedSignalDialog, CalculationWorker
from gui.raw_frame_dialog import RawFrameDialog


class MainWindow(QMainWindow):
    def __init__(self, app_name: str, version: str, parent: QWidget | None = None, splash=None) -> None:
        super().__init__(parent)
        self.app_name = app_name
        self.version = version
        self.setWindowTitle(f'{app_name} {version}')
        self.resize(1700, 950)

        self._splash = splash
        self.measurement_path: str | None = None
        # Multi-DBC channel configuration — persists between measurement loads
        self.channel_config: ChannelConfig = ChannelConfig()
        # Legacy single-DBC alias for config backward-compat
        self.dbc_path: str | None = None
        self.blf_path: str | None = None  # deprecated alias
        self.store: SignalStore | None = None
        self.calculated_signals = CalculatedSignalManager()
        self._calc_thread: QThread | None = None
        self._calc_worker: CalculationWorker | None = None
        self._calc_active_request: tuple[CalculatedSignalDefinition, str, bool] | None = None
        self._calc_queue: list[
            tuple[CalculatedSignalDefinition, str, bool, dict]
        ] = []
        self._calc_source_store: SignalStore | None = None
        # Pre-scan cache: (path, channels, ids_per_channel)
        self._prescan_cache: tuple[str, list[int], dict[int, set[int]]] | None = None
        # Full RawFrameStore channel/ID summaries are built only on an explicit
        # Database Manager refresh, then reused while this decoded store lives.
        self._channel_data_cache: tuple[
            tuple[str | None, int, int, int],
            list[int],
            dict[int, set[int]],
        ] | None = None
        self._thread: QThread | None = None
        self._worker: LoadWorker | None = None
        self._pending_plot_keys: list[str] = []
        self._pending_plot_colors: dict[str, str] = {}
        self._pending_plot_visible: dict[str, bool] = {}
        self._pending_plot_groups:  dict[str, str]  = {}
        self._pending_plot_axis_visible: dict[str, bool] = {}
        self._pending_plot_own_axis: dict[str, bool] = {}
        # Store keys plotted by the most recent plot_finding() call — cleared
        # and replaced (not accumulated) on each subsequent finding click.
        self._finding_plot_keys: set[str] = set()
        self._raw_frame_dialog = None
        self._log_file_path = Path(__file__).resolve().parents[1] / 'canscope_dev.log'

        self._splash_status('Initialising plot panel...')
        self._build_ui()
        self._splash_status('Building toolbar...')
        self._build_toolbar()
        self._build_shortcuts()
        self._update_action_states()  # grey-out on startup
        self._set_ready_status()

        # Hidden diagnostics feature — Ctrl+Shift+A. No menu/toolbar entry.
        # Disable with env var CANSCOPE_DIAGNOSTICS=0.
        from gui.diagnostics.activation import install_shortcut
        install_shortcut(self)

        self._log(f'{self.app_name} {self.version} started.')
        self._log(f'Dev log file: {self._log_file_path}')
        self._update_measurement_tab()

    def _splash_status(self, message: str) -> None:
        """Forward a status message to the splash screen if still visible."""
        if self._splash is not None:
            self._splash.set_status(message)

    def _build_ui(self) -> None:
        self.signal_tree = SignalTreeWidget()
        self.plot_panel = PlotPanel()
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.diagnostics_box = QTextEdit()
        self.diagnostics_box.setReadOnly(True)
        self.measurement_box = QTextEdit()
        self.measurement_box.setReadOnly(True)

        self.signal_tree.signalActivated.connect(self.add_signals_to_plot)
        self.signal_tree.generatedEditRequested.connect(self.edit_generated_signal)
        self.signal_tree.generatedDeleteRequested.connect(self.delete_generated_signal)
        self.plot_panel.selectionChanged.connect(self._on_plot_selection_changed)
        self.plot_panel.signalDropped.connect(self.add_signals_to_plot)
        self.plot_panel.backgroundColorChanged.connect(self._on_background_color_changed)
        self.plot_panel.signalColorChanged.connect(self._on_signal_color_changed)

        button_row = QWidget()
        button_layout = QHBoxLayout(button_row)
        button_layout.setContentsMargins(0, 0, 0, 0)
        self.btn_fit = QPushButton('Fit to Window')
        self.btn_fit_v = QPushButton('Fit Vertical')
        self.btn_remove = QPushButton('Remove Selected')
        self.btn_multi_axis = QPushButton('Multi-Axis')
        self.btn_multi_axis.setCheckable(True)
        self.btn_stacked = QPushButton('Stacked')
        self.btn_stacked.setCheckable(True)
        self.btn_stacked.setChecked(True)  # default plot mode on app startup
        self.btn_cursor1 = QPushButton('Cursor 1')
        self.btn_cursor1.setCheckable(True)
        self.btn_cursor1.setChecked(False)  # OFF by default
        self.btn_cursor2 = QPushButton('Cursor 2')
        self.btn_cursor2.setCheckable(True)
        self.btn_points = QPushButton('Show Data Points')
        self.btn_points.setCheckable(True)
        self.btn_hide_line = QPushButton('Hide Line')
        self.btn_hide_line.setCheckable(True)
        self.btn_hide_line.setEnabled(False)
        for btn in (self.btn_fit, self.btn_fit_v, self.btn_remove, self.btn_multi_axis, self.btn_stacked, self.btn_cursor1, self.btn_cursor2, self.btn_points, self.btn_hide_line):
            button_layout.addWidget(btn)
        button_layout.addStretch(1)

        self.btn_fit.clicked.connect(self.plot_panel.fit_to_window)
        self.btn_fit_v.clicked.connect(self.plot_panel.fit_vertical)
        self.btn_remove.clicked.connect(self.plot_panel.remove_selected_series)
        self.btn_multi_axis.toggled.connect(self._toggle_multi_axis)
        self.btn_stacked.toggled.connect(self._toggle_stacked)
        self.btn_cursor1.toggled.connect(self._toggle_cursor1)
        self.btn_cursor2.toggled.connect(self._toggle_cursor2)
        self.btn_points.toggled.connect(self._toggle_points)
        self.btn_hide_line.toggled.connect(self._toggle_line)
        self.plot_panel.set_stacked(self.btn_stacked.isChecked())

        center_panel = QWidget()
        center_layout = QVBoxLayout(center_panel)
        center_layout.setContentsMargins(6, 6, 6, 6)
        center_layout.addWidget(button_row)

        self.center_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.center_splitter.setChildrenCollapsible(False)
        self.center_splitter.addWidget(self.plot_panel.table_panel)
        self.center_splitter.addWidget(self.plot_panel)
        self.center_splitter.setStretchFactor(0, 0)
        self.center_splitter.setStretchFactor(1, 1)
        self.center_splitter.setSizes([240, 1280])
        center_layout.addWidget(self.center_splitter, stretch=1)
        self.setCentralWidget(center_panel)

        self.left_dock = QDockWidget('Decoded Signals', self)
        self.left_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.left_dock.setWidget(self.signal_tree)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.left_dock)

        self.bottom_tabs = QTabWidget()
        self.bottom_tabs.addTab(self.log_box, 'Log')
        self.bottom_tabs.addTab(self.diagnostics_box, 'Diagnostics')
        self.bottom_tabs.addTab(self.measurement_box, 'Measurement')
        self.bottom_dock = QDockWidget('Log / Diagnostics / Measurement', self)
        self.bottom_dock.setAllowedAreas(Qt.BottomDockWidgetArea | Qt.TopDockWidgetArea)
        self.bottom_dock.setWidget(self.bottom_tabs)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.bottom_dock)

        self.resizeDocks([self.bottom_dock], [180], Qt.Vertical)

        left_title = QWidget()
        left_title_layout = QHBoxLayout(left_title)
        left_title_layout.setContentsMargins(4, 2, 4, 2)
        left_title_layout.addWidget(QLabel('Decoded Signals'))
        left_title_layout.addStretch(1)
        self.left_toggle_btn = QToolButton()
        self.left_toggle_btn.setText('◀')
        self.left_toggle_btn.clicked.connect(self._toggle_left_panel)
        left_title_layout.addWidget(self.left_toggle_btn)
        self.left_dock.setTitleBarWidget(left_title)

        bottom_title = QWidget()
        bottom_title_layout = QHBoxLayout(bottom_title)
        bottom_title_layout.setContentsMargins(4, 2, 4, 2)
        bottom_title_layout.addWidget(QLabel('Log / Diagnostics / Measurement'))
        bottom_title_layout.addStretch(1)
        self.bottom_toggle_btn = QToolButton()
        self.bottom_toggle_btn.setText('▼')
        self.bottom_toggle_btn.clicked.connect(self._toggle_bottom_panel)
        bottom_title_layout.addWidget(self.bottom_toggle_btn)
        self.bottom_dock.setTitleBarWidget(bottom_title)

        self.left_dock.visibilityChanged.connect(self._sync_panel_toggle_buttons)
        self.bottom_dock.visibilityChanged.connect(self._sync_panel_toggle_buttons)

        self.left_edge_btn = QToolButton(self)
        self.left_edge_btn.setFixedWidth(22)
        self.left_edge_btn.setMinimumHeight(36)
        self.left_edge_btn.setStyleSheet("""
QToolButton {
    background-color: #2d3a4a; color: #e0e8f0;
    border: 1px solid #4a6080; border-radius: 4px;
    font-size: 11px; font-weight: bold; padding: 2px;
}
QToolButton:hover { background-color: #3a5070; border-color: #6090b0; }
QToolButton:pressed { background-color: #1a2a3a; }
""")
        self.left_edge_btn.setToolTip('Show / hide signal panel')
        self.left_edge_btn.clicked.connect(self._toggle_left_panel)
        self.left_edge_btn.show()

        self.bottom_edge_btn = QToolButton(self)
        self.bottom_edge_btn.setFixedHeight(22)
        self.bottom_edge_btn.setMinimumWidth(36)
        self.bottom_edge_btn.setStyleSheet("""
QToolButton {
    background-color: #2d3a4a; color: #e0e8f0;
    border: 1px solid #4a6080; border-radius: 4px;
    font-size: 11px; font-weight: bold; padding: 2px;
}
QToolButton:hover { background-color: #3a5070; border-color: #6090b0; }
QToolButton:pressed { background-color: #1a2a3a; }
""")
        self.bottom_edge_btn.setToolTip('Show / hide log panel')
        self.bottom_edge_btn.clicked.connect(self._toggle_bottom_panel)
        self.bottom_edge_btn.show()

        status_bar = QStatusBar()
        self.setStatusBar(status_bar)
        self.status_state_label = QLabel('State: Ready')
        self.status_next_step_label = QLabel('Next: Open BLF, then Open Database, then Load + Decode')
        self.statusBar().addWidget(self.status_state_label)
        self.statusBar().addPermanentWidget(self.status_next_step_label, 1)
        self._sync_panel_toggle_buttons()

    def _build_toolbar(self) -> None:
        toolbar = QToolBar('Main')
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        self._toolbar_actions: dict[str, QAction] = {}
        for text, slot in [
            ('Open File',    self.choose_blf),
            ('Open Database', self.choose_dbc),
            ('Load + Decode',self.load_data),
            ('Save Config',  self.save_configuration),
            ('Load Config',  self.load_configuration),
            ('New Signal',   self.new_generated_signal),
            ('Export',       self.export_selected),
            ('Clear Plots',  self.clear_plot),
        ]:
            act = QAction(text, self)
            act.triggered.connect(slot)
            toolbar.addAction(act)
            self._toolbar_actions[text] = act
            if text in {'Load + Decode', 'Load Config'}:
                toolbar.addSeparator()
        toolbar.addSeparator()
        shortcuts_act = QAction('Shortcuts', self)
        shortcuts_act.triggered.connect(self.show_shortcuts)
        toolbar.addAction(shortcuts_act)
        self._act_can_trace = QAction('CAN Trace', self)
        self._act_can_trace.triggered.connect(self.show_raw_frames)
        self._act_can_trace.setEnabled(False)  # enabled after decode
        toolbar.addAction(self._act_can_trace)

    def _build_shortcuts(self) -> None:
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self, activated=self.plot_panel.remove_selected_series)
        QShortcut(QKeySequence('Ctrl+S'), self, activated=self.save_configuration)
        QShortcut(QKeySequence('F'), self, activated=self.plot_panel.fit_to_window)
        QShortcut(QKeySequence('V'), self, activated=self.plot_panel.fit_vertical)
        QShortcut(QKeySequence('C'), self, activated=self._shortcut_change_signal_color)
        QShortcut(QKeySequence('R'), self, activated=self._shortcut_toggle_cursors)
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, activated=lambda: self.add_signals_to_plot(self.signal_tree.selected_signal_keys()))
        # Raw Frames hidden from GUI to prevent hang on large files — accessible via shortcut
        QShortcut(QKeySequence('Ctrl+Shift+R'), self, activated=self.show_raw_frames)
        QShortcut(QKeySequence('Ctrl+Z'), self, activated=self.plot_panel.undo)

    def choose_blf(self) -> None:
        """Open any supported measurement file (BLF, ASC, MF4, MDF, CSV)."""
        all_ext = ' '.join(f'*{e}' for e in sorted(ALL_SUFFIXES))
        filt = (
            'Measurement Files (*.blf *.asc *.mf4 *.mdf *.csv);;'
            'Vector BLF (*.blf);;'
            'Vector ASC (*.asc);;'
            'ASAM MDF4 (*.mf4);;'
            'ASAM MDF (*.mdf);;'
            'CSV Signals (*.csv);;'
            f'All supported ({all_ext})'
        )
        path, _ = QFileDialog.getOpenFileName(
            self, 'Open Measurement File', '', filt
        )
        if not path:
            return
        self.measurement_path = path
        self.blf_path = path   # keep alias in sync for config
        needs_dbc = dbc_required_for(path)
        self._log(f'Selected measurement file: {path}')
        if not needs_dbc:
            self._log('Database not required for this format.')

        # Lightweight pre-scan: extract channel numbers + arb IDs
        # so the DBC Manager can show real channels before Load+Decode
        self._prescan_cache = None
        self._channel_data_cache = None
        if needs_dbc:
            self._update_status('Scanning channels…', 'Reading measurement file header')
            QApplication.processEvents()
            try:
                chs, ids = prescan_measurement(path, progress=self._log)
                if chs:
                    self._prescan_cache = (path, chs, ids)
                    self._log(f'Pre-scan: found {len(chs)} channel(s): '
                              f'{", ".join(f"CAN {c}" for c in chs)}')
            except Exception as exc:
                self._log(f'Pre-scan warning: {exc}')

        self._update_measurement_tab()
        self._update_action_states()
        self._update_status(
            'Measurement file selected', self._next_step_message()
        )

    def _collect_channel_data(
        self,
        *,
        full_scan: bool = False,
    ) -> tuple[list[int], dict[int, set[int]]]:
        """
        Collect CAN channel numbers and per-channel arbitration ID sets
        for the Database Manager.

        Normal dialog opening reuses the lightweight pre-scan cache so a large
        decoded BLF/ASC is never walked merely to display the dialog. An
        explicit Refresh Match requests ``full_scan=True``; that full summary
        is computed in bounded NumPy chunks and cached for later refreshes.
        """
        cached_path = None
        cached_chs: list[int] = []
        cached_ids: dict[int, set[int]] = {}
        if self._prescan_cache is not None:
            cached_path, cached_chs, cached_ids = self._prescan_cache

        rfs = getattr(self.store, 'raw_frame_store', None) if self.store else None
        raw_count = len(rfs) if rfs is not None else 0
        cache_key = None
        if raw_count > 0:
            cache_key = (
                self.measurement_path,
                id(self.store),
                id(rfs),
                raw_count,
            )
            if (
                self._channel_data_cache is not None
                and self._channel_data_cache[0] == cache_key
            ):
                _, channels_in_file, ids_per_channel = self._channel_data_cache
                return (
                    list(channels_in_file),
                    {ch: set(ids) for ch, ids in ids_per_channel.items()},
                )

        if cached_path == self.measurement_path and not full_scan:
            return (
                list(cached_chs),
                {ch: set(ids) for ch, ids in cached_ids.items()},
            )

        # Config-driven loads may not have a pre-scan cache. Opening the
        # dialog must still remain O(number of channels), never O(frames).
        # The user can request complete ID coverage with Refresh Match.
        if not full_scan:
            channels = (
                {int(ch) for ch in self.store.channels if ch is not None}
                if self.store is not None else set()
            )
            return sorted(channels), {}

        if raw_count > 0:
            import numpy as np
            chs  = np.frombuffer(rfs.channels, dtype=np.uint8)
            aids = np.frombuffer(rfs.arb_ids,  dtype=np.uint32)
            ids_per_channel: dict[int, set[int]] = {}

            # Packing (channel, arbitration ID) into uint64 lets np.unique do
            # the per-frame reduction in C. Chunking bounds temporary memory
            # even when the store contains tens of millions of frames.
            chunk_size = 500_000
            for start in range(0, len(chs), chunk_size):
                stop = min(start + chunk_size, len(chs))
                chunk_channels = chs[start:stop]
                packed = aids[start:stop].astype(np.uint64, copy=True)
                packed |= chunk_channels.astype(np.uint64) << np.uint64(32)
                for packed_value in np.unique(packed):
                    value = int(packed_value)
                    channel = value >> 32
                    if channel == 255:
                        continue
                    ids_per_channel.setdefault(channel, set()).add(
                        value & 0xFFFF_FFFF
                    )

            channels_in_file = sorted(ids_per_channel)
            assert cache_key is not None
            self._channel_data_cache = (
                cache_key,
                list(channels_in_file),
                {ch: set(ids) for ch, ids in ids_per_channel.items()},
            )
            return channels_in_file, ids_per_channel

        # Native one-pass MDF has no RawFrameStore. Preserve the pre-scan IDs
        # and merge in any channels discovered from decoded signals.
        channels = set(cached_chs if cached_path == self.measurement_path else [])
        ids_per_channel = (
            {ch: set(ids) for ch, ids in cached_ids.items()}
            if cached_path == self.measurement_path else {}
        )
        if self.store is not None:
            channels.update(int(ch) for ch in self.store.channels if ch is not None)
        return sorted(channels), ids_per_channel

    def choose_dbc(self) -> None:
        """Open the DBC Manager dialog to assign DBCs to channels."""
        channels_in_file, ids_per_channel = self._collect_channel_data()

        dlg = DBCManagerDialog(
            channel_config   = self.channel_config,
            channels_in_file = channels_in_file,
            ids_per_channel  = ids_per_channel,
            parent           = self,
            data_provider    = lambda: self._collect_channel_data(full_scan=True),
        )
        if dlg.exec() != DBCManagerDialog.DialogCode.Accepted:
            return

        self.channel_config = dlg.result_config()
        paths = self.channel_config.all_dbc_paths()
        self.dbc_path = paths[0] if paths else None
        self._log(self.channel_config.summary())
        self._update_measurement_tab()
        self._update_action_states()
        self._update_status('Database configured', self._next_step_message())

    def _toggle_multi_axis(self, checked: bool) -> None:
        if checked:
            self.btn_stacked.setChecked(False)   # mutually exclusive
        self.plot_panel.set_multi_axis(checked)
        self._update_status('Plot mode updated', 'Continue plotting or fit the view')

    def _toggle_stacked(self, checked: bool) -> None:
        if checked:
            self.btn_multi_axis.setChecked(False)  # mutually exclusive
        self.plot_panel.set_stacked(checked)
        self._update_status('Plot mode updated', 'Continue plotting or fit the view')

    def _toggle_cursor1(self, checked: bool) -> None:
        self.plot_panel.set_cursor1_enabled(checked)
        self.btn_cursor1.setText('Cursor 1: ON' if checked else 'Cursor 1')
        self._update_status('Cursor 1 updated',
                            'Drag C1 line on the plot to measure')

    def _toggle_cursor2(self, checked: bool) -> None:
        self.plot_panel.set_cursor2_enabled(checked)
        self.btn_cursor2.setText('Cursor 2: ON' if checked else 'Cursor 2')
        self._update_status('Cursor 2 updated',
                            'Drag C2 line to measure time delta between cursors')

    def _shortcut_change_signal_color(self) -> None:
        key = self.plot_panel._current_key
        if not key or key not in self.plot_panel._items:
            keys = self.plot_panel.selected_keys()
            key = keys[0] if keys else None
        if key:
            self.plot_panel._choose_color_for_key(str(key))

    def _shortcut_toggle_cursors(self) -> None:
        both_on = self.btn_cursor1.isChecked() and self.btn_cursor2.isChecked()
        target = not both_on
        self.btn_cursor1.setChecked(target)
        self.btn_cursor2.setChecked(target)

    def show_raw_frames(self) -> None:
        rfs = getattr(self.store, 'raw_frame_store', None) if self.store else None
        if not rfs or len(rfs) == 0:
            reason = getattr(
                self.store,
                'raw_trace_unavailable_reason',
                'This measurement does not contain raw CAN frame records.',
            ) if self.store else 'Load a measurement file first.'
            QMessageBox.information(self, 'CAN Trace unavailable', reason)
            return
        self._raw_frame_dialog = RawFrameDialog(rfs, self)
        self._raw_frame_dialog.show()
        self._raw_frame_dialog.raise_()
        self._raw_frame_dialog.activateWindow()

    def _toggle_points(self, checked: bool) -> None:
        self.plot_panel.set_show_points(checked)
        self.btn_points.setText('Hide Data Points' if checked else 'Show Data Points')
        if not checked:
            self.btn_hide_line.setChecked(False)
        self.btn_hide_line.setEnabled(checked)
        self._update_status('Plot markers updated', 'Continue plotting, fit view, or save configuration')

    def _toggle_line(self, checked: bool) -> None:
        # The control is enabled only while data points are visible, ensuring
        # that hiding the line can never leave the plot without a data trace.
        if checked and not self.btn_points.isChecked():
            self.btn_hide_line.setChecked(False)
            return
        hide_line = bool(checked and self.btn_points.isChecked())
        self.plot_panel.set_hide_lines(hide_line)
        self._update_status('Plot line visibility updated', 'Continue plotting, fit view, or save configuration')

    def _refresh_generated_signal_tree(self) -> None:
        rows = []
        for definition in self.calculated_signals.definitions():
            unit_text = f" [{definition.unit}]" if definition.unit else ""
            rows.append((
                definition.key,
                definition.name,
                f"{definition.name}{unit_text} = {definition.formula}\n"
                "Double-click to plot; right-click to edit or delete.",
            ))
        self.signal_tree.set_generated_signals(rows)

    def _apply_pending_generated_plot_state(self, key: str) -> None:
        plotted = self.plot_panel._items.get(key)
        if plotted is None:
            return
        changed = False
        if key in self._pending_plot_colors:
            plotted.color = self._pending_plot_colors.pop(key)
            changed = True
        if key in self._pending_plot_visible:
            plotted.visible = self._pending_plot_visible.pop(key)
            changed = True
        if key in self._pending_plot_groups:
            plotted.group = self._pending_plot_groups.pop(key)
            changed = True
        if key in self._pending_plot_axis_visible:
            plotted.axis_visible = self._pending_plot_axis_visible.pop(key)
            changed = True
        if key in self._pending_plot_own_axis:
            plotted.own_axis = self._pending_plot_own_axis.pop(key)
            changed = True
        if changed:
            self.plot_panel._rebuild_curves(preserve_selection=False)

    def new_generated_signal(self) -> None:
        if self.store is None or self._calc_thread is not None:
            return
        dialog = CalculatedSignalDialog(
            self.store.all_keys(),
            name_validator=self.calculated_signals.assert_unique_name,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._queue_calculation(dialog.definition(), "create", plot_after=False)

    def edit_generated_signal(self, key: str) -> None:
        if self.store is None or self._calculation_is_pending(key):
            return
        existing = self.calculated_signals.definition(key)
        if existing is None:
            return
        dialog = CalculatedSignalDialog(
            self.store.all_keys(),
            existing=existing,
            name_validator=lambda name: self.calculated_signals.assert_unique_name(
                name, except_key=key
            ),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._queue_calculation(dialog.definition(), "edit", plot_after=False)

    def delete_generated_signal(self, key: str) -> None:
        definition = self.calculated_signals.definition(key)
        if definition is None:
            return
        if self._calculation_is_pending(key):
            QMessageBox.information(
                self,
                "Calculation in progress",
                "Wait for this generated-signal calculation to finish before deleting it.",
            )
            return
        answer = QMessageBox.question(
            self,
            "Delete generated signal",
            f"Delete generated signal '{definition.name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.plot_panel.forget_series(key)
        self.calculated_signals.delete(key)
        self._refresh_generated_signal_tree()
        self._log(f"Deleted generated signal: {definition.name}")

    def _calculation_is_pending(self, key: str) -> bool:
        if self._calc_active_request and self._calc_active_request[0].key == key:
            return True
        return any(
            definition.key == key
            for definition, _operation, _plot, _sources in self._calc_queue
        )

    def _queue_calculation(
        self,
        definition: CalculatedSignalDefinition,
        operation: str,
        plot_after: bool,
    ) -> None:
        if self.store is None:
            return
        if self._calculation_is_pending(definition.key):
            return
        try:
            if operation == "create":
                self.calculated_signals.assert_unique_name(definition.name)
            parsed = parse_formula(definition.formula, self.store.all_keys())
            source_series = {}
            for key in parsed.references:
                series = self.store.get_series(key)
                if series is None:
                    raise CalculatedSignalError(f"Measurement signal not found: {key}")
                source_series[key] = series
            estimated_points = sum(len(series.timestamps) for series in source_series.values())
        except CalculatedSignalError as exc:
            QMessageBox.warning(self, "Invalid generated signal", str(exc))
            return

        if estimated_points > LARGE_OUTPUT_WARNING_POINTS:
            answer = QMessageBox.warning(
                self,
                "Large generated signal",
                f"This calculation may produce up to {estimated_points:,} samples and "
                "may require substantial RAM.\n\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        request = (definition, operation, plot_after)
        if self._calc_thread is not None:
            self._calc_queue.append((definition, operation, plot_after, source_series))
            return
        self._start_calculation(request, source_series)

    def _start_calculation(
        self,
        request: tuple[CalculatedSignalDefinition, str, bool],
        source_series: dict,
    ) -> None:
        definition, _operation, _plot_after = request
        self._calc_active_request = request
        self._calc_source_store = self.store
        self._calc_thread = QThread(self)
        self._calc_worker = CalculationWorker(definition, source_series)
        self._calc_worker.moveToThread(self._calc_thread)
        self._calc_thread.started.connect(self._calc_worker.run)
        self._calc_worker.finished.connect(self._on_calculation_finished)
        self._calc_worker.failed.connect(self._on_calculation_failed)
        self._calc_worker.finished.connect(self._calc_worker.deleteLater)
        self._calc_worker.failed.connect(self._calc_worker.deleteLater)
        self._calc_worker.finished.connect(self._calc_thread.quit)
        self._calc_worker.failed.connect(self._calc_thread.quit)
        self._calc_thread.finished.connect(self._cleanup_calculation)
        self._update_action_states()
        self._update_status(
            f"Calculating {definition.name}...",
            "The calculation runs in the background; existing plots remain usable.",
        )
        self._calc_thread.start()

    def _on_calculation_finished(self, series) -> None:
        request = self._calc_active_request
        if request is None:
            return
        definition, operation, plot_after = request
        if self.store is not self._calc_source_store:
            self._log(f"Discarded stale generated signal result: {definition.name}")
            return
        self.calculated_signals.commit(definition, series)
        self._refresh_generated_signal_tree()
        if key_is_plotted := definition.key in self.plot_panel._items:
            self.plot_panel.replace_series(definition.key, series)
        if plot_after and not key_is_plotted:
            self.add_signal_to_plot(definition.key)
        self._apply_pending_generated_plot_state(definition.key)
        action = "Updated" if operation == "edit" else "Created"
        if operation == "lazy":
            action = "Calculated"
        self._log(f"{action} generated signal: {definition.name}")
        self._update_status(
            f"{action} generated signal {definition.name}",
            "Plot it from Generate Signals or save the configuration.",
        )

    def _on_calculation_failed(self, error_message: str) -> None:
        definition = self._calc_active_request[0] if self._calc_active_request else None
        name = definition.name if definition else "signal"
        self._log(f"Generated signal calculation failed ({name}): {error_message}")
        QMessageBox.warning(self, "Generated signal calculation failed", error_message)
        self._update_status("Generated signal failed", "Correct the formula and try again.")

    def _cleanup_calculation(self) -> None:
        if self._calc_thread is not None:
            self._calc_thread.deleteLater()
        self._calc_worker = None
        self._calc_thread = None
        self._calc_active_request = None
        self._calc_source_store = None
        self._update_action_states()
        if not self._calc_queue:
            return
        definition, operation, plot_after, source_series = self._calc_queue.pop(0)
        self._start_calculation(
            (definition, operation, plot_after),
            source_series,
        )

    def save_configuration(self) -> None:
        config = {
            'version': self.version,
            'measurement_path': self.measurement_path or self.blf_path,  # canonical key
            'blf_path': self.measurement_path or self.blf_path,  # legacy alias, read by older CANScope versions
            'dbc_path': self.dbc_path,  # legacy single-DBC
            'channel_config': {
                'name': self.channel_config.name,
                'channels': {str(k): v for k, v in self.channel_config.channels.items()},
            },
            'signals': [
                {
                    'key':          k,
                    'visible':      self.plot_panel._items[k].visible,
                    'group':        self.plot_panel._items[k].group,
                    'axis_visible': self.plot_panel._items[k].axis_visible,
                    'own_axis':     self.plot_panel._items[k].own_axis,
                }
                for k in self.plot_panel.plotted_keys()
            ],
            'generated_signals': self.calculated_signals.to_config(),
            'show_data_points': self.btn_points.isChecked(),
            'hide_plot_lines': self.btn_hide_line.isChecked(),
            'plot_background_color': self.plot_panel.background_color(),
            'signal_colors': self.plot_panel.series_colors(),
            'multi_axis': self.btn_multi_axis.isChecked(),
            'stacked': self.btn_stacked.isChecked(),
            'cursor1': self.btn_cursor1.isChecked(),
            'cursor2': self.btn_cursor2.isChecked(),
            'name_show_channel': self.plot_panel._name_show_channel,
            'name_show_message': self.plot_panel._name_show_message,           # Fix 4
            'table_column_widths': self.plot_panel.table_column_widths(),  # Fix 2
        }
        path, _ = QFileDialog.getSaveFileName(self, 'Save configuration', 'canscope_config.json', 'JSON Files (*.json)')
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(config, indent=2), encoding='utf-8')
            self._log(f'Saved configuration: {path}')
            self._update_status('Configuration saved', 'Load it later to reopen the measurement file, database, and plotted signals')
        except Exception as exc:
            QMessageBox.critical(self, 'Save configuration failed', str(exc))
            self._update_status('Save failed', 'Check path permissions and try again')

    def load_configuration(self) -> None:
        if self._calc_thread is not None:
            QMessageBox.information(
                self,
                'Calculation in progress',
                'Wait for the generated-signal calculation to finish before loading a configuration.',
            )
            return
        path, _ = QFileDialog.getOpenFileName(self, 'Load configuration', '', 'JSON Files (*.json)')
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8'))
        except Exception as exc:
            QMessageBox.critical(self, 'Load configuration failed', str(exc))
            return

        # measurement_path is the canonical key; blf_path is read as a fallback
        # for configs saved by older CANScope versions.
        cfg_mpath = data.get('measurement_path') or data.get('blf_path')
        cfg_dbc = data.get('dbc_path')
        # Restore channel_config (new format) or fall back to single-DBC compat
        cfg_ch = data.get('channel_config')
        if cfg_ch and isinstance(cfg_ch, dict):
            self.channel_config = ChannelConfig(
                name=cfg_ch.get('name', 'Unnamed'),
                channels={int(k): v for k, v in cfg_ch.get('channels', {}).items()},
            )
        elif cfg_dbc:
            self.channel_config = ChannelConfig.from_single_dbc(cfg_dbc)
        # ── Parse signals list: supports new dict format and old plain-string format ──
        signals_data         = list(data.get('signals') or [])
        pending_keys         = []
        pending_visible      = {}
        pending_groups       = {}
        pending_axis_visible = {}
        pending_own_axis     = {}
        for s in signals_data:
            if isinstance(s, str):
                pending_keys.append(s)
            elif isinstance(s, dict):
                k = s.get('key')
                if k:
                    pending_keys.append(k)
                    if 'visible' in s:
                        pending_visible[k] = bool(s['visible'])
                    if s.get('group'):
                        pending_groups[k] = str(s['group'])
                    if 'axis_visible' in s:
                        pending_axis_visible[k] = bool(s['axis_visible'])
                    if 'own_axis' in s:
                        pending_own_axis[k] = bool(s['own_axis'])
        pending_colors = dict(data.get('signal_colors') or {})
        generated_errors = self.calculated_signals.replace_definitions(
            data.get('generated_signals') or []
        )
        for error in generated_errors:
            self._log(f'Generated signal configuration skipped: {error}')
        self._refresh_generated_signal_tree()
        generated_pending = {
            key for key in pending_keys if self.calculated_signals.contains_key(key)
        }
        self._pending_plot_colors = {
            key: value for key, value in pending_colors.items() if key in generated_pending
        }
        self._pending_plot_visible = {
            key: value for key, value in pending_visible.items() if key in generated_pending
        }
        self._pending_plot_groups = {
            key: value for key, value in pending_groups.items() if key in generated_pending
        }
        self._pending_plot_axis_visible = {
            key: value for key, value in pending_axis_visible.items() if key in generated_pending
        }
        self._pending_plot_own_axis = {
            key: value for key, value in pending_own_axis.items() if key in generated_pending
        }

        # Fix 6: if data is already decoded, ask the user what to do
        use_current_data = False
        if self.store is not None:
            reply = QMessageBox.question(
                self,
                'Data already loaded',
                'A measurement file is already decoded in memory.\n\n'
                'What would you like to do?\n\n'
                '  Yes — keep current data, plot signals from config\n'
                '  No  — reload the measurement file (and database) from the configuration file',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            use_current_data = (reply == QMessageBox.StandardButton.Yes)

        # Apply visual settings regardless of data source
        self.btn_points.setChecked(bool(data.get('show_data_points', False)))
        self.btn_hide_line.setChecked(
            bool(data.get('hide_plot_lines', False)) and self.btn_points.isChecked()
        )
        bg = data.get('plot_background_color')
        if bg:
            self.plot_panel.set_background_color(str(bg))
        multi_axis = bool(data.get('multi_axis', False))
        self.btn_multi_axis.setChecked(multi_axis)
        # Configurations that explicitly saved a plot mode keep it. Older
        # configurations without a ``stacked`` key use the new app default,
        # unless they explicitly select multi-axis mode.
        self.btn_stacked.setChecked(bool(data.get('stacked', not multi_axis)))
        self.btn_cursor1.setChecked(bool(data.get('cursor1', False)))
        self.btn_cursor2.setChecked(bool(data.get('cursor2', False)))
        self.plot_panel._name_show_channel = bool(data.get('name_show_channel', False))
        self.plot_panel._name_show_message = bool(data.get('name_show_message', False))
        col_widths = data.get('table_column_widths')
        if col_widths:
            self.plot_panel.set_table_column_widths([int(w) for w in col_widths])

        if use_current_data:
            # Reuse already-decoded store — plot signals and restore all visual state
            self._log(f'Configuration loaded (using current data): {path}')
            self._update_status('Config applied', 'Plotting signals from configuration')
            self.add_signals_to_plot(pending_keys)
            for key, color in pending_colors.items():
                self.plot_panel.set_series_color(key, color)
            # Restore visibility and group assignments
            needs_rebuild = False
            for key in pending_keys:
                if key in self.plot_panel._items:
                    if key in pending_visible:
                        self.plot_panel._items[key].visible = pending_visible[key]
                        needs_rebuild = True
                    if pending_groups.get(key):
                        self.plot_panel._items[key].group = pending_groups[key]
                        needs_rebuild = True
                    if key in pending_axis_visible:
                        self.plot_panel._items[key].axis_visible = pending_axis_visible[key]
                        needs_rebuild = True
                    if key in pending_own_axis:
                        self.plot_panel._items[key].own_axis = pending_own_axis[key]
                        needs_rebuild = True
            if needs_rebuild:
                self.plot_panel._rebuild_curves(preserve_selection=False)
            return

        # Reload from config measurement path (+ database, if the format needs one)
        self.measurement_path = cfg_mpath
        self.blf_path = cfg_mpath   # alias
        self.dbc_path = cfg_dbc
        self._pending_plot_keys         = pending_keys
        self._pending_plot_colors       = pending_colors
        self._pending_plot_visible      = pending_visible
        self._pending_plot_groups       = pending_groups
        self._pending_plot_axis_visible = pending_axis_visible
        self._pending_plot_own_axis     = pending_own_axis
        self._update_measurement_tab()

        if not cfg_mpath:
            QMessageBox.warning(
                self, 'Incomplete configuration',
                'The configuration file contains no measurement file path.'
            )
            return
        if not Path(cfg_mpath).exists():
            QMessageBox.warning(
                self, 'Measurement file not found',
                f'The measurement file referenced by this configuration could not be found:\n\n'
                f'{cfg_mpath}\n\n'
                'Use "Open File" to locate it, then try again.'
            )
            return
        if dbc_required_for(cfg_mpath) and self.channel_config.is_empty():
            QMessageBox.warning(
                self, 'Incomplete configuration',
                'This measurement file requires a database (DBC or ARXML), '
                'but the configuration file does not contain one.'
            )
            return

        self._log(f'Configuration loaded: {path}')
        self.load_data(pending_plot_keys=self._pending_plot_keys)

    def load_data(self, pending_plot_keys: list[str] | None = None) -> None:
        if self._calc_thread is not None:
            QMessageBox.information(
                self,
                'Calculation in progress',
                'Wait for the generated-signal calculation to finish before loading another measurement.',
            )
            return
        mpath = self.measurement_path or self.blf_path
        if not mpath:
            QMessageBox.warning(self, 'Missing file', 'Please select a measurement file first.')
            self._update_status('Waiting for input', self._next_step_message())
            return
        # Database required only for CAN-raw formats
        if dbc_required_for(mpath) and self.channel_config.is_empty():
            reply = QMessageBox.question(
                self, 'No database configured',
                'This format requires a database (DBC or ARXML) for signal decoding.\n'
                'No database is configured yet.\n\n'
                'Open Database Manager now?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.choose_dbc()
            if self.channel_config.is_empty():
                self._update_status('Waiting for input', self._next_step_message())
                return
        self._pending_plot_keys = list(pending_plot_keys or [])
        self.plot_panel.clear_all()
        self.plot_panel.discard_undo_history()
        self.calculated_signals.invalidate_cache()
        self._calc_queue.clear()
        self._finding_plot_keys = set()
        self.signal_tree.set_payload({})
        self.diagnostics_box.clear()
        self.store = None
        self._update_action_states()
        self._update_measurement_tab(frames='0', decoded='0', samples='0', channels='0')
        self._log(f'Loading: {mpath}')
        self._update_status('Loading and decoding...', 'Wait for decode to finish, then inspect Diagnostics and plot signals')
        self._thread = QThread(self)
        self._worker = LoadWorker(mpath, self.channel_config if not self.channel_config.is_empty() else None)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_worker_progress)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.failed.connect(self._on_worker_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        # Streaming: update tree and plots while decoding
        self._worker.tree_update.connect(self._on_tree_update)
        self._worker.partial_ready.connect(self._on_partial_ready)
        self._thread.finished.connect(self._cleanup_worker)
        self._thread.start()

    def add_signals_to_plot(self, keys) -> None:
        if isinstance(keys, str):
            keys = [keys]
        keys = [k for k in (keys or []) if k]
        if not keys:
            return

        # Only fit when the plot was empty before this add — preserves user's
        # zoom when adding signals to an already-populated plot.
        was_empty = not self.plot_panel.plotted_keys()
        batch = len(keys) > 1
        if batch:
            self.plot_panel.begin_batch_add()

        plotted = 0
        for key in keys:
            if self.add_signal_to_plot(key, fit=False):
                plotted += 1

        if batch:
            self.plot_panel.end_batch_add()
        elif plotted and was_empty:
            self.plot_panel.fit_to_window()

        if plotted:
            self._update_status(f'Plotted {plotted} signal(s)', 'Use Fit to Window, reorder, or export selected CSV')

    def add_signal_to_plot(self, key: str, fit: bool = True) -> bool:
        # Generated signals are calculated only from a completed measurement;
        # ordinary measurement signals may still use the existing live store.
        if self.calculated_signals.contains_key(key) and self.store is None:
            return False
        # Allow plotting from partial store while decoding is in progress
        active_store = self.store
        if active_store is None and self._worker is not None:
            active_store = getattr(self._worker, '_live_store', None)
        if not active_store:
            return False
        if self.calculated_signals.contains_key(key):
            series = self.calculated_signals.cached_series(key)
            if series is None:
                definition = self.calculated_signals.definition(key)
                if definition is not None:
                    self._queue_calculation(definition, "lazy", plot_after=True)
                return False
        else:
            series = active_store.get_series(key)
        if not series:
            self._log(f'Signal not found: {key}')
            return False
        # Only fit when the plot was empty before this add — preserves user's
        # zoom when adding signals to an already-populated plot.
        was_empty = not self.plot_panel.plotted_keys()
        self.plot_panel.add_series(key, series)
        if fit and was_empty:
            self.plot_panel.fit_to_window()
        return True

    def _store_time_bounds(self) -> tuple[float | None, float | None]:
        """Full measurement time span across every decoded signal (not just
        what's currently plotted) — used to clamp plot_finding's centered
        zoom window."""
        if not self.store:
            return None, None
        lo = hi = None
        for key in self.store.all_keys():
            series = self.store.get_series(key)
            if series is None or len(series.timestamps) == 0:
                continue
            ts_min, ts_max = float(min(series.timestamps)), float(max(series.timestamps))
            lo = ts_min if lo is None else min(lo, ts_min)
            hi = ts_max if hi is None else max(hi, ts_max)
        return lo, hi

    def clear_plot(self) -> None:
        self.plot_panel.clear_all()
        self._finding_plot_keys.clear()

    def plot_finding(self, finding) -> None:
        """Plot a diagnostic finding's signals and zoom to its time window."""
        if not self.store:
            return

        # Replace, don't accumulate: drop the previous finding's auto-plotted
        # signals before adding the new ones. Manually-added signals (outside
        # the tracked set) are untouched.
        if self._finding_plot_keys:
            self.plot_panel.remove_series_many(self._finding_plot_keys)
            self._finding_plot_keys.clear()

        keys = finding.plot_signals or finding.signals
        if keys:
            self.add_signals_to_plot(keys)
            self._finding_plot_keys.update(keys)

        t0, t1 = finding.time_window
        if t0 or t1:
            half = finding.context_window_s
            if not half:
                diag = getattr(self, '_diagnostics_window', None)
                engine = getattr(diag, 'engine', None)
                half = getattr(getattr(engine, 'domain_profile', None), 'context_window_s_default', None)
            if not half:
                half = 1.0
            dmin, dmax = self._store_time_bounds()
            self.plot_panel.center_on_time(t0, half, data_min=dmin, data_max=dmax)

    # Export formats, in the order shown in the picker. Add a new format by
    # appending one (label, file_filter, default_ext, handler) entry — the
    # handler returns True if a file was written, False if the user backed out.
    def _export_formats(self) -> list[tuple[str, str, str, object]]:
        return [
            ('CAN CSV',      'CSV Files (*.csv)',   '.csv',  self._write_export_csv),
            ('Excel (.xlsx)', 'Excel Files (*.xlsx)', '.xlsx', self._write_export_xlsx),
        ]

    def export_selected(self) -> None:
        series_items = self.plot_panel.plotted_series()
        if not series_items:
            QMessageBox.information(self, 'No plots', 'Plot one or more signals before exporting.')
            return

        formats = self._export_formats()
        choice = self._ask_export_format(formats)
        if choice is None:
            return
        label, file_filter, default_ext, handler = choice

        path, _ = QFileDialog.getSaveFileName(
            self, 'Export selected signals', f'selected_signals{default_ext}', file_filter,
        )
        if not path:
            return
        if not path.lower().endswith(default_ext):
            path += default_ext

        try:
            wrote = handler(series_items, path)
        except Exception as exc:
            QMessageBox.critical(self, 'Export failed', str(exc))
            return
        if wrote:
            self._log(f'Exported {label}: {path}')

    def _ask_export_format(self, formats):
        """Modal format picker (radio list); returns the chosen entry or None."""
        dlg = QDialog(self)
        dlg.setWindowTitle('Export')
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel('Choose an export format:'))
        group = QButtonGroup(dlg)
        for i, (label, *_rest) in enumerate(formats):
            rb = QRadioButton(label, dlg)
            if i == 0:
                rb.setChecked(True)
            group.addButton(rb, i)
            layout.addWidget(rb)
        buttons = QHBoxLayout()
        export_btn = QPushButton('Export')
        cancel_btn = QPushButton('Cancel')
        export_btn.clicked.connect(dlg.accept)
        cancel_btn.clicked.connect(dlg.reject)
        buttons.addStretch(1)
        buttons.addWidget(export_btn)
        buttons.addWidget(cancel_btn)
        layout.addLayout(buttons)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return formats[group.checkedId()]

    def _write_export_csv(self, series_items, path) -> bool:
        ExportService.export_series_to_csv(series_items, path)
        return True

    def _write_export_xlsx(self, series_items, path) -> bool:
        data_rows = ExportService.count_data_rows(series_items)
        max_data_rows = None
        if data_rows + 1 > ExportService.EXCEL_MAX_ROWS:
            cap = ExportService.EXCEL_MAX_ROWS - 1
            answer = QMessageBox.warning(
                self, 'Row limit exceeded',
                f'The data has {data_rows:,} rows, but Excel supports at most '
                f'{ExportService.EXCEL_MAX_ROWS:,} rows per sheet (including the '
                f'header).\n\nContinue and keep only the first {cap:,} rows, or cancel?',
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Ok:
                return False
            max_data_rows = cap
        ExportService.export_series_to_excel(series_items, path, max_data_rows=max_data_rows)
        return True

    # ── Shortcuts dialog ──────────────────────────────────────────────────

    # Single source-of-truth for all keyboard shortcuts
    _SHORTCUTS: list[tuple[str, str]] = [
        ('F',               'Fit to Window — rescale X and Y to all data'),
        ('V',               'Fit Vertical — rescale Y only (keep current X)'),
        ('Space',           'Plot selected signal(s) from the signal tree'),
        ('C',               'Change color of the selected signal'),
        ('R',               'Toggle Cursor 1 and Cursor 2 on/off together'),
        ('Delete',          'Remove selected signal from plot'),
        ('Ctrl + Z',        'Undo last plot action (up to 3 levels)'),
        ('Ctrl + S',        'Save current configuration to JSON'),
        ('Ctrl + Shift + R','Open Raw CAN Frame viewer (BLF / ASC only)'),
    ]

    def show_shortcuts(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle('Keyboard Shortcuts')
        dlg.resize(560, 340)
        tbl = QTableWidget(len(self._SHORTCUTS), 2, dlg)
        tbl.setHorizontalHeaderLabels(['Shortcut', 'Action'])
        tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tbl.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        tbl.setAlternatingRowColors(True)
        for row, (key, desc) in enumerate(self._SHORTCUTS):
            tbl.setItem(row, 0, QTableWidgetItem(key))
            tbl.setItem(row, 1, QTableWidgetItem(desc))
        layout = QVBoxLayout(dlg)
        layout.addWidget(tbl)
        close_btn = QPushButton('Close')
        close_btn.clicked.connect(dlg.accept)
        layout.addWidget(close_btn)
        dlg.exec()

    def _on_worker_progress(self, message: str) -> None:
        self._log(message)
        self._update_status(message, 'Wait for decode to finish')

    def _on_tree_update(self, payload: dict) -> None:
        """Show available signals in tree while decoding is still running."""
        self.signal_tree.set_payload(payload)

    def _on_partial_ready(self) -> None:
        """Refresh any live-plotted curves with new samples decoded so far."""
        if self.store is None and self._worker is not None:
            # Store is being built by the worker — get reference via worker
            pass   # curves hold direct series references — just redraw
        self.plot_panel.refresh_plotted_curves()

    def _on_worker_finished(self, store: SignalStore) -> None:
        self.store = store
        self._update_action_states()
        # Expose store immediately so pending plots and post-decode plots work
        self.signal_tree.set_payload(store.build_tree_payload())
        self._refresh_generated_signal_tree()
        self.diagnostics_box.setPlainText(store.diagnostics_text)
        self._update_measurement_tab(
            channels=store.channel_summary_text(),
            frames=f'{store.total_frames:,}',
            decoded=f'{store.decoded_frames:,}',
            samples=f'{store.total_samples:,}',
        )
        self._log('Decode finished successfully.')
        if self._pending_plot_keys:
            wanted       = list(self._pending_plot_keys)
            colors       = dict(self._pending_plot_colors)
            visible      = dict(getattr(self, '_pending_plot_visible',      {}))
            groups       = dict(getattr(self, '_pending_plot_groups',       {}))
            axis_visible = dict(getattr(self, '_pending_plot_axis_visible', {}))
            own_axis     = dict(getattr(self, '_pending_plot_own_axis',     {}))
            self._pending_plot_keys = []
            self.add_signals_to_plot(wanted)
            for key, color in colors.items():
                self.plot_panel.set_series_color(key, color)
            # Restore visibility, group, and axis_visible from saved config
            needs_rebuild = False
            for key in wanted:
                if key in self.plot_panel._items:
                    if key in visible:
                        self.plot_panel._items[key].visible = visible[key]
                        needs_rebuild = True
                    if key in groups and groups[key]:
                        self.plot_panel._items[key].group = groups[key]
                        needs_rebuild = True
                    if key in axis_visible:
                        self.plot_panel._items[key].axis_visible = axis_visible[key]
                        needs_rebuild = True
                    if key in own_axis:
                        self.plot_panel._items[key].own_axis = own_axis[key]
                        needs_rebuild = True
            if needs_rebuild:
                self.plot_panel._rebuild_curves(preserve_selection=False)
            waiting_generated = {
                key for key in wanted
                if self.calculated_signals.contains_key(key)
                and key not in self.plot_panel._items
            }
            self._pending_plot_colors = {
                key: value for key, value in colors.items() if key in waiting_generated
            }
            self._pending_plot_visible = {
                key: value for key, value in visible.items() if key in waiting_generated
            }
            self._pending_plot_groups = {
                key: value for key, value in groups.items() if key in waiting_generated
            }
            self._pending_plot_axis_visible = {
                key: value for key, value in axis_visible.items() if key in waiting_generated
            }
            self._pending_plot_own_axis = {
                key: value for key, value in own_axis.items() if key in waiting_generated
            }
        self._update_status('Decode complete', 'Select signal(s) and plot them by double-click, right-click, drag, or Space.')

    def _on_worker_failed(self, error_message: str) -> None:
        self._log(f'ERROR: {error_message}')
        QMessageBox.critical(self, 'Load failed', error_message)
        self._update_status('Load failed', 'Review the log, verify BLF/DBC paths, and try again')

    def _cleanup_worker(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        if self._thread is not None:
            self._thread.deleteLater()
            self._thread = None

    def _on_plot_selection_changed(self, key: str) -> None:
        self._update_status(f'Selected plot: {key}', 'Delete removes selected plot rows; drag rows to reorder them')

    def _toggle_left_panel(self) -> None:
        self.left_dock.setVisible(not self.left_dock.isVisible())
        self._sync_panel_toggle_buttons()

    def _toggle_bottom_panel(self) -> None:
        self.bottom_dock.setVisible(not self.bottom_dock.isVisible())
        self._sync_panel_toggle_buttons()

    def _sync_panel_toggle_buttons(self) -> None:
        left_visible = self.left_dock.isVisible()
        bottom_visible = self.bottom_dock.isVisible()
        self.left_toggle_btn.setText('◀' if left_visible else '▶')
        self.bottom_toggle_btn.setText('▼' if bottom_visible else '▲')
        self.left_edge_btn.setText('◀' if left_visible else '▶')
        self.bottom_edge_btn.setText('▼' if bottom_visible else '▲')
        self._position_panel_toggle_buttons()

    def _position_panel_toggle_buttons(self) -> None:
        left_w = self.left_edge_btn.width() or 18
        left_h = max(self.left_edge_btn.sizeHint().height(), 36)
        x = 2
        y = max(80, (self.height() - left_h) // 2)
        self.left_edge_btn.setGeometry(x, y, left_w, left_h)
        self.left_edge_btn.raise_()

        btn_w = max(self.bottom_edge_btn.sizeHint().width(), 36)
        btn_h = self.bottom_edge_btn.height() or 18
        bottom_h = self.bottom_dock.height() if self.bottom_dock.isVisible() else 0
        y = self.height() - self.statusBar().height() - bottom_h - btn_h - 2
        y = max(80, y)
        x = max(40, (self.width() - btn_w) // 2)
        self.bottom_edge_btn.setGeometry(x, y, btn_w, btn_h)
        self.bottom_edge_btn.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._position_panel_toggle_buttons()

    def _on_background_color_changed(self, color: str) -> None:
        self._log(f'Plot background color changed: {color}')

    def _on_signal_color_changed(self, key: str, color: str) -> None:
        self._log(f'Signal color changed: {key} -> {color}')

    def _set_ready_status(self) -> None:
        self._update_status('Ready', "Click 'Open File' to load BLF / ASC / MF4 / MDF / CSV.")

    def _update_status(self, state: str, next_step: str) -> None:
        self.status_state_label.setText(f'State: {state}')
        self.status_next_step_label.setText(f'Next: {next_step}')
        self.plot_panel.set_status_overlay(f'State: {state}', f'Next: {next_step}')

    # Actions enabled/disabled per app state
    # needs_file  = requires measurement file to be selected
    # needs_store = requires decode to have completed
    _ACTS_ALWAYS_ENABLED = {'Load Config'}
    _ACTS_NEEDS_FILE  = {'Load + Decode'}
    _ACTS_NEEDS_STORE = {'Save Config', 'New Signal', 'Export', 'Clear Plots'}

    def _update_action_states(self) -> None:
        """Grey out toolbar actions that are not yet usable."""
        has_file  = bool(self.measurement_path or self.blf_path)
        has_store = self.store is not None
        for name, act in self._toolbar_actions.items():
            if name in self._ACTS_ALWAYS_ENABLED:
                act.setEnabled(True)
            elif name in self._ACTS_NEEDS_STORE:
                act.setEnabled(has_store)
            elif name in self._ACTS_NEEDS_FILE:
                act.setEnabled(has_file)
        if self._calc_thread is not None and 'New Signal' in self._toolbar_actions:
            for name in (
                'New Signal', 'Load + Decode',
                'Save Config', 'Export', 'Clear Plots',
            ):
                self._toolbar_actions[name].setEnabled(False)
            # Open File and Load Config remain available while calculating.
        # Keep CAN Trace discoverable after every load. For decoded-only MDF or
        # CSV data the action explains why an authentic raw trace is unavailable.
        _rfs = getattr(self.store, 'raw_frame_store', None) if self.store else None
        has_trace = has_store and _rfs is not None and len(_rfs) > 0
        if hasattr(self, '_act_can_trace'):
            self._act_can_trace.setEnabled(has_store)
            self._act_can_trace.setToolTip(
                'Open raw CAN Trace'
                if has_trace
                else 'Show CAN Trace availability information'
            )

    def _next_step_message(self) -> str:
        mpath = self.measurement_path or self.blf_path
        if not mpath:
            return "Click 'Open File' to load BLF / ASC / MF4 / MDF / CSV."
        if dbc_required_for(mpath) and self.channel_config.is_empty():
            suffix = Path(mpath).suffix.lower()
            if suffix in ('.mf4', '.mdf'):
                return "MDF bus log detected — database required. Click 'Open Database' to configure."
            return "Database required — click 'Open Database' to configure channel mapping."
        return "Click 'Load + Decode', then select signal(s) to plot."

    def _update_measurement_tab(self, channels: str = '', frames: str = '', decoded: str = '', samples: str = '') -> None:
        mpath = self.measurement_path or self.blf_path
        lines = [
            f'File: {mpath or ""}',
            f'Database:  {self.dbc_path or "(not required)"}',
            f'Channels: {channels}',
            f'Frames: {frames}',
            f'Decoded Frames: {decoded}',
            f'Samples: {samples}',
        ]
        self.measurement_box.setPlainText('\n'.join(lines))

    def _log(self, message: str) -> None:
        self.log_box.append(message)
        try:
            with self._log_file_path.open('a', encoding='utf-8') as fh:
                fh.write(message + '\n')
        except Exception:
            pass
