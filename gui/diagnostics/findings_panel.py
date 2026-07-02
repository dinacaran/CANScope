"""
Findings panel — list of detected anomalies on the left side of the window.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QTextEdit,
    QVBoxLayout, QWidget,
)

from core.diagnostics.models import AnalysisResult, Finding


class FindingsPanel(QWidget):
    """
    Top: header + count summary.
    Middle: severity-coloured list of findings.
    Bottom: details pane for the selected finding.
    """

    findingSelected = Signal(object)   # emits Finding | None

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self._header = QLabel("No analysis run yet.")
        self._header.setStyleSheet("font-weight: bold; padding: 4px;")

        self._list = QListWidget()
        self._list.itemSelectionChanged.connect(self._on_selection_changed)

        self._details = QTextEdit()
        self._details.setReadOnly(True)
        self._details.setPlaceholderText(
            "Select a finding above to view details."
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._header)
        layout.addWidget(self._list, 2)

        details_label = QLabel("Finding details:")
        details_label.setStyleSheet(
            "color: #8090a0; font-size: 11px; padding: 2px 4px;"
        )
        layout.addWidget(details_label)
        layout.addWidget(self._details, 1)

    # ── public API ──────────────────────────────────────────────────────

    def set_status(self, text: str) -> None:
        self._header.setText(text)

    def append_log(self, msg: str, color: str | None = None) -> None:
        """Append a line to the log list during analysis (before results arrive)."""
        item = QListWidgetItem(msg)
        item.setForeground(QBrush(QColor(color or "#a0b8d0")))
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        self._list.addItem(item)
        self._list.scrollToBottom()

    def show_result(self, result: AnalysisResult) -> None:
        self._list.clear()
        self._details.clear()

        if not result.findings:
            self._header.setText(
                f"{result.domain_name} — no faults detected "
                f"({result.duration_s:.2f}s, "
                f"{result.signal_count} signals scanned)."
            )
            return

        crit = result.critical_count()
        self._header.setText(
            f"{result.domain_name} — {len(result.findings)} finding(s), "
            f"{crit} critical • analysed in {result.duration_s:.2f}s"
        )

        for f in result.by_severity():
            item = self._build_item(f)
            self._list.addItem(item)

    def selected_finding(self) -> Finding | None:
        items = self._list.selectedItems()
        if not items:
            return None
        return items[0].data(Qt.ItemDataRole.UserRole)

    def clear(self) -> None:
        self._list.clear()
        self._details.clear()
        self._header.setText("No analysis run yet.")

    # ── internals ───────────────────────────────────────────────────────

    def _build_item(self, f: Finding) -> QListWidgetItem:
        label = f"[{f.severity.label():>8}]  {f.title}"
        item = QListWidgetItem(label)
        item.setData(Qt.ItemDataRole.UserRole, f)
        item.setForeground(QBrush(QColor(f.severity.colour())))
        item.setToolTip(f.description)
        return item

    def _on_selection_changed(self) -> None:
        f = self.selected_finding()
        self.findingSelected.emit(f)
        if f is None:
            self._details.clear()
            return
        self._details.setMarkdown(self._format_finding(f))

    @staticmethod
    def _format_finding(f: Finding) -> str:
        lines: list[str] = [
            f"## {f.title}",
            "",
            f"- **Severity**: {f.severity.label()}",
            f"- **Detector**: `{f.detector_name}`",
            f"- **Time window**: t={f.time_window[0]:.2f}s "
            f"– {f.time_window[1]:.2f}s",
        ]
        if f.signals:
            lines.append("- **Signals**: " + ", ".join(f"`{s}`" for s in f.signals))
        if f.metrics:
            lines.append("- **Metrics**:")
            for k, v in f.metrics.items():
                if isinstance(v, float):
                    lines.append(f"  - `{k}` = {v:g}")
                else:
                    lines.append(f"  - `{k}` = {v!r}")
        if f.description:
            lines.append("")
            lines.append(f.description)
        if f.llm_explanation:
            lines.append("")
            lines.append("### AI explanation")
            lines.append(f.llm_explanation)
        return "\n".join(lines)
