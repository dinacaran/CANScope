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
_CAN_TREE_INTERVAL     = 10_000   # was 2 000 — build_tree_payload() sorts all signals; every 2k frames was ~250 rebuilds for 500k frames
_CAN_PLOT_INTERVAL     = 25_000   # was 5 000 — partial_ready triggers main-thread redraw; every 5k was 100 redraws
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

            if reader.has_raw_frames and hasattr(reader, "iter_frames_only"):
                # Two-pass vectorised decode: ~50× faster on cantools-heavy
                # files because the per-frame Python decode call is replaced
                # by numpy bit ops applied to entire arb-id groups at once.
                self._run_can_raw_vectorized(reader, store)
            elif reader.has_raw_frames and hasattr(reader, "iter_with_frames"):
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

    # ── CAN-raw path: 2-pass vectorised (preferred) ──────────────────────

    def _run_can_raw_vectorized(self, reader, store: SignalStore) -> None:
        """
        Two-pass loader for BLF/ASC streams.

        Pass 1 — drain every raw frame into the on-disk RawFrameStore with no
        decoding. python-can's BLF/ASC reader is the only Python work.

        Pass 2 — group frames by ``(channel, arbitration_id)`` and apply the
        vectorised DBC decoder once per group. Each group bulk-inserts via
        :meth:`SignalStore.add_series_bulk` (one C-level memcopy per signal).
        Compared to the old per-frame cantools decode, this skips ~50× of the
        Python work for the common little-endian integer-signal case.
        """
        import numpy as np
        from core.vectorized_decoder import VectorizedDBC

        cfg = self._channel_config
        if cfg:
            cfg.build_all_decoders()

        # Channel → decoder map (None = "All Channels" fallback)
        _decoder_map: dict[int | None, object | None] = {}
        if cfg:
            dec_cache = cfg._decoder_cache
            for ch_key, path in cfg.channels.items():
                actual_ch = None if ch_key == ALL_CHANNELS_KEY else ch_key
                _decoder_map[actual_ch] = dec_cache.get(path)

        # Choices lookup so SignalStore knows which signals carry display labels.
        choices_lookup: dict[tuple[int | None, str, str], dict] = {}
        for ch, decoder in _decoder_map.items():
            if decoder is None:
                continue
            for (msg_name, sig_name), choices in decoder._choices_cache.items():
                if choices:
                    choices_lookup[(ch, msg_name, sig_name)] = choices
        store.set_choices_lookup(choices_lookup)

        rfs = RawFrameStore()
        store.raw_frame_store = rfs

        # ── Pass 1: drain frames into raw_frame_store ────────────────────
        self.progress.emit("Reading frames (pass 1/2)...")
        base_ts: float | None = None
        index = 0
        rfs_append = rfs.append           # bind once for hot loop
        note_frame = store.note_frame

        for frame in reader.iter_frames_only():
            index += 1
            if base_ts is None:
                base_ts = frame.timestamp
                store.base_ts = base_ts
            frame.timestamp -= base_ts
            note_frame(frame)
            rfs_append(
                timestamp   = frame.timestamp,
                channel     = frame.channel,
                arb_id      = frame.arbitration_id,
                dlc         = frame.dlc,
                direction   = frame.direction,
                is_extended = frame.is_extended_id,
                is_fd       = frame.is_fd,
                data        = frame.data,
                frame_name  = '',
                decoded     = False,
            )
            if index == 1 or index % _CAN_PROGRESS_INTERVAL == 0:
                self.progress.emit(f"Reading frames: {index:,}...")

        rfs.seal()
        n = len(rfs)
        if n == 0:
            store.normalize_timestamps(already_normalized=True)
            store.diagnostics_text = (
                store.channel_summary_text() + "\n\n(no frames in file)"
            )
            return

        if rfs._mmap is None:
            self.progress.emit(
                "WARNING: mmap unavailable for raw-frame store; "
                "falling back to per-frame decode."
            )
            self._decode_fallback_in_place(rfs, store, _decoder_map)
            return

        # ── Pass 2: vectorised decode ───────────────────────────────────
        self.progress.emit(
            f"Read {n:,} frames. Decoding signals (pass 2/2)..."
        )

        timestamps_np = np.frombuffer(rfs.timestamps, dtype=np.float64)
        channels_np   = np.frombuffer(rfs.channels,   dtype=np.uint8)
        arb_ids_np    = np.frombuffer(rfs.arb_ids,    dtype=np.uint32)
        # Mmap → [N, 64] uint8 view (zero-copy); rows are zero-padded.
        data_view = np.frombuffer(
            rfs._mmap, dtype=np.uint8, count=n * 64
        ).reshape(n, 64)

        # Writable views into the in-memory metadata so we can flip the
        # "decoded" bit and assign name_ids per group post-hoc.
        flags_np    = np.asarray(memoryview(rfs.flags))
        name_ids_np = np.asarray(memoryview(rfs.name_ids))

        # Build (channel, arb_id) groups
        combined = (
            channels_np.astype(np.uint64) << np.uint64(32)
        ) | arb_ids_np.astype(np.uint64)
        sort_idx = np.argsort(combined, kind='stable')
        sorted_combined = combined[sort_idx]
        boundaries = np.concatenate((
            [0],
            np.where(np.diff(sorted_combined) != 0)[0] + 1,
            [n],
        ))

        vec_dbcs: dict[int, VectorizedDBC] = {}
        decoded_total  = 0
        decoded_groups = 0
        total_groups   = len(boundaries) - 1

        for g in range(total_groups):
            start, end = int(boundaries[g]), int(boundaries[g + 1])
            group_idx  = sort_idx[start:end]

            ch_byte = int(channels_np[group_idx[0]])
            arb_id  = int(arb_ids_np[group_idx[0]])
            ch      = None if ch_byte == 255 else ch_byte

            decoder = _decoder_map.get(ch) or _decoder_map.get(None)
            if decoder is None:
                continue

            vec = vec_dbcs.get(id(decoder))
            if vec is None:
                vec = VectorizedDBC(decoder)
                vec_dbcs[id(decoder)] = vec

            candidates = vec.get_candidates(arb_id, is_extended=(arb_id > 0x7FF))
            if not candidates:
                continue

            # Match existing single-decoder behaviour: pick the first candidate.
            message = candidates[0]
            msg_name = message.name
            msg_id   = int(getattr(message, 'frame_id', arb_id))
            msg_dec  = vec.get_message_decoder(message)

            try:
                sig_results = msg_dec.decode(data_view[group_idx])
            except Exception:
                continue

            ts_arr = timestamps_np[group_idx]

            for sig_name, (ext, numeric_arr) in sig_results.items():
                choices = ext.choices
                if choices:
                    disp_list: list[object] = []
                    for v in numeric_arr:
                        if np.isnan(v):
                            disp_list.append('')
                        else:
                            label = choices.get(int(v))
                            disp_list.append(
                                str(label) if label is not None else float(v)
                            )
                    has_labels = True
                    raw_values = disp_list
                else:
                    has_labels = False
                    raw_values = []
                store.add_series_bulk(
                    channel      = ch,
                    message_name = msg_name,
                    message_id   = msg_id,
                    signal_name  = sig_name,
                    unit         = ext.unit,
                    timestamps   = ts_arr,
                    values       = numeric_arr,
                    raw_values   = raw_values,
                    has_labels   = has_labels,
                )

            # Mark these frames as decoded in the raw-frame store
            nid = rfs._name_to_id.get(msg_name)
            if nid is None:
                if len(rfs.name_table) <= 65535:
                    nid = len(rfs.name_table)
                    rfs.name_table.append(msg_name)
                    rfs._name_to_id[msg_name] = nid
                else:
                    nid = 0
            flags_np[group_idx]    |= np.uint8(4)         # decoded bit
            name_ids_np[group_idx]  = np.uint16(nid)

            # Update decoder stats so diagnostics_text() makes sense
            decoder.stats['decode_success'] = (
                decoder.stats.get('decode_success', 0) + len(group_idx)
            )

            decoded_total  += len(group_idx)
            decoded_groups += 1
            if decoded_groups % 50 == 0 or decoded_groups == total_groups:
                self.progress.emit(
                    f"Decoded {decoded_groups}/{total_groups} message groups | "
                    f"signals: {len(store._series_by_key):,} | "
                    f"frames: {decoded_total:,}"
                )
                # Refresh tree + plot every batch of groups so the GUI is
                # responsive while pass 2 is running.
                if store.is_tree_dirty():
                    self.tree_update.emit(store.build_tree_payload())
                self.partial_ready.emit()

        # Override per-frame counters (add_series_bulk is series-oriented and
        # over-counts decoded_frames; note_frame increments unmatched_frames
        # for every frame in pass 1 because it doesn't know yet).
        store.decoded_frames   = decoded_total
        store.unmatched_frames = n - decoded_total

        # Trace tab: needs decoder + config for on-demand signal expansion.
        if cfg:
            rfs.decoder        = cfg.decoder_for(None)
            rfs.channel_config = cfg

        if store.is_tree_dirty():
            self.tree_update.emit(store.build_tree_payload())
        self.partial_ready.emit()

        diag_parts = [store.channel_summary_text()]
        diag_parts.append(
            "\nFirst frame IDs seen in file:\n"
            + ("\n".join(store.first_frame_ids) if store.first_frame_ids else "(none)")
        )
        if cfg:
            for path in cfg.all_dbc_paths():
                dec = cfg._decoder_cache.get(path)
                if dec:
                    diag_parts.append(f"\n\n{dec.diagnostics_text()}")

        store.normalize_timestamps(already_normalized=True)
        store.diagnostics_text = "\n".join(diag_parts)

    # ── Per-frame fallback (used only when mmap unavailable) ─────────────

    def _decode_fallback_in_place(self, rfs, store, decoder_map) -> None:
        """Final-resort: walk the sealed rfs frame-by-frame using cantools."""
        import numpy as np
        from core.models import RawFrame

        n = len(rfs)
        if n == 0:
            return
        timestamps_np = np.frombuffer(rfs.timestamps, dtype=np.float64)
        channels_np   = np.frombuffer(rfs.channels,   dtype=np.uint8)
        arb_ids_np    = np.frombuffer(rfs.arb_ids,    dtype=np.uint32)
        flags_np      = np.frombuffer(rfs.flags,      dtype=np.uint8)
        dlcs_np       = np.frombuffer(rfs.dlcs,       dtype=np.uint8)

        decoded_total = 0
        for i in range(n):
            ch_byte = int(channels_np[i])
            ch      = None if ch_byte == 255 else ch_byte
            decoder = decoder_map.get(ch) or decoder_map.get(None)
            if decoder is None:
                continue
            data = rfs._read_data(i, min(int(dlcs_np[i]), 64))
            frame = RawFrame(
                timestamp=float(timestamps_np[i]),
                channel=ch,
                arbitration_id=int(arb_ids_np[i]),
                is_extended_id=bool(flags_np[i] & 1),
                is_fd=bool(flags_np[i] & 2),
                dlc=int(dlcs_np[i]),
                data=data,
                direction='Unknown',
            )
            samples = decoder.decode_frame(frame)
            if samples:
                store.add_samples_direct(samples)
                decoded_total += 1
        store.decoded_frames   = decoded_total
        store.unmatched_frames = n - decoded_total
        if store.is_tree_dirty():
            self.tree_update.emit(store.build_tree_payload())
        self.partial_ready.emit()
        store.normalize_timestamps(already_normalized=True)
        store.diagnostics_text = store.channel_summary_text()

    # ── CAN-raw path: legacy 1-pass (fallback) ───────────────────────────

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

        # Pre-compute channel→decoder map so the hot loop pays one dict.get()
        # instead of a Python function call + property access + two dict lookups
        # per frame.  Unknown channels seen mid-file are resolved once and cached.
        _decoder_map: dict[int | None, object | None] = {}
        if cfg:
            dec_cache = cfg._decoder_cache
            for ch_key, path in cfg.channels.items():
                actual_ch = None if ch_key == ALL_CHANNELS_KEY else ch_key
                _decoder_map[actual_ch] = dec_cache.get(path)
            _fallback_dec = _decoder_map.get(None)
        else:
            _fallback_dec = None

        # Build (channel, msg_name, sig_name) → choices lookup so the SignalStore
        # only allocates the per-sample raw_values list for label-bearing signals.
        # Frames on unmapped channels use the fallback decoder; the SignalStore
        # falls back to the (None, msg, sig) entry when the channel-specific key
        # is absent.
        choices_lookup: dict[tuple[int | None, str, str], dict] = {}
        for ch, decoder in _decoder_map.items():
            if decoder is None:
                continue
            for (msg_name, sig_name), choices in decoder._choices_cache.items():
                if choices:
                    choices_lookup[(ch, msg_name, sig_name)] = choices
        store.set_choices_lookup(choices_lookup)

        for frame, _legacy_samples in reader.iter_with_frames():
            index += 1
            if base_ts is None:
                base_ts = frame.timestamp
                store.base_ts = base_ts
            frame.timestamp -= base_ts  # normalise to t=0

            store.note_frame(frame)

            # ── Multi-DBC decode: pick decoder by channel ─────────────────
            if cfg:
                ch = frame.channel
                if ch in _decoder_map:
                    decoder = _decoder_map[ch]
                else:
                    # First time seeing this channel — resolve once and cache
                    decoder = cfg.decoder_for(ch) or _fallback_dec
                    _decoder_map[ch] = decoder
                samples = decoder.decode_frame(frame) if decoder else []
            else:
                samples = []
            # NOTE: samples already carry frame.timestamp (already normalised above).
            # Do NOT subtract base_ts again here — that would double-normalise.

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

            # Only rebuild the (sorted) tree payload when a new signal has
            # been added since the last emit. After ~10 s of decode the signal
            # set has stabilised and every later interval would be wasted work.
            if index % _CAN_TREE_INTERVAL == 0 and store.is_tree_dirty():
                self.tree_update.emit(store.build_tree_payload())
            if index % _CAN_PLOT_INTERVAL == 0:
                self.partial_ready.emit()
            if index == 1 or index % _CAN_PROGRESS_INTERVAL == 0:
                self.progress.emit(
                    f"Processed {index:,} frames | "
                    f"signals: {len(store._series_by_key):,} | "
                    f"samples: {store.total_samples:,}"
                )

        # Final flush — guarantees the tree reflects everything we saw, even
        # when the last new signal appeared between intervals.
        if store.is_tree_dirty():
            self.tree_update.emit(store.build_tree_payload())

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
