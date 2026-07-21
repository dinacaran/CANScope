from __future__ import annotations

from core.models import RawFrame  # noqa: F401 — re-exported

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

import can
from can.io import blf as _blf


class _PackedBLFReader(can.BLFReader):
    """python-can BLF container reader that yields raw columns, not Messages."""

    def _parse_data(self, data: bytes):
        unpack_base = _blf.OBJ_HEADER_BASE_STRUCT.unpack_from
        base_size = _blf.OBJ_HEADER_BASE_STRUCT.size
        unpack_v1 = _blf.OBJ_HEADER_V1_STRUCT.unpack_from
        v1_size = _blf.OBJ_HEADER_V1_STRUCT.size
        unpack_v2 = _blf.OBJ_HEADER_V2_STRUCT.unpack_from
        v2_size = _blf.OBJ_HEADER_V2_STRUCT.size
        unpack_can = _blf.CAN_MSG_STRUCT.unpack_from
        unpack_fd = _blf.CAN_FD_MSG_STRUCT.unpack_from
        unpack_fd64 = _blf.CAN_FD_MSG_64_STRUCT.unpack_from
        fd64_size = _blf.CAN_FD_MSG_64_STRUCT.size
        unpack_error = _blf.CAN_ERROR_EXT_STRUCT.unpack_from
        dlc2len = _blf.dlc2len
        start_timestamp = self.start_timestamp
        max_pos = len(data)
        pos = 0

        while True:
            self._pos = pos
            try:
                pos = data.index(b"LOBJ", pos, pos + 8)
            except ValueError:
                if pos + 8 > max_pos:
                    return
                raise _blf.BLFParseError("Could not find next object") from None

            signature, header_size, header_version, obj_size, obj_type = unpack_base(data, pos)
            if signature != b"LOBJ":
                raise _blf.BLFParseError()
            next_pos = pos + obj_size
            if next_pos > max_pos:
                return
            pos += base_size

            if header_version == 1:
                time_flags, _, _, timestamp = unpack_v1(data, pos)
                pos += v1_size
            elif header_version == 2:
                time_flags, _, _, timestamp = unpack_v2(data, pos)
                pos += v2_size
            else:
                pos = next_pos
                continue

            factor = 1e-5 if time_flags == _blf.TIME_TEN_MICS else 1e-9
            timestamp = timestamp * factor + start_timestamp

            if obj_type in (_blf.CAN_MESSAGE, _blf.CAN_MESSAGE2):
                channel, msg_flags, dlc, can_id, payload = unpack_can(data, pos)
                yield (
                    timestamp, channel, can_id & 0x1FFF_FFFF, dlc,
                    1 if msg_flags & _blf.DIR else 0,
                    bool(can_id & _blf.CAN_MSG_EXT), False, payload[:dlc],
                )
            elif obj_type == _blf.CAN_ERROR_EXT:
                members = unpack_error(data, pos)
                channel, dlc, can_id, payload = members[0], members[5], members[7], members[9]
                yield (
                    timestamp, channel, can_id & 0x1FFF_FFFF, dlc, 0,
                    bool(can_id & _blf.CAN_MSG_EXT), False, payload[:dlc],
                )
            elif obj_type == _blf.CAN_FD_MESSAGE:
                channel, msg_flags, dlc, can_id, _, _, fd_flags, valid_bytes, payload = unpack_fd(data, pos)
                yield (
                    timestamp, channel, can_id & 0x1FFF_FFFF, dlc2len(dlc),
                    1 if msg_flags & _blf.DIR else 0,
                    bool(can_id & _blf.CAN_MSG_EXT), bool(fd_flags & 0x1),
                    payload[:valid_bytes],
                )
            elif obj_type == _blf.CAN_FD_MESSAGE_64:
                (
                    channel, dlc, valid_bytes, _, can_id, _, fd_flags,
                    _, _, _, _, _, direction, ext_data_offset, _,
                ) = unpack_fd64(data, pos)
                data_length = min(
                    valid_bytes,
                    (ext_data_offset or obj_size) - header_size - fd64_size,
                )
                payload_offset = pos + fd64_size
                payload = data[payload_offset:payload_offset + data_length]
                if data_length < valid_bytes:
                    payload = payload.ljust(valid_bytes, b"\x00")
                yield (
                    timestamp, channel, can_id & 0x1FFF_FFFF, dlc2len(dlc),
                    1 if direction else 0,
                    bool(can_id & _blf.CAN_MSG_EXT), bool(fd_flags & 0x1000),
                    payload,
                )

            pos = next_pos



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
        """Extract BLF objects directly into packed column batches.

        Container decompression remains owned by python-can, while the inner
        reader bypasses construction of one ``can.Message`` per frame.
        """
        if not self.path.exists():
            raise BLFReadError(f"BLF file not found: {self.path}")

        timestamps: list[float] = []
        channels = bytearray()
        arb_ids: list[int] = []
        dlcs = bytearray()
        directions = bytearray()
        flags = bytearray()
        data_block = bytearray(batch_size * 64)
        base_ts: float | None = None

        try:
            with _PackedBLFReader(str(self.path)) as reader:
                for (
                    timestamp, channel, arb_id, dlc, direction,
                    is_extended, is_fd, data,
                ) in reader:
                    if base_ts is None:
                        base_ts = timestamp
                    slot = len(timestamps)
                    timestamps.append(timestamp - base_ts)
                    channels.append(channel if 0 <= channel < 255 else 255)
                    arb_ids.append(arb_id)
                    dlcs.append(min(dlc, 255))
                    directions.append(direction)
                    flags.append(
                        (1 if is_extended else 0) | (2 if is_fd else 0)
                    )
                    offset = slot * 64
                    data_len = min(len(data), 64)
                    if data_len:
                        data_block[offset:offset + data_len] = data[:data_len]

                    if len(timestamps) == batch_size:
                        yield (
                            base_ts, timestamps, channels, arb_ids, dlcs,
                            directions, flags, data_block,
                        )
                        timestamps = []
                        channels = bytearray()
                        arb_ids = []
                        dlcs = bytearray()
                        directions = bytearray()
                        flags = bytearray()
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
