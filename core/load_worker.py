from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from core.readers import reader_factory
from core.readers.base import MeasurementReader
from core.signal_store import SignalStore

# ── CAN-raw streaming constants (counts = CAN frames) ─────────────────────
_CAN_TREE_INTERVAL     = 2_000
_CAN_PLOT_INTERVAL     = 5_000
_CAN_PROGRESS_INTERVAL = 10_000

# ── Pre-decoded bulk streaming constants (counts = channels, not samples) ─
# MDF/CSV: each emit happens once per channel batch, not per sample.
# Tree update after every channel; plot refresh every 10 channels.
_BULK_PLOT_INTERVAL    = 10     # channels between plot refreshes
_BULK_PROGRESS_INTERVAL = 50   # channels between progress log lines


class LoadWorker(QObject):
    """
    Background worker that reads any supported measurement file and
    populates a :class:`SignalStore`.

    Two decode paths:
    * **CAN-raw** (BLF, ASC) — frame-by-frame with DBC decode
    * **Bulk array** (MF4, MDF, CSV) — one numpy array per channel via
      ``iter_channel_arrays()`` + ``store.add_series_bulk()``.
      No per-sample Python objects; ~10–20× faster than the old sample loop.

    Elapsed time is logged to the progress/diagnostic output after decode.
    """

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
        t_start = time.perf_counter()
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

            # ── CAN-raw path (BLF / ASC) ──────────────────────────────────
            if reader.has_raw_frames and hasattr(reader, "iter_with_frames"):
                self._run_can_raw(reader, store)

            # ── Bulk array path (MF4 / MDF / CSV with channel arrays) ─────
            elif hasattr(reader, "iter_channel_arrays"):
                self._run_bulk_array(reader, store)

            # ── Fallback sample-by-sample path ────────────────────────────
            else:
                self._run_sample_loop(reader, store)

            # ── Elapsed time ──────────────────────────────────────────────
            elapsed = time.perf_counter() - t_start
            elapsed_str = (
                f"{elapsed:.1f} s" if elapsed < 60
                else f"{elapsed/60:.1f} min ({elapsed:.0f} s)"
            )
            self.progress.emit(
                f"Completed | signals: {len(store._series_by_key):,} | "
                f"samples: {store.total_samples:,} | "
                f"elapsed: {elapsed_str}"
            )
            # Append elapsed to diagnostics so it appears in the Diagnostics tab
            store.diagnostics_text += f"\n\nFile load time: {elapsed_str}"

            self.progress.emit(store.channel_summary_text())
            self.finished.emit(store)

        except Exception as exc:
            import traceback
            elapsed = time.perf_counter() - t_start
            self.failed.emit(
                f"{exc}\n\n{traceback.format_exc()}\n\nFailed after {elapsed:.1f} s"
            )

    # ── CAN-raw (BLF / ASC) ───────────────────────────────────────────────

    def _run_can_raw(self, reader, store: SignalStore) -> None:
        self.progress.emit("Opening measurement file and starting decode...")
        base_ts: float | None = None
        index = 0

        for frame, samples in reader.iter_with_frames():
            index += 1
            if base_ts is None:
                base_ts = frame.timestamp
                store.base_ts = base_ts
            frame.timestamp -= base_ts
            for s in samples:
                s.timestamp -= base_ts

            store.note_frame(frame)
            if samples:
                store.add_samples_direct(samples)
                store.add_raw_frame(frame, samples)
            else:
                store.unmatched_frames += 1

            if index % _CAN_TREE_INTERVAL == 0:
                self.tree_update.emit(store.build_tree_payload())
            if index % _CAN_PLOT_INTERVAL == 0:
                self.partial_ready.emit()
            if index == 1 or index % _CAN_PROGRESS_INTERVAL == 0:
                self.progress.emit(
                    f"Processed {index:,} frames | "
                    f"signals: {len(store._series_by_key):,} | "
                    f"samples: {store.total_samples:,}"
                )

        store.normalize_timestamps(already_normalized=True)
        decoder = getattr(reader, "decoder", None)
        dbc_diag = decoder.diagnostics_text() if decoder else ""
        store.diagnostics_text = (
            store.channel_summary_text()
            + "\n\nFirst frame IDs seen in file:\n"
            + ("\n".join(store.first_frame_ids) if store.first_frame_ids else "(none)")
            + ("\n\n" + dbc_diag if dbc_diag else "")
        )

    # ── Bulk array fast path (MF4 / MDF / CSV) ────────────────────────────

    def _run_bulk_array(self, reader, store: SignalStore) -> None:
        """
        Process channels as numpy arrays — no per-sample Python objects.

        Each channel is a bulk insert:  array.array.frombytes(ndarray.tobytes())
        which is a single C memcopy.  For a 500-channel MF4 with 10k samples
        per channel this is ~50× faster than the old per-sample loop.

        Streaming:
        - Tree update after EVERY channel (instant signal discovery)
        - Plot refresh every _BULK_PLOT_INTERVAL channels
        - Progress log every _BULK_PROGRESS_INTERVAL channels
        """
        import numpy as np

        self.progress.emit("Reading channels (vectorised fast path)...")
        base_ts: float | None = None
        ch_count = 0

        for (grp_name, ch_name, unit), ts_arr, num_arr, disp_list in \
                reader.iter_channel_arrays():

            ch_count += 1
            n = len(ts_arr)
            if n == 0:
                continue

            # ── Inline timestamp normalisation ────────────────────────────
            if base_ts is None:
                base_ts = float(ts_arr[0])
                store.base_ts = base_ts
            # Vectorised subtraction — single numpy C call
            ts_norm = ts_arr - base_ts

            # ── Bulk insert into SignalStore ───────────────────────────────
            store.add_series_bulk(
                channel=None,
                message_name=grp_name,
                message_id=0,
                signal_name=ch_name,
                unit=unit,
                timestamps=ts_norm,
                values=num_arr,
                raw_values=disp_list,
            )

            # ── Streaming ─────────────────────────────────────────────────
            # Tree update every channel (cheap: just updates dict payload)
            self.tree_update.emit(store.build_tree_payload())
            if ch_count % _BULK_PLOT_INTERVAL == 0:
                self.partial_ready.emit()
            if ch_count == 1 or ch_count % _BULK_PROGRESS_INTERVAL == 0:
                self.progress.emit(
                    f"Loaded {ch_count:,} channels | "
                    f"samples: {store.total_samples:,}"
                )

        # Final tree + plot refresh
        self.tree_update.emit(store.build_tree_payload())
        self.partial_ready.emit()

        store.normalize_timestamps(already_normalized=True)
        store.diagnostics_text = (
            store.channel_summary_text()
            + f"\n\nSource: {reader.source_description}"
            + f"\n\nChannels loaded: {ch_count:,}"
            + "\n\nNo raw frames — pre-decoded format."
        )

    # ── Fallback sample-by-sample (CSV narrow without channel arrays) ─────

    def _run_sample_loop(self, reader, store: SignalStore) -> None:
        self.progress.emit("Reading pre-decoded signals...")
        base_ts: float | None = None
        base_ts_set = False
        index = 0

        for sample in reader:
            index += 1
            if not base_ts_set:
                base_ts = sample.timestamp
                store.base_ts = base_ts
                base_ts_set = True
            sample.timestamp -= base_ts
            store.add_samples_direct([sample])

            if index % _CAN_TREE_INTERVAL == 0:
                self.tree_update.emit(store.build_tree_payload())
            if index % _CAN_PLOT_INTERVAL == 0:
                self.partial_ready.emit()
            if index == 1 or index % _CAN_PROGRESS_INTERVAL == 0:
                self.progress.emit(
                    f"Processed {index:,} records | "
                    f"samples: {store.total_samples:,}"
                )

        store.normalize_timestamps(already_normalized=True)
        store.diagnostics_text = (
            store.channel_summary_text()
            + f"\n\nSource: {reader.source_description}"
            + "\n\nNo raw frames — pre-decoded format."
        )
