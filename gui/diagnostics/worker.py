"""
Background workers for the Diagnostics window so the UI stays responsive.

Two worker types:

* :class:`AnalysisWorker` — runs the rule engine on a SignalStore.
* :class:`LLMWorker`      — streams a chat completion from GitHub Models.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Signal


# ── Rule-engine worker ──────────────────────────────────────────────────

class AnalysisWorker(QObject):
    progress = Signal(str)
    finished = Signal(object)   # AnalysisResult
    failed   = Signal(str)

    def __init__(self, engine, store, domain_name: str) -> None:
        super().__init__()
        self.engine = engine
        self.store = store
        self.domain_name = domain_name

    def run(self) -> None:
        try:
            result = self.engine.run(
                self.store, self.domain_name,
                progress=lambda msg: self.progress.emit(msg),
            )
            self.finished.emit(result)
        except Exception as exc:                     # pragma: no cover
            self.failed.emit(str(exc))


# ── LLM streaming worker ────────────────────────────────────────────────

class LLMWorker(QObject):
    chunk    = Signal(str)
    finished = Signal()
    failed   = Signal(str)

    def __init__(self, client, messages: list[dict], model: str | None = None) -> None:
        super().__init__()
        self.client = client
        self.messages = messages
        self.model = model

    def run(self) -> None:
        try:
            for piece in self.client.chat_stream(self.messages, model=self.model):
                self.chunk.emit(piece)
            self.finished.emit()
        except Exception as exc:                     # pragma: no cover
            self.failed.emit(str(exc))


# ── Helper: spawn a worker on a fresh QThread, auto-clean on finish ─────

def run_worker(parent: QObject, worker: QObject) -> QThread:
    """Move *worker* to a new QThread, connect its lifetime, start it."""
    thread = QThread(parent)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)

    def _quit_thread(*args, **kwargs):
        thread.quit()
    # Connect any finished/failed signal to thread.quit
    if hasattr(worker, "finished"):
        worker.finished.connect(_quit_thread)
    if hasattr(worker, "failed"):
        worker.failed.connect(_quit_thread)

    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.start()
    return thread
