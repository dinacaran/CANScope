"""
Agent panel — the left-hand "Agent" tab of the diagnostics window.

Shows the closed-loop agent's live state (iteration counter, current step, a
step log) and the engineer approval gate (Approve / Edit / Skip) for each
AI-proposed rule.  The final root-cause / inconclusive report is streamed into
the shared chat panel on the right, not here.

This widget is pure UI: it emits intent signals and exposes setter methods that
the window drives from the event queue.  It never runs the loop itself.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)
from PySide6.QtGui import QBrush, QColor


class AgentPanel(QWidget):
    startRequested = Signal()
    stopRequested = Signal()
    # (action, edited_yaml) where action is "approve" | "skip"
    gateDecision = Signal(str, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        # ── controls row ──
        self._start_btn = QPushButton("▶ Start Agent")
        self._start_btn.clicked.connect(self.startRequested)
        self._stop_btn = QPushButton("■ Stop")
        self._stop_btn.clicked.connect(self.stopRequested)
        self._stop_btn.setEnabled(False)

        self._iter_label = QLabel("Iteration: —")
        self._iter_label.setStyleSheet("font-weight: bold; padding: 2px 6px;")

        controls = QHBoxLayout()
        controls.addWidget(self._start_btn)
        controls.addWidget(self._stop_btn)
        controls.addStretch()
        controls.addWidget(self._iter_label)

        # ── step label + log ──
        self._step_label = QLabel("Idle. Press Start to investigate the current domain.")
        self._step_label.setWordWrap(True)
        self._step_label.setStyleSheet("color: #a0b8d0; padding: 2px 6px;")

        self._log = QListWidget()
        self._log.setAlternatingRowColors(False)

        # ── gate box (hidden until a rule needs approval) ──
        self._gate_box = QGroupBox("Approve AI-proposed rule")
        self._gate_editor = QPlainTextEdit()
        self._gate_editor.setReadOnly(True)
        self._gate_editor.setPlaceholderText("Candidate rule YAML appears here for approval.")
        self._gate_editor.setMaximumHeight(200)

        self._approve_btn = QPushButton("Approve")
        self._approve_btn.clicked.connect(self._on_approve)
        self._edit_btn = QPushButton("Edit…")
        self._edit_btn.clicked.connect(self._on_edit)
        self._skip_btn = QPushButton("Skip")
        self._skip_btn.clicked.connect(self._on_skip)

        gate_btns = QHBoxLayout()
        gate_btns.addWidget(self._approve_btn)
        gate_btns.addWidget(self._edit_btn)
        gate_btns.addWidget(self._skip_btn)
        gate_btns.addStretch()

        gate_layout = QVBoxLayout(self._gate_box)
        gate_hint = QLabel("Review the rule. Approve to run it, Edit to change it first, or Skip.")
        gate_hint.setStyleSheet("color: #8090a0; font-size: 11px;")
        gate_layout.addWidget(gate_hint)
        gate_layout.addWidget(self._gate_editor)
        gate_layout.addLayout(gate_btns)
        self._gate_box.setVisible(False)

        # ── layout ──
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        header = QLabel("Closed-loop Agent")
        header.setStyleSheet("font-weight: bold; padding: 4px;")
        layout.addWidget(header)
        layout.addLayout(controls)
        layout.addWidget(self._step_label)
        layout.addWidget(self._log, 1)
        layout.addWidget(self._gate_box)

    # ── intent handlers ─────────────────────────────────────────────────
    def _on_approve(self) -> None:
        text = self._gate_editor.toPlainText()
        self._collapse_gate()
        self.gateDecision.emit("approve", text)

    def _on_edit(self) -> None:
        # Unlock the editor so the engineer can tweak the YAML, then Approve.
        self._gate_editor.setReadOnly(False)
        self._gate_editor.setFocus()
        self._step_label.setText("Editing rule — adjust the YAML, then click Approve.")

    def _on_skip(self) -> None:
        self._collapse_gate()
        self.gateDecision.emit("skip", "")

    def _collapse_gate(self) -> None:
        self._gate_box.setVisible(False)
        self._gate_editor.setReadOnly(True)

    # ── setters driven by the window's event queue ──────────────────────
    def set_running(self, running: bool) -> None:
        self._start_btn.setEnabled(not running)
        self._stop_btn.setEnabled(running)
        if running:
            self._log.clear()
            self._collapse_gate()

    def set_iteration(self, n: int, total: int) -> None:
        self._iter_label.setText(f"Iteration: {n}/{total}")

    def set_step(self, text: str, color: str = "#a0b8d0") -> None:
        self._step_label.setText(text)
        item = QListWidgetItem(text)
        item.setForeground(QBrush(QColor(color)))
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        self._log.addItem(item)
        self._log.scrollToBottom()

    def show_gate(self, yaml_text: str) -> None:
        self._gate_editor.setReadOnly(True)
        self._gate_editor.setPlainText(yaml_text)
        self._gate_box.setVisible(True)

    def hide_gate(self) -> None:
        self._collapse_gate()
