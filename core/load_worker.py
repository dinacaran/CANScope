from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from core.readers import reader_factory
from core.readers.base import MeasurementReader
from core.signal_store import SignalStore
from core.raw_frame_store import RawFrameStore
from core.channel_config import ChannelConfig, ALL_CHANNELS_KEY

# ── Streaming constants ───────────────────────────────────────────────────
_CAN_TREE_INTERVAL     = 2_000
_CAN_PLOT_INTERVAL     = 5_000
_CAN_PROGRESS_INTERVAL = 10_000
_BULK_PLOT_INTERVAL    = 10
_BULK_PROGRESS_INTERVAL = 50


class LoadWorker(QObject):
    """
    Background worker that reads any measurement file and populates a SignalStore.

    Accepts a ChannelConfig (multi-DBC) or a single dbc_path string (legacy).
    The channel config maps CAN channel numbers to DBCDecoder instances.
    A frame on CAN 1 is decoded with CAN-1's decoder; a frame on CAN 2 with
    CAN-2's decoder; if no channel-specific decoder exists, the All-Channels
    fallback is used.
    """

    progress      = Signal(str)
    finished      = Signal(object)
    failed        = Signal(str)
    tree_update   = Signal(dict)
    partial_ready = Signal()

    def __init__(
        self,
        measurement_path: str | Path,
        channel_config: ChannelConfig | str | Path | None = None,
    ) -> None:
        super().__init__()
        self._measurement_path = str(measurement_path)

        # Accept ChannelConfig directly or legacy single-DBC path string
        if isinstance(channel_config, ChannelConfig):
            self._channel_config: ChannelConfig | None = channel_config
        elif channel_config:
            self._channel_config = ChannelConfig.from_single_dbc(str(channel_config))
        else:
            self._channel_config = None

        # Legacy compatibility: first DBC path (for reader_factory)
        self._dbc_path: str | None = None
        if self._channel_config:
            paths = self._channel_config.all_dbc_paths()
            if paths:
                self._dbc_path = paths[0]

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

            if self._channel_config:
                self.progress.emit(self._channel_config.summary())

            store = SignalStore()
            self._live_store = store

            if reader.has_raw_frames and hasattr(reader, "iter_with_frames"):
                self._run_can_raw(reader, store)
            elif hasattr(reader, "iter_channel_arrays"):
                self._run_bulk_array(reader, store)
            else:
                self._run_sample_loop(reader, store)

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
            store.diagnostics_text += f"\n\nFile load time: {elapsed_str}"
            self.progress.emit(store.channel_summary_text())
            self.finished.emit(store)

        except Exception as exc:
            import traceback
            elapsed = time.perf_counter() - t_start
            self.failed.emit(
                f"{exc}\n\n{traceback.format_exc()}\n\nFailed after {elapsed:.1f} s"
            )

    # ── CAN-raw path ──────────────────────────────────────────────────────

    def _run_can_raw(self, reader, store: SignalStore) -> None:
        self.progress.emit("Opening measurement file and starting decode...")
        base_ts: float | None = None
        index = 0

        rfs = RawFrameStore()
        store.raw_frame_store = rfs

        # Pre-build all decoders so the first frame pays no parse cost
        cfg = self._channel_config
        if cfg:
            cfg.build_all_decoders()

        for frame, _legacy_samples in reader.iter_with_frames():
            index += 1
            if base_ts is None:
                base_ts = frame.timestamp
                store.base_ts = base_ts
            frame.timestamp -= base_ts

            store.note_frame(frame)

            # ── Multi-DBC decode: pick decoder by channel ─────────────────
            if cfg:
                decoder = cfg.decoder_for(frame.channel)
                if decoder:
                    samples = decoder.decode_frame(frame)
                else:
                    samples = []
            else:
                # No DBC at all — raw frames only
                samples = []

            # Normalise sample timestamps
            for s in samples:
                s.timestamp -= base_ts

            # Store every frame (decoded or not)
            rfs.append(
                timestamp   = frame.timestamp,
                channel     = frame.channel,
                arb_id      = frame.arbitration_id,
                dlc         = frame.dlc,
                direction   = frame.direction,
                is_extended = frame.is_extended_id,
                is_fd       = frame.is_fd,
                data        = frame.data,
                frame_name  = samples[0].message_name if samples else '',
                decoded     = bool(samples),
            )
            if samples:
                store.add_samples_direct(samples)
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

        rfs.seal()

        # Attach the first available decoder to rfs for on-demand signal decode
        if cfg:
            rfs.decoder = cfg.decoder_for(None)  # All-channels or first
            rfs.channel_config = cfg              # full config for per-channel decode

        # Diagnostics
        diag_parts = [store.channel_summary_text()]
        diag_parts.append("\nFirst frame IDs seen in file:\n"
                          + ("\n".join(store.first_frame_ids)
                             if store.first_frame_ids else "(none)"))
        if cfg:
            for path in cfg.all_dbc_paths():
                from core.dbc_decoder import DBCDecoder
                dec = cfg._decoder_cache.get(path)
                if dec:
                    diag_parts.append(f"\n\n{dec.diagnostics_text()}")

        store.normalize_timestamps(already_normalized=True)
        store.diagnostics_text = "\n".join(diag_parts)

    # ── Bulk array path (MF4 / MDF / CSV) ────────────────────────────────

    def _run_bulk_array(self, reader, store: SignalStore) -> None:
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
            if base_ts is None:
                base_ts = float(ts_arr[0])
                store.base_ts = base_ts
            ts_norm = ts_arr - base_ts
            store.add_series_bulk(
                channel=None, message_name=grp_name, message_id=0,
                signal_name=ch_name, unit=unit,
                timestamps=ts_norm, values=num_arr, raw_values=disp_list,
            )
            self.tree_update.emit(store.build_tree_payload())
            if ch_count % _BULK_PLOT_INTERVAL == 0:
                self.partial_ready.emit()
            if ch_count == 1 or ch_count % _BULK_PROGRESS_INTERVAL == 0:
                self.progress.emit(
                    f"Loaded {ch_count:,} channels | samples: {store.total_samples:,}"
                )

        self.tree_update.emit(store.build_tree_payload())
        self.partial_ready.emit()
        store.normalize_timestamps(already_normalized=True)
        store.diagnostics_text = (
            store.channel_summary_text()
            + f"\n\nSource: {reader.source_description}"
            + f"\n\nChannels loaded: {ch_count:,}"
            + "\n\nNo raw frames — pre-decoded format."
        )

    # ── Fallback sample-by-sample ─────────────────────────────────────────

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
                    f"Processed {index:,} records | samples: {store.total_samples:,}"
                )
        store.normalize_timestamps(already_normalized=True)
        store.diagnostics_text = (
            store.channel_summary_text()
            + f"\n\nSource: {reader.source_description}"
            + "\n\nNo raw frames — pre-decoded format."
        )
