"""
Diagnostics window — separate top-level window combining:

* Domain selector + Run / Reload Rules / Edit Rules controls (top bar).
* Findings panel (left).
* Chat panel (right).

Non-modal: stays open while the user keeps using the main app.
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPushButton, QSplitter, QStatusBar, QToolBar, QVBoxLayout, QWidget,
)

from core.diagnostics import (
    AnalysisResult, ConfigError, DiagnosticEngine,
)
from core.diagnostics.llm import (
    GitHubModelsClient, LLMError,
    build_analysis_prompt, build_chat_followup_prompt,
)
from core.diagnostics.llm.client import DEFAULT_MODEL, list_models
from gui.diagnostics.chat_panel import ChatPanel
from gui.diagnostics.findings_panel import FindingsPanel
from gui.diagnostics.worker import run_worker


class DiagnosticsWindow(QMainWindow):
    """
    Standalone non-modal diagnostics window.

    Lifetime is owned by :class:`MainWindow` (stored on
    ``main_window._diagnostics_window``), so closing/reopening it via
    Ctrl+Shift+A reuses the same instance and preserves the chat history.
    """

    def __init__(self, store, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("CANScope Diagnostics")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.resize(1400, 800)

        self.store = store
        self.engine = DiagnosticEngine()
        self._llm_client: GitHubModelsClient | None = None
        self._latest_result: AnalysisResult | None = None

        self._build_ui()
        self._refresh_domains()

    # ── UI construction ─────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── top control bar ──
        self.domain_combo = QComboBox()
        self.domain_combo.setMinimumWidth(200)
        self.domain_combo.setToolTip("Pick a fault rule set")

        self.model_combo = QComboBox()
        for m in list_models():
            self.model_combo.addItem(m)
        idx = self.model_combo.findText(DEFAULT_MODEL)
        if idx >= 0:
            self.model_combo.setCurrentIndex(idx)
        self.model_combo.setToolTip("LLM model used for diagnosis")

        self.btn_run = QPushButton("Run Analysis")
        self.btn_run.setDefault(True)
        self.btn_run.clicked.connect(self._on_run)

        self.btn_reload = QPushButton("Reload Rules")
        self.btn_reload.clicked.connect(self._on_reload_rules)
        self.btn_reload.setToolTip(
            "Re-read all YAML files under config/diagnostics/ "
            "after editing rules"
        )

        self.btn_edit = QPushButton("Edit Rules…")
        self.btn_edit.clicked.connect(self._on_edit_rules)
        self.btn_edit.setToolTip(
            "Open the diagnostics config folder in your file manager"
        )

        top_bar = QHBoxLayout()
        top_bar.addWidget(QLabel("Domain:"))
        top_bar.addWidget(self.domain_combo)
        top_bar.addSpacing(12)
        top_bar.addWidget(QLabel("Model:"))
        top_bar.addWidget(self.model_combo)
        top_bar.addSpacing(12)
        top_bar.addWidget(self.btn_run)
        top_bar.addWidget(self.btn_reload)
        top_bar.addWidget(self.btn_edit)
        top_bar.addStretch()

        # ── splitter: findings | chat ──
        self.findings_panel = FindingsPanel()
        self.findings_panel.findingSelected.connect(self._on_finding_selected)
        self.chat_panel = ChatPanel()
        self.chat_panel.sendRequested.connect(self._on_chat_send)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.findings_panel)
        splitter.addWidget(self.chat_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([700, 700])

        # ── central layout ──
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addLayout(top_bar)
        layout.addWidget(splitter, 1)
        self.setCentralWidget(central)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Ready.")

        # Esc closes the window
        QShortcut(QKeySequence("Escape"), self, activated=self.close)

    # ── actions ─────────────────────────────────────────────────────────

    def _refresh_domains(self) -> None:
        self.domain_combo.clear()
        try:
            domains = self.engine.load_configs()
        except ConfigError as exc:
            QMessageBox.warning(self, "Config error", str(exc))
            return
        if not domains:
            self.domain_combo.addItem("(no rule files found)")
            self.domain_combo.setEnabled(False)
            self.btn_run.setEnabled(False)
            self.statusBar().showMessage(
                f"No YAML files in {self.engine.config_dir}"
            )
            return
        for d in domains:
            self.domain_combo.addItem(
                f"{d.name}  ({len(d.rules)} rules)", d.name
            )
        self.domain_combo.setEnabled(True)
        self.btn_run.setEnabled(True)
        self.statusBar().showMessage(
            f"Loaded {len(domains)} domain(s) from {self.engine.config_dir}"
        )

    def _on_reload_rules(self) -> None:
        try:
            self.engine.load_configs()
        except ConfigError as exc:
            QMessageBox.warning(self, "Config error", str(exc))
            return
        self._refresh_domains()
        self.statusBar().showMessage("Rules reloaded.")

    def _on_edit_rules(self) -> None:
        path = self.engine.config_dir
        path.mkdir(parents=True, exist_ok=True)
        # Cross-platform "open folder"
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))                  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])    # noqa: S603,S607
            else:
                subprocess.Popen(["xdg-open", str(path)])  # noqa: S603,S607
        except Exception as exc:
            QMessageBox.information(
                self, "Edit Rules",
                f"Open this folder manually:\n{path}\n\n({exc})"
            )

    def _on_run(self) -> None:
        domain_name = self.domain_combo.currentData()
        if not domain_name:
            return
        self.btn_run.setEnabled(False)
        self.findings_panel.clear()
        self.chat_panel.clear()
        self.findings_panel.set_status(f"Running rules for {domain_name}…")
        self.findings_panel.append_log(f"Domain: {domain_name}", "#a0b8d0")
        self._pending_domain = domain_name
        # One event-loop tick so the UI renders the log header before we block
        QTimer.singleShot(0, self._run_analysis)

    def _run_analysis(self) -> None:
        """Run the rule engine in the main thread.

        Avoids cross-thread Signal delivery issues that caused the UI to hang.
        processEvents() inside the progress callback renders each log line live.
        """
        domain_name = getattr(self, "_pending_domain", None)
        if not domain_name:
            return

        def _progress(msg: str) -> None:
            self.statusBar().showMessage(msg)
            if not msg.startswith("  "):
                self.findings_panel.set_status(msg)
            color = (
                "#60c060" if msg.startswith("  ✓") else
                "#d08040" if msg.startswith("  ✗") else
                "#ffd060" if msg.startswith("  ●") else
                "#808080" if msg.startswith("  ○") else
                "#a0b8d0"
            )
            self.findings_panel.append_log(msg, color)
            QApplication.processEvents()

        try:
            result = self.engine.run(self.store, domain_name, progress=_progress)
            self._on_analysis_finished(result)
        except Exception as exc:
            self._on_analysis_failed(str(exc))

    def _on_analysis_finished(self, result: AnalysisResult) -> None:
        self._latest_result = result
        self.findings_panel.show_result(result)
        self.btn_run.setEnabled(True)
        if result.findings:
            # Auto-plot the most severe finding immediately
            self._plot_finding_on_main(result.by_severity()[0])
            self.statusBar().showMessage(
                f"{len(result.findings)} finding(s) in {result.duration_s:.2f}s. "
                "Asking AI for root-cause analysis…"
            )
            self._kick_off_llm(result)
        else:
            self.statusBar().showMessage(
                f"No faults detected for {result.domain_name}."
            )

    def _on_finding_selected(self, finding) -> None:
        """Plot the clicked finding's signals in the main window."""
        if finding is None:
            return
        self._plot_finding_on_main(finding)

    def _plot_finding_on_main(self, finding) -> None:
        main = self.parent()
        if main is not None and hasattr(main, "plot_finding"):
            main.plot_finding(finding)

    def _on_analysis_failed(self, msg: str) -> None:
        self.btn_run.setEnabled(True)
        self.findings_panel.set_status(f"Analysis failed: {msg}")
        QMessageBox.critical(self, "Analysis failed", msg)

    # ── LLM calls ──────────────────────────────────────────────────────

    def _ensure_client(self) -> GitHubModelsClient | None:
        if self._llm_client is not None:
            return self._llm_client
        try:
            self._llm_client = GitHubModelsClient(
                model=self.model_combo.currentText() or DEFAULT_MODEL,
            )
        except LLMError as exc:
            QMessageBox.warning(self, "AI not available", str(exc))
            return None
        return self._llm_client

    def _kick_off_llm(self, result: AnalysisResult) -> None:
        client = self._ensure_client()
        if client is None:
            return

        manifest = self.engine.build_manifest(self.store, result.domain_name)
        messages = build_analysis_prompt(
            domain_name=result.domain_name,
            manifest=manifest,
            findings=result.findings,
        )
        # The first user message is the giant analysis prompt — record a
        # short version in the chat history so follow-ups stay coherent.
        self.chat_panel.add_user_message(
            f"Analyse {len(result.findings)} finding(s) for "
            f"domain '{result.domain_name}'."
        )
        self._stream_assistant(messages)

    def _on_chat_send(self, user_text: str) -> None:
        client = self._ensure_client()
        if client is None:
            return
        self.chat_panel.add_user_message(user_text)
        messages = build_chat_followup_prompt(
            history=self.chat_panel.history(),
            user_question=user_text,
            result=self._latest_result,
        )
        # build_chat_followup_prompt already includes the new user message
        # at the end; remove our just-added local copy from history payload
        # to avoid duplicate.
        if messages and messages[-1]["role"] == "user":
            pass
        self._stream_assistant(messages)

    def _stream_assistant(self, messages: list[dict]) -> None:
        client = self._ensure_client()
        if client is None:
            return
        model = self.model_combo.currentText() or None
        self.chat_panel.set_send_enabled(False)
        self.chat_panel.begin_assistant_message()
        self.statusBar().showMessage(f"Asking AI ({model or 'default'})…")

        result_q: queue.Queue = queue.Queue()

        def _llm_thread() -> None:
            try:
                for piece in client.chat_stream(messages, model=model):
                    result_q.put(("chunk", piece))
                result_q.put(("done", None))
            except Exception as exc:
                result_q.put(("error", str(exc)))

        threading.Thread(target=_llm_thread, daemon=True).start()

        poll_timer = QTimer(self)

        def _poll() -> None:
            while True:
                try:
                    kind, data = result_q.get_nowait()
                except queue.Empty:
                    break
                if kind == "chunk":
                    self.chat_panel.append_chunk(data)
                elif kind == "done":
                    poll_timer.stop()
                    poll_timer.deleteLater()
                    self._on_llm_done()
                    return
                elif kind == "error":
                    poll_timer.stop()
                    poll_timer.deleteLater()
                    self._on_llm_failed(data)
                    return

        poll_timer.timeout.connect(_poll)
        poll_timer.start(50)   # drain queue every 50 ms

    def _on_llm_done(self) -> None:
        self.chat_panel.end_assistant_message()
        self.chat_panel.set_send_enabled(True)
        self.statusBar().showMessage("AI diagnosis ready.")

    def _on_llm_failed(self, msg: str) -> None:
        self.chat_panel.append_chunk(f"\n\n**Error:** {msg}")
        self.chat_panel.end_assistant_message()
        self.chat_panel.set_send_enabled(True)
        self.chat_panel.set_ai_status(f"Error — see chat.", "#d04040")
        self.statusBar().showMessage(f"AI error: {msg}")
