from __future__ import annotations

from core.models import RawFrame  # noqa: F401 — re-exported

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

import can



class BLFReadError(RuntimeError):
    pass


class BLFReaderService:
    """Read Vector BLF files through python-can."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def __iter__(self) -> Iterator[RawFrame]:
        if not self.path.exists():
            raise BLFReadError(f"BLF file not found: {self.path}")

        try:
            with can.BLFReader(str(self.path)) as reader:
                for msg in reader:
                    data = bytes(msg.data or b"")
                    raw_ch = getattr(msg, "channel", None)
                    # python-can returns 0-indexed channels from BLF binary.
                    # Vector hardware/software uses 1-indexed (CAN 1, CAN 2).
                    channel = (int(raw_ch) + 1) if isinstance(raw_ch, (int, float)) else raw_ch
                    yield RawFrame(
                        timestamp=float(msg.timestamp),
                        channel=channel,
                        arbitration_id=int(msg.arbitration_id),
                        is_extended_id=bool(getattr(msg, "is_extended_id", False)),
                        is_fd=bool(getattr(msg, "is_fd", False)),
                        dlc=int(getattr(msg, "dlc", len(data))),
                        data=data,
                        direction=self._direction(msg),
                    )
        except Exception as exc:  # pragma: no cover - runtime protection
            raise BLFReadError(f"Failed to read BLF file '{self.path}': {exc}") from exc

    def iter_raw_tuples(self):
        """
        Yield ``(timestamp, channel_byte, arb_id, dlc, direction_int,
        is_extended, is_fd, data)`` tuples directly from python-can.

        Avoids constructing a :class:`RawFrame` dataclass per frame — no
        Python object allocation, no ``bytes()`` copy, no string direction.

        * ``channel_byte`` — uint8: channel + 1 (1-indexed), or 255 for None
        * ``direction_int`` — int: 0=Rx, 1=Tx, 2=Unknown
        * ``data``         — raw bytes-like from python-can (bytearray / None)

        Used by the 2-pass vectorised loader (Bottleneck 1 / Pass 1 hot loop).
        """
        if not self.path.exists():
            raise BLFReadError(f"BLF file not found: {self.path}")

        try:
            with can.BLFReader(str(self.path)) as reader:
                for msg in reader:
                    raw_ch = getattr(msg, "channel", None)
                    ch_byte = (
                        (int(raw_ch) + 1) & 0xFF
                        if isinstance(raw_ch, (int, float))
                        else 255
                    )
                    is_rx = getattr(msg, "is_rx", None)
                    dir_int = 0 if is_rx is True else (1 if is_rx is False else 2)
                    d = msg.data
                    yield (
                        float(msg.timestamp),
                        ch_byte,
                        int(msg.arbitration_id),
                        int(getattr(msg, "dlc", len(d) if d else 0)),
                        dir_int,
                        bool(getattr(msg, "is_extended_id", False)),
                        bool(getattr(msg, "is_fd", False)),
                        d if d is not None else b"",
                    )
        except Exception as exc:
            raise BLFReadError(f"Failed to read BLF file '{self.path}': {exc}") from exc

    def iter_raw_batches(self, batch_size: int = 16_384):
        """Extract python-can BLF messages directly into column batches."""
        if not self.path.exists():
            raise BLFReadError(f"BLF file not found: {self.path}")

        timestamps: list[float] = []
        channels: list[int] = []
        arb_ids: list[int] = []
        dlcs: list[int] = []
        directions: list[int] = []
        flags: list[int] = []
        data_block = bytearray(batch_size * 64)
        base_ts: float | None = None

        try:
            with can.BLFReader(str(self.path)) as reader:
                for msg in reader:
                    timestamp = float(msg.timestamp)
                    if base_ts is None:
                        base_ts = timestamp
                    raw_ch = getattr(msg, "channel", None)
                    channel = (
                        (int(raw_ch) + 1) & 0xFF
                        if isinstance(raw_ch, (int, float))
                        else 255
                    )
                    data = msg.data if msg.data is not None else b""
                    is_rx = getattr(msg, "is_rx", None)
                    slot = len(timestamps)
                    timestamps.append(timestamp - base_ts)
                    channels.append(channel)
                    arb_ids.append(int(msg.arbitration_id) & 0xFFFF_FFFF)
                    dlcs.append(min(int(getattr(msg, "dlc", len(data))), 255))
                    directions.append(0 if is_rx is True else 1 if is_rx is False else 2)
                    flags.append(
                        (1 if getattr(msg, "is_extended_id", False) else 0)
                        | (2 if getattr(msg, "is_fd", False) else 0)
                    )
                    offset = slot * 64
                    data_block[offset:offset + 64] = b'\x00' * 64
                    data_len = min(len(data), 64)
                    if data_len:
                        data_block[offset:offset + data_len] = data[:data_len]

                    if len(timestamps) == batch_size:
                        yield (
                            base_ts, timestamps, channels, arb_ids, dlcs,
                            directions, flags, data_block,
                        )
                        timestamps = []
                        channels = []
                        arb_ids = []
                        dlcs = []
                        directions = []
                        flags = []
                        data_block = bytearray(batch_size * 64)

            if timestamps:
                yield (
                    base_ts, timestamps, channels, arb_ids, dlcs,
                    directions, flags, data_block,
                )
        except Exception as exc:
            raise BLFReadError(f"Failed to read BLF file '{self.path}': {exc}") from exc

    @staticmethod
    def _direction(msg: can.Message) -> str:
        is_rx = getattr(msg, "is_rx", None)
        if is_rx is True:
            return "Rx"
        if is_rx is False:
            return "Tx"
        return "Unknown"
