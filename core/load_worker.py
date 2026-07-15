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
_RAW_BATCH_SIZE         = 16_384


def _bulk_compute_store_stats(store, rfs) -> None:
    """
    Populate store frame-statistics from the sealed RawFrameStore arrays.

    Replaces the per-frame ``note_frame()`` calls removed from the Pass 1 hot
    loop (Bottleneck 3).  All work is vectorised numpy; no Python loop over
    individual frames.
    """
    import numpy as np
    n = len(rfs)
    if n == 0:
        return

    channels_np = np.frombuffer(rfs.channels,   dtype=np.uint8)
    arb_ids_np  = np.frombuffer(rfs.arb_ids,    dtype=np.uint32)
    dlcs_np     = np.frombuffer(rfs.dlcs,        dtype=np.uint8)
    dirs_np     = np.frombuffer(rfs.directions,  dtype=np.uint8)
    flags_np    = np.frombuffer(rfs.flags,        dtype=np.uint8)

    # Channel set + per-channel frame counts (single numpy unique pass)
    unique_chs, counts = np.unique(channels_np, return_counts=True)
    for ch_byte, cnt in zip(unique_chs.tolist(), counts.tolist()):
        ch = None if ch_byte == 255 else ch_byte
        store.channels.add(ch)
        store.channel_frame_counts[ch] = cnt

    store.total_frames    = n
    store.unmatched_frames = n  # corrected to n - decoded_total by Pass 2

    # First-20-frame diagnostic strings (same format as note_frame)
    _dir_strs = ('Rx', 'Tx', 'Unknown')
    for i in range(min(20, n)):
        ch_byte   = int(channels_np[i])
        ch        = None if ch_byte == 255 else ch_byte
        aid       = int(arb_ids_np[i])
        dlc       = int(dlcs_np[i])
        fl        = int(flags_np[i])
        direction = _dir_strs[min(int(dirs_np[i]), 2)]
        id_hex    = (
            f"0x{aid:08X}" if (bool(fl & 1) or aid > 0x7FF)
            else f"0x{aid:03X}"
        )
        label = f"CH{ch}" if ch is not None else "CH?"
        store.first_frame_ids.append(
            f"{label} | {id_hex} | DLC={dlc} | {direction}"
        )


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
    # Nested payload uses integer/None channel keys, which Qt's QVariantMap
    # conversion cannot represent reliably. Keep it as an opaque Python object.
    tree_update   = Signal(object)
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

            if hasattr(reader, "iter_decoded_channel_arrays"):
                self._run_mdf_bus_arrays(reader, store)
            elif reader.has_raw_frames and hasattr(reader, "iter_frames_only"):
                # Packed raw-frame read followed by one bulk vectorised decode.
                # The GUI receives the completed signal hierarchy in one
                # handoff instead of rebuilding it group by group.
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

    # ── CAN-raw path: packed read + bulk vectorised decode (preferred) ───

    def _run_can_raw_vectorized(self, reader, store: SignalStore) -> None:
        """
        Bulk loader for BLF/ASC streams.

        Drain every raw frame once into the packed on-disk RawFrameStore.  The
        complete arrays are then grouped by ``(channel, arbitration_id)`` and
        decoded with NumPy.  The raw store remains available for CAN Trace.

        Each group bulk-inserts via
        :meth:`SignalStore.add_series_bulk` (one C-level memcopy per signal).
        Compared to the old per-frame cantools decode, this skips ~50× of the
        Python work for the common little-endian integer-signal case.  Signal
        tree publication is deliberately deferred until every group is ready,
        avoiding queued sequential tree rebuilds in the GUI.
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

        # ── Read once: drain packed frames into raw_frame_store ──────────
        self.progress.emit("Reading and packing raw frames...")
        read_started = time.perf_counter()
        base_ts: float | None = None
        index   = 0
        _batched = hasattr(reader, 'iter_raw_batches')
        _fast   = hasattr(reader, 'iter_raw_tuples')

        if _batched:
            batch_number = 0
            for (
                batch_base_ts, batch_ts, batch_ch, batch_ids, batch_dlcs,
                batch_dirs, batch_flags, batch_data,
            ) in reader.iter_raw_batches(_RAW_BATCH_SIZE):
                batch_number += 1
                if base_ts is None:
                    base_ts = float(batch_base_ts)
                    store.base_ts = base_ts
                rfs.append_raw_batch(
                    batch_ts, batch_ch, batch_ids, batch_dlcs,
                    batch_dirs, batch_flags, batch_data,
                )
                index += len(batch_ts)
                if batch_number == 1 or batch_number % 8 == 0 \
                        or len(batch_ts) < _RAW_BATCH_SIZE:
                    self.progress.emit(f"Reading frames: {index:,}...")

        elif _fast:
            # Zero-object fast path (Bottlenecks 1, 3, 4):
            #   • yields tuples instead of RawFrame dataclasses
            #   • append_raw() skips name-table lookup and string direction
            #   • note_frame() removed; stats computed in bulk after seal()
            # Batch metadata conversion and raw-data writes.  A 16K-frame
            # batch turns ~12 million Python append/write calls for a 1.5M
            # frame file into fewer than 100 C-level bulk operations.
            batch_ts: list[float] = []
            batch_ch: list[int] = []
            batch_ids: list[int] = []
            batch_dlcs: list[int] = []
            batch_dirs: list[int] = []
            batch_flags: list[int] = []
            batch_data = bytearray(_RAW_BATCH_SIZE * 64)

            def flush_batch() -> None:
                if not batch_ts:
                    return
                rfs.append_raw_batch(
                    batch_ts, batch_ch, batch_ids, batch_dlcs,
                    batch_dirs, batch_flags, batch_data,
                )
                batch_ts.clear()
                batch_ch.clear()
                batch_ids.clear()
                batch_dlcs.clear()
                batch_dirs.clear()
                batch_flags.clear()

            for ts, ch_byte, arb_id, dlc, dir_int, is_ext, is_fd, data in \
                    reader.iter_raw_tuples():
                index += 1
                if base_ts is None:
                    base_ts = ts
                    store.base_ts = base_ts
                slot = len(batch_ts)
                batch_ts.append(ts - base_ts)
                batch_ch.append(ch_byte)
                batch_ids.append(arb_id & 0xFFFF_FFFF)
                batch_dlcs.append(min(dlc, 255))
                batch_dirs.append(dir_int)
                batch_flags.append((1 if is_ext else 0) | (2 if is_fd else 0))
                offset = slot * 64
                batch_data[offset:offset + 64] = b'\x00' * 64
                data_len = min(len(data), 64)
                if data_len:
                    batch_data[offset:offset + data_len] = data[:data_len]
                if len(batch_ts) == _RAW_BATCH_SIZE:
                    flush_batch()
                if index == 1 or index % _CAN_PROGRESS_INTERVAL == 0:
                    self.progress.emit(f"Reading frames: {index:,}...")
            flush_batch()
        else:
            # Legacy path: RawFrame objects with per-frame note_frame()
            rfs_append = rfs.append
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
        read_elapsed = time.perf_counter() - read_started
        self.progress.emit(
            f"Packed frame read/store complete: {index:,} frames in {read_elapsed:.1f} s"
        )

        if _batched or _fast:
            # Bulk-compute what per-frame note_frame() would have done.
            _bulk_compute_store_stats(store, rfs)
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

        # ── One global vectorised decode, followed by one GUI handoff ────
        decode_started = time.perf_counter()

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
        decoded_total    = 0
        decoded_groups   = 0
        no_signals_total = 0
        total_groups     = len(boundaries) - 1
        self.progress.emit(
            f"Bulk decoding {n:,} frames across {total_groups:,} CAN ID groups..."
        )

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
                # Slow-path decode and vectorized multiplexing represent
                # inactive/failed rows as NaN.  Do not materialize those rows
                # in SignalStore; asammdf and cantools expose sparse branch
                # series containing only timestamps where the signal exists.
                valid_mask = ~np.isnan(numeric_arr)
                if not valid_mask.any():
                    continue
                if valid_mask.all():
                    signal_ts = ts_arr
                else:
                    signal_ts = ts_arr[valid_mask]
                    numeric_arr = numeric_arr[valid_mask]

                choices = ext.choices
                if choices:
                    # Vectorised choices lookup (Bottleneck 5).
                    # Build a numpy object LUT keyed by integer choice value,
                    # then use fancy indexing instead of a Python per-sample loop.
                    # Falls back to a Python loop only when keys are non-integer
                    # or exceed 65 535 (never seen in practice for DBC enums).
                    nan_mask = np.isnan(numeric_arr)
                    int_arr  = numeric_arr.astype(np.int64)
                    try:
                        int_keys = {
                            k: str(v) for k, v in choices.items()
                            if isinstance(k, int) and k >= 0
                        }
                        max_key = max(int_keys) if int_keys else -1
                    except (TypeError, ValueError):
                        int_keys = {}
                        max_key  = -1

                    if int_keys and max_key < 65536:
                        # Build LUT and a parallel bool mask for valid entries.
                        lut       = np.empty(max_key + 1, dtype=object)
                        lut_valid = np.zeros(max_key + 1, dtype=bool)
                        for k, v in int_keys.items():
                            lut[k]       = v
                            lut_valid[k] = True

                        safe_idx  = np.clip(int_arr, 0, max_key)
                        in_range  = ~nan_mask & (int_arr >= 0) & (int_arr <= max_key)
                        has_val   = in_range & lut_valid[safe_idx]

                        result = np.empty(len(numeric_arr), dtype=object)
                        result[nan_mask]           = ''
                        result[~nan_mask & ~in_range] = numeric_arr[~nan_mask & ~in_range]
                        result[in_range & ~lut_valid[safe_idx]] = \
                            numeric_arr[in_range & ~lut_valid[safe_idx]]
                        if has_val.any():
                            result[has_val] = lut[safe_idx[has_val]]
                        disp_list = result.tolist()
                    else:
                        # Fallback: Python loop (non-integer / very large keys)
                        disp_list = [
                            '' if np.isnan(v)
                            else (str(choices[int(v)])
                                  if int(v) in choices
                                  else float(v))
                            for v in numeric_arr
                        ]
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
                    timestamps   = signal_ts,
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

            # Update decoder stats so diagnostics_text() makes sense.
            # Signal-less groups (e.g. raw UDS diagnostic frames) are counted
            # separately — they are not decode failures.
            if sig_results:
                decoder.stats['decode_success'] = (
                    decoder.stats.get('decode_success', 0) + len(group_idx)
                )
            elif not message.signals:
                decoder.stats['decoded_no_signals'] = (
                    decoder.stats.get('decoded_no_signals', 0) + len(group_idx)
                )
                no_signals_total += len(group_idx)

            decoded_total  += len(group_idx)
            decoded_groups += 1

        # Override per-frame counters (add_series_bulk is series-oriented and
        # over-counts decoded_frames; bulk stats initially count every frame as
        # unmatched because decoding has not happened yet).
        store.decoded_frames   = decoded_total
        store.unmatched_frames = n - decoded_total
        decode_elapsed = time.perf_counter() - decode_started
        n_sigs = len(store._series_by_key)
        hint = (
            " (measurement contains only diagnostic / signal-less frames)"
            if n_sigs == 0 and no_signals_total > 0 else ""
        )
        self.progress.emit(
            f"Bulk vectorized decode/import complete: "
            f"{decoded_groups:,}/{total_groups:,} groups | "
            f"signals: {n_sigs:,} | frames: {decoded_total:,}{hint} | "
            f"elapsed: {decode_elapsed:.1f} s"
        )

        # Trace tab: needs decoder + config for on-demand signal expansion.
        if cfg:
            rfs.decoder        = cfg.decoder_for(None)
            rfs.channel_config = cfg

        # Publish the completed hierarchy once.  Intermediate group updates
        # queued multiple expensive Qt tree rebuilds after large BLF/ASC loads.
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

    # ── Native asammdf bus decode path ───────────────────────────────────

    def _run_mdf_bus_arrays(self, reader, store: SignalStore) -> None:
        """Native asammdf signal decode plus bulk raw-array CAN Trace import."""
        import numpy as np

        stage_start = time.perf_counter()
        self.progress.emit("Decoding MF4 bus log with asammdf (one pass)...")
        cfg = self._channel_config
        metadata_keys: set[tuple[int | None, str, str]] | None = None
        trace_message_names: dict[tuple[int | None, int], str] = {}

        trace_store = (
            RawFrameStore()
            if getattr(reader, "supports_raw_frame_arrays", False)
            else None
        )
        trace_frames = 0

        def on_raw_frame_batch(
            timestamps,
            channels,
            arb_ids,
            dlcs,
            directions,
            flags,
            data_rows,
        ):
            nonlocal trace_frames
            trace_store.append_numpy_batch(
                timestamps,
                channels,
                arb_ids,
                dlcs,
                directions,
                flags,
                data_rows,
            )
            trace_frames += len(timestamps)

        last_progress = [-1]

        def on_extract_progress(current, total):
            if total:
                pct = int(current * 100 / total)
                if pct >= last_progress[0] + 5 or current == total:
                    last_progress[0] = pct
                    self.progress.emit(f"asammdf bus decode: {pct}%")

        def on_metadata_ready(metadata_rows):
            nonlocal metadata_keys
            payload = {}
            metadata_keys = set()
            for (
                channel,
                message_name,
                _message_id,
                signal_name,
                _unit,
            ) in metadata_rows:
                messages = payload.setdefault(channel, {})
                signals = messages.setdefault(message_name, [])
                if signal_name not in signals:
                    signals.append(signal_name)
                metadata_keys.add((channel, message_name, signal_name))
                trace_message_names.setdefault(
                    (channel, int(_message_id)), message_name
                )
            self.tree_update.emit(payload)
            self.progress.emit(
                f"MF4 signal list ready: {len(metadata_rows):,} signals in "
                f"{time.perf_counter() - stage_start:.1f} s"
            )

        # Retain each array until extraction closes, and find the global first
        # timestamp before bulk insertion so every signal shares the same t=0.
        arrays = []
        base_ts: float | None = None
        try:
            iterator_kwargs = {
                "progress": on_extract_progress,
                "metadata_ready": on_metadata_ready,
            }
            if trace_store is not None:
                iterator_kwargs["raw_frame_batch"] = on_raw_frame_batch
            for meta, ts_arr, num_arr, disp_list in \
                    reader.iter_decoded_channel_arrays(cfg, **iterator_kwargs):
                if len(ts_arr) == 0:
                    continue
                arrays.append((meta, ts_arr, num_arr, disp_list))
                first = float(ts_arr[0])
                base_ts = first if base_ts is None else min(base_ts, first)
        except Exception as exc:
            if trace_store is not None:
                trace_store.close()
            self.progress.emit(
                "WARNING: asammdf native extraction was unavailable; "
                f"using CANScope's two-pass fallback ({exc})"
            )
            self._run_can_raw_vectorized(reader, store)
            return

        extraction_elapsed = time.perf_counter() - stage_start
        self.progress.emit(
            f"asammdf extraction and global array read complete: "
            f"{len(arrays):,} signals in "
            f"{extraction_elapsed:.1f} s"
        )

        trace_decoded_frames = 0
        trace_warning = getattr(reader, "raw_trace_error", "")
        if trace_store is not None and trace_warning:
            trace_store.close()
            trace_store = None
            trace_frames = 0
            self.progress.emit(
                f"WARNING: CAN Trace could not be loaded ({trace_warning})"
            )
        if trace_store is not None and trace_frames:
            timestamps_np = np.frombuffer(trace_store.timestamps, dtype=np.float64)
            trace_base_ts = float(np.min(timestamps_np))
            base_ts = (
                trace_base_ts
                if base_ts is None
                else min(base_ts, trace_base_ts)
            )
            timestamps_np -= base_ts

            # Mark raw rows decoded and attach their message names in one
            # vectorised lookup. This keeps row expansion/search identical to
            # BLF/ASC CAN Trace without walking individual frames in Python.
            if trace_message_names:
                channels_np = np.frombuffer(trace_store.channels, dtype=np.uint8)
                arb_ids_np = np.frombuffer(trace_store.arb_ids, dtype=np.uint32)
                flags_np = np.frombuffer(trace_store.flags, dtype=np.uint8)
                name_ids_np = np.frombuffer(trace_store.name_ids, dtype=np.uint16)

                encoded_to_name: dict[int, str] = {}
                for (channel, message_id), message_name in trace_message_names.items():
                    channel_byte = 255 if channel is None else int(channel) & 0xFF
                    is_extended = 1 if int(message_id) > 0x7FF else 0
                    encoded = (
                        (channel_byte << 33)
                        | (is_extended << 32)
                        | (int(message_id) & 0xFFFF_FFFF)
                    )
                    encoded_to_name.setdefault(encoded, message_name)

                if encoded_to_name:
                    encoded_keys = np.asarray(
                        sorted(encoded_to_name), dtype=np.uint64
                    )
                    key_name_ids = np.empty(len(encoded_keys), dtype=np.uint16)
                    for idx, encoded in enumerate(encoded_keys):
                        message_name = encoded_to_name[int(encoded)]
                        name_id = trace_store._name_to_id.get(message_name)
                        if name_id is None:
                            if len(trace_store.name_table) <= 65535:
                                name_id = len(trace_store.name_table)
                                trace_store.name_table.append(message_name)
                                trace_store._name_to_id[message_name] = name_id
                            else:
                                name_id = 0
                        key_name_ids[idx] = name_id

                    frame_keys = (
                        channels_np.astype(np.uint64) << np.uint64(33)
                    ) | (
                        (flags_np.astype(np.uint64) & np.uint64(1))
                        << np.uint64(32)
                    ) | arb_ids_np.astype(np.uint64)
                    positions = np.searchsorted(encoded_keys, frame_keys)
                    safe_positions = np.minimum(positions, len(encoded_keys) - 1)
                    decoded_mask = (
                        (positions < len(encoded_keys))
                        & (encoded_keys[safe_positions] == frame_keys)
                    )
                    flags_np[decoded_mask] |= np.uint8(4)
                    name_ids_np[decoded_mask] = key_name_ids[
                        safe_positions[decoded_mask]
                    ]
                    trace_decoded_frames = int(np.count_nonzero(decoded_mask))

            trace_store.seal()
            store.raw_frame_store = trace_store
            _bulk_compute_store_stats(store, trace_store)
            store.decoded_frames = trace_decoded_frames
            store.unmatched_frames = trace_frames - trace_decoded_frames
            if cfg:
                trace_store.decoder = cfg.decoder_for(None)
                trace_store.channel_config = cfg
            self.progress.emit(
                f"CAN Trace ready: {trace_frames:,} raw frames "
                "(bulk MDF array extraction)."
            )
        elif trace_store is not None:
            trace_store.close()
            trace_store = None

        if base_ts is None:
            base_ts = 0.0
        store.base_ts = base_ts

        total = len(arrays)
        import_start = time.perf_counter()
        self.progress.emit(
            f"Bulk importing {total:,} decoded signal arrays into memory..."
        )
        for meta, ts_arr, num_arr, disp_list in arrays:
            channel, message_name, message_id, signal_name, unit = meta
            store.add_series_bulk(
                channel=channel,
                message_name=message_name,
                message_id=message_id,
                signal_name=signal_name,
                unit=unit,
                timestamps=ts_arr - base_ts,
                values=num_arr,
                raw_values=disp_list,
                has_labels=bool(disp_list),
            )

        # add_series_bulk counts one logical insert per signal. When raw MDF
        # frames are present, report actual decoded/unmatched CAN frame counts.
        if trace_frames:
            store.decoded_frames = trace_decoded_frames
            store.unmatched_frames = trace_frames - trace_decoded_frames

        import_elapsed = time.perf_counter() - import_start
        self.progress.emit(
            f"Bulk import complete: {total:,} signals | samples: "
            f"{store.total_samples:,} | elapsed: {import_elapsed:.1f} s"
        )

        # Verify the metadata-first hierarchy against the imported arrays.
        if store.is_tree_dirty():
            final_payload = store.build_tree_payload()
            final_keys = {
                (channel, message_name, signal_name)
                for channel, messages in final_payload.items()
                for message_name, signal_names in messages.items()
                for signal_name in signal_names
            }
            # The metadata-first tree is normally identical to the completed
            # store. Avoid queuing a second full Qt tree rebuild in that case.
            # If asammdf advertised an empty/unreadable channel, publish the
            # verified final tree so correctness is unchanged.
            if metadata_keys is None or final_keys != metadata_keys:
                self.tree_update.emit(final_payload)
        self.partial_ready.emit()
        store.normalize_timestamps(already_normalized=True)
        store.diagnostics_text = (
            store.channel_summary_text()
            + f"\n\nSource: {reader.source_description}"
            + f"\n\nDecoded signals: {total:,}"
            + "\n\nMF4 decoded directly by asammdf in one pass."
            + (
                f"\nCAN Trace: {trace_frames:,} raw frames loaded in bulk."
                if trace_frames
                else "\nCAN Trace was not available from the MDF raw groups."
            )
        )

    # ── Bulk array path (pre-decoded MF4 / MDF / CSV) ────────────────────

    def _run_bulk_array(self, reader, store: SignalStore) -> None:
        import numpy as np
        stage_start = time.perf_counter()
        self.progress.emit("Reading channels (vectorised fast path)...")
        base_ts: float | None = None
        ch_count = 0
        metadata_first = bool(getattr(reader, "metadata_first_arrays", False))

        def on_metadata_ready(metadata_rows):
            messages = {}
            for group_name, channel_name, _unit in metadata_rows:
                signals = messages.setdefault(group_name, [])
                if channel_name not in signals:
                    signals.append(channel_name)
            self.tree_update.emit({None: messages})
            self.progress.emit(
                f"MDF signal list ready: {len(metadata_rows):,} channels in "
                f"{time.perf_counter() - stage_start:.1f} s"
            )

        if metadata_first:
            array_iter = reader.iter_channel_arrays(
                metadata_ready=on_metadata_ready,
                batch_all_groups=True,
            )
        else:
            array_iter = reader.iter_channel_arrays()

        for (grp_name, ch_name, unit), ts_arr, num_arr, disp_list in \
                array_iter:
            ch_count += 1
            n = len(ts_arr)
            if n == 0:
                continue
            if base_ts is None:
                base_ts = float(ts_arr[0])
                store.base_ts = base_ts
            ts_norm    = ts_arr - base_ts
            has_labels = len(disp_list) > 0
            store.add_series_bulk(
                channel=None, message_name=grp_name, message_id=0,
                signal_name=ch_name, unit=unit,
                timestamps=ts_norm, values=num_arr, raw_values=disp_list,
                has_labels=has_labels,
            )
            # Gate tree rebuild on an interval + dirty flag — avoids a full
            # sort of all signals for every channel (Bottleneck 3).
            if not metadata_first and ch_count % _BULK_PLOT_INTERVAL == 0:
                if store.is_tree_dirty():
                    self.tree_update.emit(store.build_tree_payload())
                self.partial_ready.emit()
            if (
                not metadata_first
                and (ch_count == 1 or ch_count % _BULK_PROGRESS_INTERVAL == 0)
            ):
                self.progress.emit(
                    f"Loaded {ch_count:,} channels | samples: {store.total_samples:,}"
                )

        self.tree_update.emit(store.build_tree_payload())
        self.partial_ready.emit()
        if metadata_first:
            self.progress.emit(
                f"Imported {ch_count:,} channels | samples: "
                f"{store.total_samples:,}"
            )
        store.normalize_timestamps(already_normalized=True)
        store.raw_trace_unavailable_reason = getattr(
            reader,
            "raw_trace_unavailable_reason",
            "This pre-decoded measurement does not contain raw CAN frame records.",
        )
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
