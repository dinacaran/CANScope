from __future__ import annotations

import array as _array
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

import numpy as np

from core.models import RawFrame
from core.models import DecodedSignalSample

@dataclass(slots=True)
class SignalSeries:
    channel: int | None
    message_name: str
    message_id: int
    signal_name: str
    unit: str
    # array.array('d') uses ~3× less memory than a Python float list and
    # appends in amortised O(1) without Python object overhead per element.
    timestamps: _array.array = field(default_factory=lambda: _array.array('d'))
    values: _array.array     = field(default_factory=lambda: _array.array('d'))
    # raw_values is populated only for signals that carry display labels
    # (DBC enum/choice signals, MDF text channels). For ordinary numeric
    # signals it stays empty — consumers fall back to values[idx].
    raw_values: list[object] = field(default_factory=list)
    has_labels: bool = False

    @property
    def key(self) -> str:
        channel_text = f"CH{self.channel}" if self.channel is not None else "CH?"
        return f"{channel_text}::{self.message_name}::{self.signal_name}"

    @property
    def latest_value(self) -> object:
        if self.raw_values:
            return self.raw_values[-1]
        if self.values:
            return self.values[-1]
        return ""

    def display_value_at(self, idx: int) -> object:
        """
        Display value for sample *idx*.
        For label-bearing signals returns the cached label; for plain numeric
        signals returns the float (kept in `values` to save RAM).
        """
        if self.has_labels and 0 <= idx < len(self.raw_values):
            return self.raw_values[idx]
        if 0 <= idx < len(self.values):
            return self.values[idx]
        return ""

    def numpy_timestamps(self) -> np.ndarray:
        """Return timestamps as a numpy float64 array (zero-copy if possible)."""
        return np.frombuffer(self.timestamps, dtype=np.float64)

    def numpy_values(self) -> np.ndarray:
        """Return values as a numpy float64 array (zero-copy if possible)."""
        return np.frombuffer(self.values, dtype=np.float64)


class SignalStore:
    def __init__(self) -> None:
        self._series_by_key: dict[str, SignalSeries] = {}
        self._signals_by_channel_message: dict[int | None, dict[str, list[str]]] = (
            defaultdict(lambda: defaultdict(list))
        )
        # Hot-path cache: (channel, msg_name) → ordered list of SignalSeries refs.
        # Built lazily on first decoded frame for each (channel, msg_name); reused
        # for every subsequent frame so the inner loop avoids string-key concat
        # and dict lookup per signal sample.
        self._msg_series_cache: dict[tuple[int | None, str], list[SignalSeries]] = {}
        # Per-(channel, msg_name, sig_name) choices map provided by the decoder(s).
        # An empty/missing entry means "no labels" — series.has_labels stays False
        # and raw_values is never appended to.
        self._choices_lookup: dict[tuple[int | None, str, str], dict] = {}
        self._tree_dirty: bool = True
        self.total_frames    = 0
        self.decoded_frames  = 0
        self.total_samples   = 0
        self.channels: set[int | None] = set()
        self.channel_frame_counts: dict[int | None, int] = defaultdict(int)
        self.message_hits: dict[tuple[int | None, str], int] = defaultdict(int)
        self.unmatched_frames = 0
        self.first_frame_ids: list[str] = []
        self.diagnostics_text: str = ""
        # On-disk indexed frame store (Option B: no cap, temp file on disk)
        self.raw_frame_store = None   # set to RawFrameStore by LoadWorker
        self.base_ts: float = 0.0  # set by LoadWorker for streaming

    # ── Decoder integration (called once by LoadWorker) ──────────────────

    def set_choices_lookup(
        self,
        lookup: dict[tuple[int | None, str, str], dict],
    ) -> None:
        """
        Provide the per-signal DBC choices map keyed by
        ``(channel, message_name, signal_name) → {int_key: label}``.
        Used at series-creation time to decide whether to maintain
        ``raw_values`` (label-bearing signals only).
        """
        self._choices_lookup = lookup

    # ── Tree-dirty bookkeeping ────────────────────────────────────────────

    def is_tree_dirty(self) -> bool:
        return self._tree_dirty

    def note_frame(self, frame: RawFrame, decoded: bool = False) -> None:
        self.total_frames += 1
        self.channels.add(frame.channel)
        self.channel_frame_counts[frame.channel] += 1
        if not decoded:
            self.unmatched_frames += 1
        if len(self.first_frame_ids) < 20:
            id_hex = (
                f"0x{frame.arbitration_id:08X}"
                if (frame.is_extended_id or frame.arbitration_id > 0x7FF)
                else f"0x{frame.arbitration_id:03X}"
            )
            label = f"CH{frame.channel}" if frame.channel is not None else "CH?"
            self.first_frame_ids.append(
                f"{label} | {id_hex} | DLC={frame.dlc} | {frame.direction}"
            )

    def add_samples(self, samples: Iterable[DecodedSignalSample]) -> None:
        sample_list = list(samples)
        if sample_list:
            self.decoded_frames  += 1
            self.unmatched_frames = max(0, self.unmatched_frames - 1)
        for sample in sample_list:
            self.channels.add(sample.channel)
            self.message_hits[(sample.channel, sample.message_name)] += 1
            key = self._make_key(sample.channel, sample.message_name, sample.signal_name)
            if key not in self._series_by_key:
                series = SignalSeries(
                    channel=sample.channel,
                    message_name=sample.message_name,
                    message_id=sample.message_id,
                    signal_name=sample.signal_name,
                    unit=sample.unit,
                    has_labels=bool(self._choices_lookup.get(
                        (sample.channel, sample.message_name, sample.signal_name)
                    )),
                )
                self._series_by_key[key] = series
                sigs = self._signals_by_channel_message[sample.channel][sample.message_name]
                if sample.signal_name not in sigs:
                    sigs.append(sample.signal_name)
                self._tree_dirty = True
            series = self._series_by_key[key]
            series.timestamps.append(sample.timestamp)
            if series.has_labels:
                series.raw_values.append(sample.value)
            numeric = getattr(sample, 'numeric_value', None)
            if numeric is not None:
                series.values.append(numeric)
            else:
                try:
                    series.values.append(float(sample.value))
                except (TypeError, ValueError):
                    series.values.append(float('nan'))
            self.total_samples += 1

    def add_samples_direct(self, samples: list) -> None:
        """
        Hot path used by the BLF/ASC streaming decoder.

        All samples in *samples* belong to the same decoded message (same
        channel + message_name). The first call for a given (channel,
        message_name) builds and caches the SignalSeries reference list;
        subsequent calls walk the cached list directly — no string-key
        construction, no per-sample dict lookups.
        """
        if not samples:
            return
        self.decoded_frames += 1
        self.unmatched_frames = max(0, self.unmatched_frames - 1)

        first = samples[0]
        channel  = first.channel
        msg_name = first.message_name
        msg_key  = (channel, msg_name)

        cached = self._msg_series_cache.get(msg_key)
        # Cache miss OR signal-set changed (rare — e.g. multiplexed messages)
        if cached is None or len(cached) != len(samples):
            cached = self._build_msg_cache(channel, msg_name, samples)

        n = len(samples)
        self.total_samples += n
        self.channels.add(channel)
        self.message_hits[msg_key] += 1

        # Tight inner loop — only the work that scales with sample count.
        for series, sample in zip(cached, samples):
            series.timestamps.append(sample.timestamp)
            series.values.append(sample.numeric_value)
            if series.has_labels:
                series.raw_values.append(sample.value)

    def _build_msg_cache(
        self,
        channel: int | None,
        msg_name: str,
        samples: list,
    ) -> list[SignalSeries]:
        """Create / look up SignalSeries for each sample's signal and cache them."""
        sigs_for_msg = self._signals_by_channel_message[channel][msg_name]
        choices_lookup = self._choices_lookup
        series_list: list[SignalSeries] = []
        for sample in samples:
            sig_name = sample.signal_name
            str_key = self._make_key(channel, msg_name, sig_name)
            series = self._series_by_key.get(str_key)
            if series is None:
                series = SignalSeries(
                    channel=channel,
                    message_name=msg_name,
                    message_id=sample.message_id,
                    signal_name=sig_name,
                    unit=sample.unit,
                    has_labels=bool(
                        choices_lookup.get((channel, msg_name, sig_name))
                        or choices_lookup.get((None, msg_name, sig_name))
                    ),
                )
                self._series_by_key[str_key] = series
                if sig_name not in sigs_for_msg:
                    sigs_for_msg.append(sig_name)
                self._tree_dirty = True
            series_list.append(series)
        self._msg_series_cache[(channel, msg_name)] = series_list
        return series_list

    def add_series_bulk(
        self,
        channel: int | None,
        message_name: str,
        message_id: int,
        signal_name: str,
        unit: str,
        timestamps: "np.ndarray",
        values: "np.ndarray",
        raw_values: list,
        has_labels: bool = True,
    ) -> None:
        """
        Insert a complete channel time-series in one bulk operation.

        Uses ``array.array.frombytes(ndarray.tobytes())`` which is a single
        C-level memcopy — no per-sample Python loop, no temporary list.
        Called by the LoadWorker MDF/CSV fast path and the BLF/ASC vectorised
        decode path.

        Parameters
        ----------
        timestamps : float64 ndarray, already normalised (t=0 at recording start)
        values     : float64 ndarray of numeric values (integer keys for enum)
        raw_values : list of display values (string labels or floats); may be
                     empty when *has_labels* is False
        has_labels : True for signals whose display value differs from the
                     numeric value (DBC enums, MDF text channels). When False
                     the series is treated as plain numeric and consumers fall
                     back to ``values[idx]`` for display.
        """
        if len(timestamps) == 0:
            return

        key = self._make_key(channel, message_name, signal_name)
        if key not in self._series_by_key:
            series = SignalSeries(
                channel=channel,
                message_name=message_name,
                message_id=message_id,
                signal_name=signal_name,
                unit=unit,
                has_labels=has_labels,
            )
            self._series_by_key[key] = series
            sigs = self._signals_by_channel_message[channel][message_name]
            if signal_name not in sigs:
                sigs.append(signal_name)
            self._tree_dirty = True
        else:
            series = self._series_by_key[key]

        # C-level memcopy — no Python loop, no object allocation per sample
        ts_bytes  = np.asarray(timestamps, dtype=np.float64).tobytes()
        val_bytes = np.asarray(values,     dtype=np.float64).tobytes()
        series.timestamps.frombytes(ts_bytes)
        series.values.frombytes(val_bytes)
        if has_labels and raw_values:
            series.raw_values.extend(raw_values)

        n = len(timestamps)
        self.total_samples  += n
        self.decoded_frames += 1        # one "frame" per bulk channel insert
        self.channels.add(channel)
        self.message_hits[(channel, message_name)] += n

    def normalize_timestamps(self, already_normalized: bool = False) -> None:
        """
        Shift all timestamps so that t=0 is the start of the recording.
        If already_normalized=True (inline normalisation was done by LoadWorker),
        only the raw_frames need correcting (their timestamps were set before subtraction).
        """
        if already_normalized:
            # SignalSeries timestamps AND raw_frame timestamps are already correct:
            # LoadWorker does frame.timestamp -= base_ts BEFORE calling
            # add_raw_frame(), so time_s is already 0-based at storage time.
            # Nothing to do here.
            return

        # Legacy path: full normalisation pass (used if called without LoadWorker)
        min_ts: float | None = None
        for series in self._series_by_key.values():
            if series.timestamps:
                local_min = min(series.timestamps)
                if min_ts is None or local_min < min_ts:
                    min_ts = local_min
        if min_ts is None:
            return
        for series in self._series_by_key.values():
            if series.timestamps:
                new_ts = _array.array('d', (t - min_ts for t in series.timestamps))
                series.timestamps = new_ts
        # raw_frame_store timestamps are normalised inline by LoadWorker

    def get_series(self, key: str) -> SignalSeries | None:
        return self._series_by_key.get(key)

    def all_keys(self) -> list[str]:
        return sorted(self._series_by_key.keys())

    def build_tree_payload(self) -> dict[int | None, dict[str, list[str]]]:
        payload: dict[int | None, dict[str, list[str]]] = {}
        for channel in sorted(self.channels, key=lambda x: (999999 if x is None else x)):
            payload[channel] = {}
        for channel, message_map in self._signals_by_channel_message.items():
            payload.setdefault(channel, {})
            payload[channel].update(
                {msg: sorted(sigs) for msg, sigs in sorted(message_map.items())}
            )
        self._tree_dirty = False
        return payload

    def channel_summary_text(self) -> str:
        if not self.channels:
            return 'Channels: 0'
        ordered = sorted(self.channels, key=lambda x: (999999 if x is None else x))
        parts = [
            f"{'CH' + str(ch) if ch is not None else 'CH?'} "
            f"({self.channel_frame_counts.get(ch, 0):,} frames)"
            for ch in ordered
        ]
        rfs = self.raw_frame_store
        total_raw = len(rfs) if rfs is not None else 0
        raw_note = f"  | CAN Trace: {total_raw:,} frames" if total_raw else ""
        return f"Channels: {len(ordered)} | " + ', '.join(parts) + raw_note

    @staticmethod
    def _make_key(channel: int | None, message_name: str, signal_name: str) -> str:
        channel_text = f"CH{channel}" if channel is not None else "CH?"
        return f"{channel_text}::{message_name}::{signal_name}"
