from __future__ import annotations

import array as _array
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

import numpy as np

from core.blf_reader import RawFrame
from core.dbc_decoder import DecodedSignalSample

# ── Bug 3 fix: cap raw frame storage to avoid OOM on large BLF files ──────
_MAX_RAW_FRAMES = 100_000


@dataclass(slots=True)
class RawFrameSignalView:
    signal_name: str
    physical_value: object
    unit: str
    raw_value: object = ""


@dataclass(slots=True)
class RawFrameEntry:
    time_s: float
    start_of_frame_s: float
    channel: int | None
    arbitration_id: int
    frame_name: str
    direction: str
    dlc: int
    data_hex: str
    decoded: bool
    signals: list[RawFrameSignalView] = field(default_factory=list)


@dataclass(slots=True)
class SignalSeries:
    channel: int | None
    message_name: str
    message_id: int
    signal_name: str
    unit: str
    # Bug 3 fix: array.array('d') uses ~3× less memory than a Python float list
    # and appends in amortised O(1) without Python object overhead per element.
    timestamps: _array.array = field(default_factory=lambda: _array.array('d'))
    values: _array.array     = field(default_factory=lambda: _array.array('d'))
    raw_values: list[object] = field(default_factory=list)

    @property
    def key(self) -> str:
        channel_text = f"CH{self.channel}" if self.channel is not None else "CH?"
        return f"{channel_text}::{self.message_name}::{self.signal_name}"

    @property
    def latest_value(self) -> object:
        return self.raw_values[-1] if self.raw_values else ""

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
        self.total_frames    = 0
        self.decoded_frames  = 0
        self.total_samples   = 0
        self.channels: set[int | None] = set()
        self.channel_frame_counts: dict[int | None, int] = defaultdict(int)
        self.message_hits: dict[tuple[int | None, str], int] = defaultdict(int)
        self.unmatched_frames = 0
        self.first_frame_ids: list[str] = []
        self.diagnostics_text: str = ""
        # Bug 3 fix: bounded; stops storing after _MAX_RAW_FRAMES
        self.raw_frames: list[RawFrameEntry] = []
        self._raw_frames_capped = False
        self.base_ts: float = 0.0  # set by LoadWorker for streaming

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
                )
                self._series_by_key[key] = series
                sigs = self._signals_by_channel_message[sample.channel][sample.message_name]
                if sample.signal_name not in sigs:
                    sigs.append(sample.signal_name)
            series = self._series_by_key[key]
            series.timestamps.append(sample.timestamp)
            series.raw_values.append(sample.value)
            # Use pre-computed numeric_value (handles enum/choice signals).
            # Falls back to float(sample.value) for older callers without the field.
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
        Like add_samples() but accepts an already-materialised list directly.
        Avoids the list() re-wrap overhead in the hot decode loop.
        """
        if not samples:
            return
        self.decoded_frames += 1
        self.unmatched_frames = max(0, self.unmatched_frames - 1)
        for sample in samples:
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
                )
                self._series_by_key[key] = series
                sigs = self._signals_by_channel_message[sample.channel][sample.message_name]
                if sample.signal_name not in sigs:
                    sigs.append(sample.signal_name)
            series = self._series_by_key[key]
            series.timestamps.append(sample.timestamp)
            series.raw_values.append(sample.value)
            numeric = sample.numeric_value
            if numeric is not None:
                series.values.append(numeric)
            else:
                try:
                    series.values.append(float(sample.value))
                except (TypeError, ValueError):
                    series.values.append(float('nan'))
            self.total_samples += 1

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
    ) -> None:
        """
        Insert a complete channel time-series in one bulk operation.

        Uses ``array.array.frombytes(ndarray.tobytes())`` which is a single
        C-level memcopy — no per-sample Python loop, no temporary list.
        Called by the LoadWorker MDF/CSV fast path.

        Parameters
        ----------
        timestamps : float64 ndarray, already normalised (t=0 at recording start)
        values     : float64 ndarray of numeric values (integer keys for enum)
        raw_values : list of display values (string labels or floats)
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
            )
            self._series_by_key[key] = series
            sigs = self._signals_by_channel_message[channel][message_name]
            if signal_name not in sigs:
                sigs.append(signal_name)
        else:
            series = self._series_by_key[key]

        series = self._series_by_key[key]

        # C-level memcopy — no Python loop, no object allocation per sample
        ts_bytes  = np.asarray(timestamps, dtype=np.float64).tobytes()
        val_bytes = np.asarray(values,     dtype=np.float64).tobytes()
        series.timestamps.frombytes(ts_bytes)
        series.values.frombytes(val_bytes)
        series.raw_values.extend(raw_values)

        n = len(timestamps)
        self.total_samples  += n
        self.decoded_frames += 1        # one "frame" per bulk channel insert
        self.channels.add(channel)
        self.message_hits[(channel, message_name)] += n

    def add_raw_frame(self, frame: RawFrame, samples: Iterable[DecodedSignalSample]) -> None:
        # Bug 3 fix: stop accumulating after cap to prevent OOM on large BLF files.
        if self._raw_frames_capped:
            return
        if len(self.raw_frames) >= _MAX_RAW_FRAMES:
            self._raw_frames_capped = True
            return
        sample_list = list(samples)
        frame_name  = sample_list[0].message_name if sample_list else ""
        signal_views = [
            RawFrameSignalView(
                signal_name=s.signal_name,
                physical_value=s.value,
                unit=s.unit,
                raw_value=s.value,
            )
            for s in sample_list
        ]
        self.raw_frames.append(
            RawFrameEntry(
                time_s=frame.timestamp,
                start_of_frame_s=frame.timestamp,
                channel=frame.channel,
                arbitration_id=frame.arbitration_id,
                frame_name=frame_name,
                direction=frame.direction,
                dlc=frame.dlc,
                data_hex=" ".join(f"{b:02X}" for b in frame.data),
                decoded=bool(sample_list),
                signals=signal_views,
            )
        )

    def normalize_timestamps(self, already_normalized: bool = False) -> None:
        """
        Shift all timestamps so that t=0 is the start of the recording.
        If already_normalized=True (inline normalisation was done by LoadWorker),
        only the raw_frames need correcting (their timestamps were set before subtraction).
        """
        if already_normalized:
            # Timestamps in SignalSeries are already correct (subtracted in LoadWorker).
            # raw_frames store the original absolute timestamps → correct them now.
            for frame in self.raw_frames:
                frame.time_s           -= self.base_ts
                frame.start_of_frame_s -= self.base_ts
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
        for frame in self.raw_frames:
            frame.time_s           -= min_ts
            frame.start_of_frame_s -= min_ts

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
        suffix = f"  [raw frame display capped at {_MAX_RAW_FRAMES:,}]" if self._raw_frames_capped else ""
        return f"Channels: {len(ordered)} | " + ', '.join(parts) + suffix

    @staticmethod
    def _make_key(channel: int | None, message_name: str, signal_name: str) -> str:
        channel_text = f"CH{channel}" if channel is not None else "CH?"
        return f"{channel_text}::{message_name}::{signal_name}"
