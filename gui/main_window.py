from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QThread, Qt
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
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
from core.load_worker import LoadWorker
from core.readers import dbc_required_for, ALL_SUFFIXES
from core.channel_config import ChannelConfig, ALL_CHANNELS_KEY
from gui.dbc_manager import DBCManagerDialog
from core.signal_store import SignalStore
from gui.plot_widget import PlotPanel
from gui.signal_tree import SignalTreeWidget
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
        self._thread: QThread | None = None
        self._worker: LoadWorker | None = None
        self._pending_plot_keys: list[str] = []
        self._pending_plot_colors: dict[str, str] = {}
        self._pending_plot_visible: dict[str, bool] = {}
        self._pending_plot_groups:  dict[str, str]  = {}
        self._raw_frame_dialog = None
        self._log_file_path = Path(__file__).resolve().parents[1] / 'canscope_dev.log'

        self._splash_status('Initialising plot panel...')
        self._build_ui()
        self._splash_status('Building toolbar...')
        self._build_toolbar()
        self._build_shortcuts()
        self._update_action_states()  # grey-out on startup
        self._set_ready_status()
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
        self.plot_panel.selectionChanged.connect(self._on_plot_selection_changed)
        self.plot_panel.signalDropped.connect(self.add_signals_to_plot)
        self.plot_panel.backgroundColorChanged.connect(self._on_background_color_changed)
        self.plot_panel.signalColorChanged.connect(self._on_signal_color_changed)

        button_row = QWidget()
        button_layout = QHBoxLayout(button_row)
        button_layout.setContentsMargins(0, 0, 0, 0)
        self.btn_fit = QPushButton('Fit to Window')
        self.btn_fit_v = QPushButton('Fit Vertical')
        self.btn_move_up = QPushButton('Move Up')
        self.btn_move_down = QPushButton('Move Down')
        self.btn_remove = QPushButton('Remove Selected')
        self.btn_multi_axis = QPushButton('Multi-Axis')
        self.btn_multi_axis.setCheckable(True)
        self.btn_stacked = QPushButton('Stacked')
        self.btn_stacked.setCheckable(True)
        self.btn_cursor1 = QPushButton('Cursor 1')
        self.btn_cursor1.setCheckable(True)
        self.btn_cursor1.setChecked(False)  # OFF by default
        self.btn_cursor2 = QPushButton('Cursor 2')
        self.btn_cursor2.setCheckable(True)
        self.btn_points = QPushButton('Show Data Points')
        self.btn_points.setCheckable(True)
        for btn in (self.btn_fit, self.btn_fit_v, self.btn_move_up, self.btn_move_down, self.btn_remove, self.btn_multi_axis, self.btn_stacked, self.btn_cursor1, self.btn_cursor2, self.btn_points):
            button_layout.addWidget(btn)
        button_layout.addStretch(1)

        self.btn_fit.clicked.connect(self.plot_panel.fit_to_window)
        self.btn_fit_v.clicked.connect(self.plot_panel.fit_vertical)
        self.btn_move_up.clicked.connect(self.plot_panel.move_selected_up)
        self.btn_move_down.clicked.connect(self.plot_panel.move_selected_down)
        self.btn_remove.clicked.connect(self.plot_panel.remove_selected_series)
        self.btn_multi_axis.toggled.connect(self._toggle_multi_axis)
        self.btn_stacked.toggled.connect(self._toggle_stacked)
        self.btn_cursor1.toggled.connect(self._toggle_cursor1)
        self.btn_cursor2.toggled.connect(self._toggle_cursor2)
        self.btn_points.toggled.connect(self._toggle_points)

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
        self.status_next_step_label = QLabel('Next: Open BLF, then Open DBC, then Load + Decode')
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
            ('Open DBC',     self.choose_dbc),
            ('Load + Decode',self.load_data),
            ('Save Config',  self.save_configuration),
            ('Load Config',  self.load_configuration),
            ('Export CSV',   self.export_selected_csv),
            ('Clear Plots',  self.plot_panel.clear_all),
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
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, activated=lambda: self.add_signals_to_plot(self.signal_tree.selected_signal_keys()))
        QShortcut(QKeySequence('Ctrl+Up'), self, activated=self.plot_panel.move_selected_up)
        # Raw Frames hidden from GUI to prevent hang on large files — accessible via shortcut
        QShortcut(QKeySequence('Ctrl+Shift+R'), self, activated=self.show_raw_frames)
        QShortcut(QKeySequence('Ctrl+Down'), self, activated=self.plot_panel.move_selected_down)
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
            self._log('DBC not required for this format.')
        self._update_measurement_tab()
        self._update_action_states()
        self._update_status(
            'Measurement file selected', self._next_step_message()
        )

    def _collect_channel_data(self) -> tuple[list[int], dict[int, set[int]]]:
        """
        Collect CAN channel numbers and per-channel arbitration ID sets
        from all available sources.  Used by the DBC Manager on open and
        on Refresh Match.
        """
        ids_per_channel: dict[int, set[int]] = {}
        channels_in_file: list[int] = []
        rfs = getattr(self.store, 'raw_frame_store', None) if self.store else None
        if rfs is not None and len(rfs) > 0:
            import numpy as np
            chs  = np.frombuffer(rfs.channels, dtype=np.uint8)
            aids = np.frombuffer(rfs.arb_ids,  dtype=np.uint32)
            for ch_val in np.unique(chs):
                if ch_val == 255:
                    continue
                ch_int = int(ch_val)
                channels_in_file.append(ch_int)
                ids_per_channel[ch_int] = set(int(x) for x in aids[chs == ch_val])
        if not channels_in_file and self.store is not None:
            for ch in sorted(self.store.channels):
                if ch is not None:
                    channels_in_file.append(int(ch))
        return channels_in_file, ids_per_channel

    def choose_dbc(self) -> None:
        """Open the DBC Manager dialog to assign DBCs to channels."""
        channels_in_file, ids_per_channel = self._collect_channel_data()

        dlg = DBCManagerDialog(
            channel_config   = self.channel_config,
            channels_in_file = channels_in_file,
            ids_per_channel  = ids_per_channel,
            parent           = self,
            data_provider    = self._collect_channel_data,
        )
        if dlg.exec() != DBCManagerDialog.DialogCode.Accepted:
            return

        self.channel_config = dlg.result_config()
        paths = self.channel_config.all_dbc_paths()
        self.dbc_path = paths[0] if paths else None
        self._log(self.channel_config.summary())
        self._update_measurement_tab()
        self._update_action_states()
        self._update_status('DBC configured', self._next_step_message())

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

    def show_raw_frames(self) -> None:
        rfs = getattr(self.store, 'raw_frame_store', None) if self.store else None
        if not rfs or len(rfs) == 0:
            QMessageBox.information(self, 'No raw frames', 'Load and decode a BLF/ASC file first.')
            return
        self._raw_frame_dialog = RawFrameDialog(rfs, self)
        self._raw_frame_dialog.show()
        self._raw_frame_dialog.raise_()
        self._raw_frame_dialog.activateWindow()

    def _toggle_points(self, checked: bool) -> None:
        self.plot_panel.set_show_points(checked)
        self.btn_points.setText('Hide Data Points' if checked else 'Show Data Points')
        self._update_status('Plot markers updated', 'Continue plotting, fit view, or save configuration')

    def save_configuration(self) -> None:
        config = {
            'version': self.version,
            'blf_path': self.measurement_path or self.blf_path,
            'dbc_path': self.dbc_path,  # legacy single-DBC
            'channel_config': {
                'name': self.channel_config.name,
                'channels': {str(k): v for k, v in self.channel_config.channels.items()},
            },
            'signals': [
                {
                    'key':     k,
                    'visible': self.plot_panel._items[k].visible,
                    'group':   self.plot_panel._items[k].group,
                }
                for k in self.plot_panel.plotted_keys()
            ],
            'show_data_points': self.btn_points.isChecked(),
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
            self._update_status('Configuration saved', 'Load it later to reopen BLF, DBC, and plotted signals')
        except Exception as exc:
            QMessageBox.critical(self, 'Save configuration failed', str(exc))
            self._update_status('Save failed', 'Check path permissions and try again')

    def load_configuration(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, 'Load configuration', '', 'JSON Files (*.json)')
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8'))
        except Exception as exc:
            QMessageBox.critical(self, 'Load configuration failed', str(exc))
            return

        cfg_blf = data.get('blf_path')
        # measurement_path is the canonical name; blf_path kept for config backward-compat
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
        signals_data    = list(data.get('signals') or [])
        pending_keys    = []
        pending_visible = {}
        pending_groups  = {}
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
        pending_colors = dict(data.get('signal_colors') or {})

        # Fix 6: if data is already decoded, ask the user what to do
        use_current_data = False
        if self.store is not None:
            reply = QMessageBox.question(
                self,
                'Data already loaded',
                'A BLF/DBC is already decoded in memory.\n\n'
                'What would you like to do?\n\n'
                '  Yes — keep current data, plot signals from config\n'
                '  No  — reload BLF/DBC paths from configuration file',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            use_current_data = (reply == QMessageBox.StandardButton.Yes)

        # Apply visual settings regardless of data source
        self.btn_points.setChecked(bool(data.get('show_data_points', False)))
        bg = data.get('plot_background_color')
        if bg:
            self.plot_panel.set_background_color(str(bg))
        self.btn_multi_axis.setChecked(bool(data.get('multi_axis', False)))
        self.btn_stacked.setChecked(bool(data.get('stacked', False)))
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
            if needs_rebuild:
                self.plot_panel._rebuild_curves(preserve_selection=False)
            return

        # Reload from config BLF/DBC paths
        self.measurement_path = cfg_blf
        self.blf_path = cfg_blf   # alias
        self.dbc_path = cfg_dbc
        self._pending_plot_keys    = pending_keys
        self._pending_plot_colors  = pending_colors
        self._pending_plot_visible = pending_visible
        self._pending_plot_groups  = pending_groups
        self._update_measurement_tab()
        if not self.blf_path or not self.dbc_path:
            QMessageBox.warning(self, 'Incomplete configuration', 'The configuration file does not contain both BLF and DBC paths.')
            return
        self._log(f'Configuration loaded: {path}')
        self.load_data(pending_plot_keys=self._pending_plot_keys)

    def load_data(self, pending_plot_keys: list[str] | None = None) -> None:
        mpath = self.measurement_path or self.blf_path
        if not mpath:
            QMessageBox.warning(self, 'Missing file', 'Please select a measurement file first.')
            self._update_status('Waiting for input', self._next_step_message())
            return
        # DBC required only for CAN-raw formats
        if dbc_required_for(mpath) and self.channel_config.is_empty():
            reply = QMessageBox.question(
                self, 'No DBC configured',
                'This format requires a DBC for signal decoding.\n'
                'No DBC is configured yet.\n\n'
                'Open DBC Manager now?',
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

        batch = len(keys) > 1
        if batch:
            self.plot_panel.begin_batch_add()

        plotted = 0
        for key in keys:
            if self.add_signal_to_plot(key, fit=False):
                plotted += 1

        if batch:
            self.plot_panel.end_batch_add()
        elif plotted:
            self.plot_panel.fit_to_window()

        if plotted:
            self._update_status(f'Plotted {plotted} signal(s)', 'Use Fit to Window, reorder, or export selected CSV')

    def add_signal_to_plot(self, key: str, fit: bool = True) -> bool:
        # Allow plotting from partial store while decoding is in progress
        active_store = self.store
        if active_store is None and self._worker is not None:
            active_store = getattr(self._worker, '_live_store', None)
        if not active_store:
            return False
        series = active_store.get_series(key)
        if not series:
            self._log(f'Signal not found: {key}')
            return False
        self.plot_panel.add_series(key, series)
        if fit:
            self.plot_panel.fit_to_window()
        return True

    def export_selected_csv(self) -> None:
        series_items = self.plot_panel.plotted_series()
        if not series_items:
            QMessageBox.information(self, 'No plots', 'Plot one or more signals before exporting CSV.')
            return
        path, _ = QFileDialog.getSaveFileName(self, 'Export selected signals', 'selected_signals.csv', 'CSV Files (*.csv)')
        if not path:
            return
        try:
            ExportService.export_series_to_csv(series_items, path)
            self._log(f'Exported CSV: {path}')
        except Exception as exc:
            QMessageBox.critical(self, 'Export failed', str(exc))

    # ── Shortcuts dialog ──────────────────────────────────────────────────

    # Single source-of-truth for all keyboard shortcuts
    _SHORTCUTS: list[tuple[str, str]] = [
        ('F',               'Fit to Window — rescale X and Y to all data'),
        ('V',               'Fit Vertical — rescale Y only (keep current X)'),
        ('Space',           'Plot selected signal(s) from the signal tree'),
        ('Delete',          'Remove selected signal from plot'),
        ('Ctrl + Z',        'Undo last plot action (up to 3 levels)'),
        ('Ctrl + S',        'Save current configuration to JSON'),
        ('Ctrl + Up',       'Move selected signal up in the plot list'),
        ('Ctrl + Down',     'Move selected signal down in the plot list'),
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
        self.diagnostics_box.setPlainText(store.diagnostics_text)
        self._update_measurement_tab(
            channels=store.channel_summary_text(),
            frames=f'{store.total_frames:,}',
            decoded=f'{store.decoded_frames:,}',
            samples=f'{store.total_samples:,}',
        )
        self._log('Decode finished successfully.')
        if self._pending_plot_keys:
            wanted  = list(self._pending_plot_keys)
            colors  = dict(self._pending_plot_colors)
            visible = dict(getattr(self, '_pending_plot_visible', {}))
            groups  = dict(getattr(self, '_pending_plot_groups',  {}))
            self._pending_plot_keys = []
            self.add_signals_to_plot(wanted)
            for key, color in colors.items():
                self.plot_panel.set_series_color(key, color)
            # Restore visibility and group from saved config
            needs_rebuild = False
            for key in wanted:
                if key in self.plot_panel._items:
                    if key in visible:
                        self.plot_panel._items[key].visible = visible[key]
                        needs_rebuild = True
                    if key in groups and groups[key]:
                        self.plot_panel._items[key].group = groups[key]
                        needs_rebuild = True
            if needs_rebuild:
                self.plot_panel._rebuild_curves(preserve_selection=False)
            self._pending_plot_colors  = {}
            self._pending_plot_visible = {}
            self._pending_plot_groups  = {}
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
        self._update_status(f'Selected plot: {key}', 'Delete removes selected plot rows; Ctrl+Up/Down reorders them')

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
    _ACTS_NEEDS_FILE  = {'Load + Decode'}
    _ACTS_NEEDS_STORE = {'Save Config', 'Export CSV', 'Clear Plots'}

    def _update_action_states(self) -> None:
        """Grey out toolbar actions that are not yet usable."""
        has_file  = bool(self.measurement_path or self.blf_path)
        has_store = self.store is not None
        for name, act in self._toolbar_actions.items():
            if name in self._ACTS_NEEDS_STORE:
                act.setEnabled(has_store)
            elif name in self._ACTS_NEEDS_FILE:
                act.setEnabled(has_file)
            # else: Open File, Load Config — always enabled
        # CAN Trace button requires raw frames (BLF/ASC with store)
        _rfs = getattr(self.store, 'raw_frame_store', None) if self.store else None
        has_trace = has_store and _rfs is not None and len(_rfs) > 0
        if hasattr(self, '_act_can_trace'):
            self._act_can_trace.setEnabled(has_trace)

    def _next_step_message(self) -> str:
        mpath = self.measurement_path or self.blf_path
        if not mpath:
            return "Click 'Open File' to load BLF / ASC / MF4 / MDF / CSV."
        if dbc_required_for(mpath) and self.channel_config.is_empty():
            suffix = Path(mpath).suffix.lower()
            if suffix in ('.mf4', '.mdf'):
                return "MDF bus log detected — DBC required. Click 'Open DBC' to configure."
            return "DBC required — click 'Open DBC' to configure channel mapping."
        return "Click 'Load + Decode', then select signal(s) to plot."

    def _update_measurement_tab(self, channels: str = '', frames: str = '', decoded: str = '', samples: str = '') -> None:
        mpath = self.measurement_path or self.blf_path
        lines = [
            f'File: {mpath or ""}',
            f'DBC:  {self.dbc_path or "(not required)"}',
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
