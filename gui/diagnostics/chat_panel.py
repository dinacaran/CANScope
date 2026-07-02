"""
Chat panel — streaming AI responses on the right side of the window.

Holds:
* A scrollable Markdown view of the conversation so far.
* A multi-line input at the bottom with a Send button.
* Streaming append: AI chunks arrive in real time via :meth:`append_chunk`.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QTextEdit, QVBoxLayout, QWidget,
    QPlainTextEdit,
)


class ChatPanel(QWidget):
    sendRequested = Signal(str)   # user typed a question + clicked Send

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self._history_view = QTextEdit()
        self._history_view.setReadOnly(True)
        self._history_view.setPlaceholderText(
            "AI diagnosis will stream here. Run an analysis first."
        )

        self._input = QPlainTextEdit()
        self._input.setPlaceholderText(
            "Ask a follow-up question about the findings… (Ctrl+Enter to send)"
        )
        self._input.setMaximumHeight(80)

        self._send_btn = QPushButton("Send")
        self._send_btn.clicked.connect(self._on_send)
        self._send_btn.setDefault(True)

        # Internal buffer for the in-flight AI message — accumulated chunks
        # are re-rendered as Markdown each time so formatting works while
        # streaming.
        self._pending_md = ""
        self._pending_active = False
        # Conversation history (excluding system messages) for follow-ups
        self._history: list[dict] = []

        self._status_label = QLabel("Ready.")
        self._status_label.setStyleSheet(
            "color: #808080; font-size: 11px; padding: 2px 6px;"
        )

        # ── layout ──
        input_row = QHBoxLayout()
        input_row.addWidget(self._input, 1)
        input_row.addWidget(self._send_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        header_row = QHBoxLayout()
        header = QLabel("AI Diagnosis")
        header.setStyleSheet("font-weight: bold; padding: 4px;")
        header_row.addWidget(header)
        header_row.addStretch()
        header_row.addWidget(self._status_label)
        layout.addLayout(header_row)
        layout.addWidget(self._history_view, 1)
        layout.addLayout(input_row)

    # ── public API ──────────────────────────────────────────────────────

    def set_ai_status(self, msg: str, color: str = "#808080") -> None:
        self._status_label.setText(msg)
        self._status_label.setStyleSheet(
            f"color: {color}; font-size: 11px; padding: 2px 6px;"
        )

    def clear(self) -> None:
        self._history_view.clear()
        self._history.clear()
        self._pending_md = ""
        self._pending_active = False
        self.set_ai_status("Ready.")

    def begin_assistant_message(self) -> None:
        """Mark the start of a new streaming assistant reply."""
        self._pending_md = ""
        self._pending_active = True
        self.set_ai_status("Waiting for AI…", "#a0b8d0")
        self._render()

    def append_chunk(self, text: str) -> None:
        """Append a streamed text chunk to the in-flight assistant message."""
        if not self._pending_active:
            self.begin_assistant_message()
        self._pending_md += text
        n = len(self._pending_md)
        self.set_ai_status(f"Streaming… {n:,} chars", "#60c080")
        self._render()

    def end_assistant_message(self) -> None:
        """Finalise the in-flight assistant message and append to history."""
        if self._pending_active and self._pending_md:
            self._history.append({"role": "assistant", "content": self._pending_md})
            self.set_ai_status(
                f"Done — {len(self._pending_md):,} chars.", "#60c060"
            )
        else:
            self.set_ai_status("No response received.", "#d08040")
        self._pending_active = False
        self._pending_md = ""
        self._render()

    def add_user_message(self, text: str) -> None:
        self._history.append({"role": "user", "content": text})
        self._render()

    def history(self) -> list[dict]:
        return list(self._history)

    def set_send_enabled(self, enabled: bool) -> None:
        self._send_btn.setEnabled(enabled)

    # ── internals ───────────────────────────────────────────────────────

    def _on_send(self) -> None:
        text = self._input.toPlainText().strip()
        if not text:
            return
        self._input.clear()
        self.sendRequested.emit(text)

    def keyPressEvent(self, ev) -> None:                 # pragma: no cover
        if (
            ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and (ev.modifiers() & Qt.KeyboardModifier.ControlModifier)
        ):
            self._on_send()
            return
        super().keyPressEvent(ev)

    def _render(self) -> None:
        """Render history + the in-flight pending assistant message as Markdown."""
        parts: list[str] = []
        for msg in self._history:
            role = msg["role"].upper()
            parts.append(f"**{role}**\n\n{msg['content']}\n\n---\n")
        if self._pending_active:
            parts.append(f"**ASSISTANT**\n\n{self._pending_md}")
        self._history_view.setMarkdown("\n".join(parts))
        self._history_view.verticalScrollBar().setValue(
            self._history_view.verticalScrollBar().maximum()
        )
