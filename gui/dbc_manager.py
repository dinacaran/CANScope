"""
DBC Manager dialog — assign DBC files to CAN channels.

Layout:

    ┌──────────────────────────────────────────────────────────────────┐
    │  DBC Manager                                              [Name: Truck ECU Setup] │
    ├─────────────────────┬──────────────────┬────────────────────────┤
    │  DBC File           │  Assigned to     │  Match  (IDs in file)  │
    ├─────────────────────┼──────────────────┼────────────────────────┤
    │  Powertrain.dbc [×] │  [CAN 1       ▼] │  ████████░░  91%  34/37│
    │  Chassis.dbc    [×] │  [CAN 2       ▼] │  ███████░░░  78%  21/27│
    │                     │                  │                        │
    │  [+ Add DBC…]       │                  │                        │
    ├─────────────────────┴──────────────────┴────────────────────────┤
    │  [Save Channel Config…]   [Load Channel Config…]   [OK] [Cancel]│
    └──────────────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QProgressBar, QScrollArea,
    QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from core.channel_config import ChannelConfig, ALL_CHANNELS_KEY


class _DBCRow(QWidget):
    """One row in the DBC manager: filename label + channel dropdown + match bar + remove btn."""

    def __init__(
        self,
        dbc_path: str,
        channels: list[int],          # available channel numbers from measurement
        assigned_to: int,             # initially assigned channel
        match_pct: float,             # 0.0–1.0 match quality
        match_text: str,              # e.g. "34 / 37 IDs"
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.dbc_path = dbc_path

        # File name label
        name_lbl = QLabel(Path(dbc_path).name)
        name_lbl.setToolTip(dbc_path)
        name_lbl.setMinimumWidth(180)
        name_lbl.setMaximumWidth(260)

        # Channel dropdown
        self.channel_combo = QComboBox()
        self.channel_combo.addItem("All Channels", ALL_CHANNELS_KEY)
        for ch in sorted(channels):
            self.channel_combo.addItem(f"CAN {ch}", ch)
        # Select initial assignment
        idx = self.channel_combo.findData(assigned_to)
        self.channel_combo.setCurrentIndex(max(0, idx))
        self.channel_combo.setFixedWidth(130)

        # Match quality bar (stored as instance attrs for later refresh)
        self._bar = QProgressBar()
        self._bar.setMinimum(0)
        self._bar.setMaximum(100)
        self._bar.setValue(int(match_pct * 100))
        self._bar.setFixedWidth(120)
        self._bar.setFixedHeight(16)
        self._bar.setTextVisible(False)
        bar_style = (
            "QProgressBar { border: 1px solid #4a6080; border-radius: 3px;"
            "  background: #1a2a3a; }"
            "QProgressBar::chunk { background: #3a8060; border-radius: 2px; }"
        )
        self._bar.setStyleSheet(bar_style)
        bar = self._bar   # alias for layout

        self._match_lbl = QLabel(f"{int(match_pct*100)}%  {match_text}")
        self._match_lbl.setStyleSheet("color: #8ab4a0; font-size: 11px;")
        self._match_lbl.setFixedWidth(110)
        match_lbl = self._match_lbl   # alias for layout

        # Remove button
        rm_btn = QToolButton()
        rm_btn.setText("×")
        rm_btn.setFixedSize(22, 22)
        rm_btn.setStyleSheet(
            "QToolButton { color: #c07070; border: 1px solid #6a4040;"
            "  border-radius: 3px; background: #2a1a1a; }"
            "QToolButton:hover { background: #4a2020; }"
        )
        rm_btn.clicked.connect(self._on_remove)

        row = QHBoxLayout(self)
        row.setContentsMargins(4, 2, 4, 2)
        row.addWidget(name_lbl)
        row.addWidget(self.channel_combo)
        row.addWidget(bar)
        row.addWidget(match_lbl)
        row.addStretch()
        row.addWidget(rm_btn)

    def assigned_channel(self) -> int:
        return int(self.channel_combo.currentData())

    def update_match(self, pct: float, text: str) -> None:
        """Refresh the match quality bar and label."""
        self._bar.setValue(int(pct * 100))
        self._match_lbl.setText(f'{int(pct*100)}%  {text}')

    def _on_remove(self) -> None:
        # Signal parent scroll container to remove this row
        parent = self.parent()
        while parent and not isinstance(parent, DBCManagerDialog):
            parent = parent.parent()
        if isinstance(parent, DBCManagerDialog):
            parent.remove_row(self)


class DBCManagerDialog(QDialog):
    """
    DBC Manager — assign DBC files to CAN channels.

    Parameters
    ----------
    channel_config : ChannelConfig
        Current config (may be empty on first use).
    channels_in_file : list[int]
        Channel numbers seen in the currently loaded measurement.
    ids_per_channel : dict[int, set[int]]
        Arbitration IDs seen per channel — used to compute match quality.
    parent : QWidget | None
    """

    def __init__(
        self,
        channel_config: ChannelConfig,
        channels_in_file: list[int],
        ids_per_channel: dict[int, set[int]],
        parent=None,
        data_provider=None,
    ) -> None:
        """
        Parameters
        ----------
        data_provider : callable() -> (list[int], dict[int, set[int]]) | None
            Called by Refresh Match to get fresh channel/ID data from the
            main window.  Allows refresh to work even if the measurement
            was decoded after the dialog was opened.
        """
        super().__init__(parent)
        self.setWindowTitle("DBC Manager — Channel Configuration")
        self.setMinimumWidth(720)
        self.resize(760, 380)

        self._channels_in_file = sorted(channels_in_file) if channels_in_file else []
        self._ids_per_channel  = ids_per_channel
        self._data_provider    = data_provider  # callable for refresh
        self._rows: list[_DBCRow] = []

        # ── Config name ───────────────────────────────────────────────────
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Config name:"))
        self._name_edit = QLineEdit(channel_config.name)
        self._name_edit.setPlaceholderText("e.g. Truck ECU Setup")
        name_row.addWidget(self._name_edit, 1)

        # ── Rows container ────────────────────────────────────────────────
        self._rows_widget = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_widget)
        self._rows_layout.setSpacing(2)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._rows_widget)
        scroll.setMinimumHeight(150)

        # Column headers
        hdr = QHBoxLayout()
        for text, width in [("DBC File", 190), ("Assigned to", 140),
                             ("Match quality", 240), ("", 30)]:
            lbl = QLabel(text)
            lbl.setFixedWidth(width)
            lbl.setStyleSheet("color: #8090a0; font-size: 11px;")
            hdr.addWidget(lbl)
        hdr.addStretch()

        # Channel info banner
        self._channel_info = QLabel()
        self._channel_info.setStyleSheet("color: #a0a060; font-size: 11px; padding: 2px 4px;")
        self._update_channel_info()

        # Populate existing rows
        for ch, path in channel_config.channels.items():
            self._add_row(path, preferred_channel=ch)
        # If we have measurement data, immediately show real match quality
        if ids_per_channel:
            self._refresh_all_matches()

        # ── Add DBC button ────────────────────────────────────────────────
        add_btn = QPushButton("+ Add DBC…")
        add_btn.clicked.connect(self._on_add_dbc)
        add_btn.setFixedWidth(110)

        refresh_btn = QPushButton("↻ Refresh Match")
        refresh_btn.clicked.connect(self._refresh_all_matches)
        refresh_btn.setFixedWidth(120)
        refresh_btn.setToolTip(
            'Re-compute match quality for all DBC rows\n'
            '(useful after loading a measurement file)'
        )

        add_row = QHBoxLayout()
        add_row.addWidget(add_btn)
        add_row.addWidget(refresh_btn)
        add_row.addStretch()

        # ── Bottom buttons ────────────────────────────────────────────────
        save_ch_btn = QPushButton("Save Channel Config…")
        save_ch_btn.clicked.connect(self._on_save_channel_config)
        load_ch_btn = QPushButton("Load Channel Config…")
        load_ch_btn.clicked.connect(self._on_load_channel_config)

        bbox = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        bbox.accepted.connect(self.accept)
        bbox.rejected.connect(self.reject)

        btn_row = QHBoxLayout()
        btn_row.addWidget(save_ch_btn)
        btn_row.addWidget(load_ch_btn)
        btn_row.addStretch()
        btn_row.addWidget(bbox)

        # ── Main layout ───────────────────────────────────────────────────
        main = QVBoxLayout(self)
        main.addLayout(name_row)
        main.addWidget(self._channel_info)
        main.addLayout(hdr)
        main.addWidget(scroll, 1)
        main.addLayout(add_row)
        main.addLayout(btn_row)

    # ── Channel info ────────────────────────────────────────────────────

    def _update_channel_info(self) -> None:
        if self._channels_in_file:
            ch_list = ", ".join(f"CAN {c}" for c in self._channels_in_file)
            self._channel_info.setText(
                f"Channels detected in measurement: {ch_list}"
            )
            self._channel_info.setStyleSheet(
                "color: #80b0a0; font-size: 11px; padding: 2px 4px;"
            )
        else:
            self._channel_info.setText(
                "No measurement loaded — open a measurement file first to detect channels"
            )
            self._channel_info.setStyleSheet(
                "color: #b0a060; font-size: 11px; padding: 2px 4px;"
            )

    # ── Public result ─────────────────────────────────────────────────────

    def result_config(self) -> ChannelConfig:
        """Return the ChannelConfig as configured by the user."""
        cfg = ChannelConfig(name=self._name_edit.text().strip() or "Unnamed")
        for row in self._rows:
            ch  = row.assigned_channel()
            path = row.dbc_path
            # Last assignment wins if two rows assign the same channel
            cfg.channels[ch] = path
        return cfg

    # ── Row management ────────────────────────────────────────────────────

    def _add_row(self, dbc_path: str, preferred_channel: int | None = None) -> None:
        """Add a DBC row, compute match quality, auto-suggest channel."""
        pct, text, best_ch = self._compute_match(dbc_path)

        # Use preferred_channel if given, otherwise auto-suggest best match
        assigned = preferred_channel if preferred_channel is not None else best_ch

        row = _DBCRow(
            dbc_path=dbc_path,
            channels=self._channels_in_file,
            assigned_to=assigned,
            match_pct=pct,
            match_text=text,
            parent=self,
        )
        self._rows.append(row)
        self._rows_layout.addWidget(row)

    def remove_row(self, row: _DBCRow) -> None:
        self._rows.remove(row)
        self._rows_layout.removeWidget(row)
        row.deleteLater()

    # ── Match quality computation ─────────────────────────────────────────

    def _refresh_all_matches(self) -> None:
        """
        Recompute match quality for every DBC row.
        First calls data_provider() to fetch fresh channel/ID data
        so the refresh works even if decode finished after dialog opened.
        """
        # Pull fresh data from the main window
        if self._data_provider is not None:
            try:
                fresh_channels, fresh_ids = self._data_provider()
                if fresh_channels:
                    self._ids_per_channel = fresh_ids
                    # Update channel dropdowns to include any newly seen channels
                    new_chs = sorted(set(fresh_channels) - set(self._channels_in_file))
                    if new_chs:
                        self._channels_in_file = sorted(
                            set(self._channels_in_file) | set(new_chs)
                        )
                        for row in self._rows:
                            for ch in new_chs:
                                if row.channel_combo.findData(ch) < 0:
                                    row.channel_combo.addItem(f'CAN {ch}', ch)
                        self._update_channel_info()
            except Exception:
                pass

        if not self._ids_per_channel:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(
                self, 'No measurement data',
                'Load and decode a BLF/ASC measurement first,\n'
                'then click Refresh Match to see match quality.'
            )
            return

        for row in self._rows:
            pct, text, best_ch = self._compute_match(row.dbc_path)
            row.update_match(pct, text)
            # Auto-suggest best channel if user hasn't changed it
            if row.assigned_channel() == ALL_CHANNELS_KEY:
                idx = row.channel_combo.findData(best_ch)
                if idx >= 0:
                    row.channel_combo.setCurrentIndex(idx)

    def _compute_match(self, dbc_path: str) -> tuple[float, str, int]:
        """
        Return (pct, label_text, best_channel_int) for a DBC file.

        Matching strategy (match quality display only — decode stays exact):
        1. Exact 29-bit ID match
        2. J1939 PGN fallback — strip source address byte for extended frames
           where PF >= 0xF0 (PDU2 format, PS = destination, SA = last byte).
           E.g. DBC has 0x18FEBD00, file has 0x18FEBDFE — same PGN 0xFEBD.

        This gives realistic match percentages for J1939 files where ECUs
        broadcast on different source addresses than the DBC template value.
        """
        try:
            import cantools
            db = cantools.database.load_file(dbc_path, strict=False)
            dbc_ids = {int(m.frame_id) & 0x1FFFFFFF for m in db.messages}
        except Exception:
            return 0.0, "can't read DBC", ALL_CHANNELS_KEY

        if not dbc_ids or not self._ids_per_channel:
            return 0.0, "0 / 0 IDs", ALL_CHANNELS_KEY

        best_ch    = ALL_CHANNELS_KEY
        best_count = 0
        best_total = len(dbc_ids)

        # Pre-compute PGN sets for DBC (J1939 extended frames only)
        # PGN for PDU2 (PF >= 0xF0): bits 8-23 of the 29-bit ID
        def _pgn(fid: int) -> int | None:
            pf = (fid >> 16) & 0xFF
            if fid > 0x7FF and pf >= 0xF0:
                return (fid >> 8) & 0xFFFF
            return None

        dbc_pgns = {_pgn(fid) for fid in dbc_ids} - {None}

        for ch, file_ids in self._ids_per_channel.items():
            file_ids_norm = {fid & 0x1FFFFFFF for fid in file_ids}
            file_pgns     = {_pgn(fid) for fid in file_ids_norm} - {None}

            # Pass 1: exact ID match
            exact_hits = len(dbc_ids & file_ids_norm)

            # Pass 2: PGN fallback for IDs that didn't match exactly
            unmatched_dbc_pgns = {
                _pgn(fid) for fid in (dbc_ids - file_ids_norm)
                if _pgn(fid) is not None
            }
            pgn_hits = len(unmatched_dbc_pgns & file_pgns)

            hits = exact_hits + pgn_hits
            if hits > best_count:
                best_count = hits
                best_ch    = ch

        pct  = best_count / best_total if best_total else 0.0
        text = f"{best_count} / {best_total} IDs"
        return pct, text, best_ch

    # ── Slots ─────────────────────────────────────────────────────────────

    def _on_add_dbc(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add DBC file(s)", "",
            "DBC files (*.dbc);;All files (*)"
        )
        for path in paths:
            self._add_row(path)

    def _on_save_channel_config(self) -> None:
        cfg = self.result_config()
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Channel Config", f"{cfg.name}.canscope_ch",
            f"Channel Config (*{ChannelConfig.FILE_EXTENSION});;All files (*)"
        )
        if not path:
            return
        try:
            cfg.save(path)
            QMessageBox.information(self, "Saved",
                                    f"Channel config saved:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))

    def _on_load_channel_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Channel Config", "",
            f"Channel Config (*{ChannelConfig.FILE_EXTENSION});;All files (*)"
        )
        if not path:
            return
        try:
            cfg = ChannelConfig.load(path)
        except Exception as exc:
            QMessageBox.critical(self, "Load failed", str(exc))
            return

        # Clear existing rows and repopulate
        for row in list(self._rows):
            self.remove_row(row)
        self._name_edit.setText(cfg.name)
        for ch, dbc_path in cfg.channels.items():
            self._add_row(dbc_path, preferred_channel=ch)
