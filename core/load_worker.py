from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from core.readers import reader_factory
from core.readers.base import MeasurementReader
from core.signal_store import SignalStore

# ── Streaming constants ───────────────────────────────────────────────────
_TREE_EMIT_INTERVAL = 2_000
_PLOT_EMIT_INTERVAL = 5_000
_PROGRESS_INTERVAL  = 10_000


class LoadWorker(QObject):
    progress      = Signal(str)
    finished      = Signal(object)
    failed        = Signal(str)
    tree_update   = Signal(dict)
    partial_ready = Signal()

    def __init__(
        self,
        measurement_path: str | Path,
        dbc_path: str | Path | None = None,
    ) -> None:
        super().__init__()
        self._measurement_path = str(measurement_path)
        self._dbc_path = str(dbc_path) if dbc_path else None

    @Slot()
    def run(self) -> None:
        try:
            self.progress.emit("Initialising reader...")
            reader: MeasurementReader = reader_factory(
                self._measurement_path, self._dbc_path
            )
            self.progress.emit(f"Source: {reader.source_description}")
            for msg in getattr(reader, "load_messages", []):
                self.progress.emit(msg)

            store = SignalStore()
            self._live_store = store

            base_ts: float | None = None
            index = 0

            # ── CAN-raw path (BLF / ASC) ──────────────────────────────────
            if reader.has_raw_frames and hasattr(reader, "iter_with_frames"):
                self.progress.emit("Opening measurement file and starting decode...")
                for frame, samples in reader.iter_with_frames():
                    index += 1
                    if base_ts is None:
                        base_ts = frame.timestamp
                        store.base_ts = base_ts
                    frame.timestamp -= base_ts
                    # samples were decoded inside the reader before base_ts was
                    # known — correct their timestamps here in the same pass
                    for s in samples:
                        s.timestamp -= base_ts

                    store.note_frame(frame)
                    if samples:
                        store.add_samples_direct(samples)
                        store.add_raw_frame(frame, samples)
                    else:
                        store.unmatched_frames += 1

                    self._emit_streaming(store, index)
                    self._emit_progress(store, index)

                store.normalize_timestamps(already_normalized=True)
                decoder = getattr(reader, "decoder", None)
                dbc_diag = decoder.diagnostics_text() if decoder else ""
                store.diagnostics_text = (
                    store.channel_summary_text()
                    + "\n\nFirst frame IDs seen in file:\n"
                    + ("\n".join(store.first_frame_ids) if store.first_frame_ids else "(none)")
                    + ("\n\n" + dbc_diag if dbc_diag else "")
                )

            # ── Pre-decoded path (MF4 / MDF / CSV) ───────────────────────
            else:
                self.progress.emit("Reading pre-decoded signals...")
                base_ts_set = False
                for sample in reader:
                    index += 1
                    if not base_ts_set:
                        base_ts = sample.timestamp
                        store.base_ts = base_ts
                        base_ts_set = True
                    sample.timestamp -= base_ts
                    store.add_samples_direct([sample])
                    self._emit_streaming(store, index)
                    self._emit_progress(store, index)

                store.normalize_timestamps(already_normalized=True)
                store.diagnostics_text = (
                    store.channel_summary_text()
                    + f"\n\nSource: {reader.source_description}"
                    + "\n\nNo raw frames — pre-decoded format."
                )

            self.progress.emit(store.channel_summary_text())
            self.progress.emit(
                f"Completed | records: {index:,} | "
                f"signals: {len(store._series_by_key):,} | "
                f"samples: {store.total_samples:,}"
            )
            self.finished.emit(store)

        except Exception as exc:
            import traceback
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")

    def _emit_streaming(self, store: SignalStore, index: int) -> None:
        if index % _TREE_EMIT_INTERVAL == 0:
            self.tree_update.emit(store.build_tree_payload())
        if index % _PLOT_EMIT_INTERVAL == 0:
            self.partial_ready.emit()

    def _emit_progress(self, store: SignalStore, index: int) -> None:
        if index == 1 or index % _PROGRESS_INTERVAL == 0:
            self.progress.emit(
                f"Processed {index:,} records | "
                f"signals: {len(store._series_by_key):,} | "
                f"samples: {store.total_samples:,}"
            )
