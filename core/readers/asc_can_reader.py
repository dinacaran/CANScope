from __future__ import annotations

from pathlib import Path
from typing import Iterator
import binascii

import can

from core.models import RawFrame
from core.dbc_decoder import DBCDecoder
from core.models import DecodedSignalSample


class ASCReadError(RuntimeError):
    pass


def _parse_payload_tail(tail: bytes, data_len: int, numeric_base: int) -> bytes:
    """Decode only the payload prefix, leaving ASC trailing columns untouched."""
    if data_len <= 0:
        return b""
    if numeric_base == 16:
        # Vector ASC uses two hex digits plus one space per byte.  Cutting the
        # fixed-width prefix avoids splitting the many trailing CAN-FD fields.
        compact = tail.lstrip()[:data_len * 3 - 1].replace(b" ", b"")
        if len(compact) == data_len * 2:
            try:
                return binascii.unhexlify(compact)
            except binascii.Error:
                pass
        fields = tail.split(None, data_len)
        return binascii.unhexlify(b"".join(fields[:data_len]))
    fields = tail.split(None, data_len)
    return bytes(int(value, 10) for value in fields[:data_len])


class ASCCANReader:
    """
    Reads a Vector CANalyzer ASCII log (.asc) and decodes signals using
    a :class:`DBCDecoder`.

    python-can's ``ASCReader`` yields ``can.Message`` objects — the same
    interface as ``BLFReader`` — so the downstream DBC pipeline is identical
    to :class:`BLFCANReader`.

    Attributes
    ----------
    source_description : str
    has_raw_frames : bool
        Always ``True``.
    """

    has_raw_frames: bool = True

    def __init__(self, asc_path: str | Path, decoder: DBCDecoder) -> None:
        self._path    = Path(asc_path)
        self._decoder = decoder
        self.source_description = (
            f"ASC + DBC  ({self._path.name} / {self._decoder.dbc_path.name})"
        )
        self.load_messages: list[str] = list(decoder.load_messages) + [
            "Fast path: bulk fixed-column ASC array extraction."
        ]

    # ── Protocol-required iterator ────────────────────────────────────────

    def __iter__(self) -> Iterator[DecodedSignalSample]:
        for _frame, samples in self.iter_with_frames():
            yield from samples

    # ── Extended iterator ─────────────────────────────────────────────────

    def iter_with_frames(self) -> Iterator[tuple[RawFrame, list[DecodedSignalSample]]]:
        for frame in self.iter_frames_only():
            samples = self._decoder.decode_frame(frame)
            yield frame, samples

    def iter_frames_only(self) -> Iterator[RawFrame]:
        """Yield raw frames without decoding (used by vectorised 2-pass load)."""
        if not self._path.exists():
            raise ASCReadError(f"ASC file not found: {self._path}")
        try:
            with can.ASCReader(str(self._path)) as reader:
                for msg in reader:
                    # Skip non-data objects (e.g. ASC comment/event lines)
                    if not hasattr(msg, 'arbitration_id'):
                        continue
                    data = bytes(msg.data or b"")
                    is_rx = getattr(msg, "is_rx", None)
                    direction = (
                        "Rx" if is_rx is True else
                        "Tx" if is_rx is False else
                        "Unknown"
                    )
                    raw_ch = getattr(msg, 'channel', None)
                    # ASC also returns 0-indexed channels from python-can
                    ch_1idx = (int(raw_ch) + 1) if isinstance(raw_ch, (int, float)) else raw_ch
                    yield RawFrame(
                        timestamp     = float(msg.timestamp),
                        channel       = ch_1idx,
                        arbitration_id= int(msg.arbitration_id),
                        is_extended_id= bool(getattr(msg, "is_extended_id", False)),
                        is_fd         = bool(getattr(msg, "is_fd", False)),
                        dlc           = int(getattr(msg, "dlc", len(data))),
                        data          = data,
                        direction     = direction,
                    )
        except ASCReadError:
            raise
        except Exception as exc:
            raise ASCReadError(
                f"Failed to read ASC file '{self._path}': {exc}"
            ) from exc

    def iter_raw_tuples(self):
        """Yield compact raw tuples for LoadWorker's batched fast path."""
        if not self._path.exists():
            raise ASCReadError(f"ASC file not found: {self._path}")
        try:
            with can.ASCReader(str(self._path)) as reader:
                for msg in reader:
                    if not hasattr(msg, 'arbitration_id'):
                        continue
                    data = msg.data if msg.data is not None else b""
                    raw_ch = getattr(msg, 'channel', None)
                    channel = (
                        (int(raw_ch) + 1) & 0xFF
                        if isinstance(raw_ch, (int, float))
                        else 255
                    )
                    is_rx = getattr(msg, "is_rx", None)
                    direction = 0 if is_rx is True else 1 if is_rx is False else 2
                    yield (
                        float(msg.timestamp),
                        channel,
                        int(msg.arbitration_id),
                        int(getattr(msg, "dlc", len(data))),
                        direction,
                        bool(getattr(msg, "is_extended_id", False)),
                        bool(getattr(msg, "is_fd", False)),
                        data,
                    )
        except ASCReadError:
            raise
        except Exception as exc:
            raise ASCReadError(
                f"Failed to read ASC file '{self._path}': {exc}"
            ) from exc

    def iter_raw_batches(self, batch_size: int = 16_384):
        """Parse Vector ASC directly into column batches.

        Supports the standard classic-CAN and CAN-FD layouts written by
        CANalyzer/CANoe and python-can.  Header, trigger, comment, status and
        error-event lines are ignored.  Channels in ASC text are already
        1-indexed, so no channel conversion is needed.
        """
        if not self._path.exists():
            raise ASCReadError(f"ASC file not found: {self._path}")

        timestamps: list[float] = []
        channels = bytearray()
        arb_ids: list[int] = []
        dlcs = bytearray()
        directions = bytearray()
        flags = bytearray()
        data_block = bytearray(batch_size * 64)
        base_ts: float | None = None
        numeric_base = 16

        try:
            with self._path.open('rb', buffering=4 << 20) as stream:
                for line in stream:
                    if line[:5].lower() == b'base ':
                        header = line.split(None, 2)
                        numeric_base = 10 if header[1].lower() == b'dec' else 16
                        continue

                    # asammdf/Vector CAN-FD exports use fixed prefix columns.
                    # Read those slices directly so a multi-million-line file
                    # does not allocate and discard every unused trailing
                    # status field.  Non-standard lines use the general parser
                    # below without changing accepted ASC variants.
                    fixed_canfd = False
                    if numeric_base == 16 and len(line) >= 79 \
                            and line[12:17] == b'CANFD':
                        try:
                            timestamp = float(line[:12])
                            channel = int(line[17:22])
                            direction_initial = line[22]
                            direction_value = (
                                0 if direction_initial in (ord('R'), ord('r'))
                                else 1 if direction_initial in (ord('T'), ord('t'))
                                else 2
                            )
                            id_token = line[32:42].strip()
                            data_len = min(int(line[75:79]), 64)
                            extended = id_token[-1:] in (b'x', b'X')
                            if extended:
                                id_token = id_token[:-1]
                            arb_id = int(id_token, 16)
                            payload = binascii.unhexlify(
                                line[79:79 + data_len * 3 - 1].replace(b' ', b'')
                            ) if data_len else b''
                            if len(payload) != data_len:
                                raise ValueError("short fixed-column payload")
                            is_fd = True
                            fixed_canfd = True
                        except (ValueError, IndexError, binascii.Error):
                            fixed_canfd = False

                    if not fixed_canfd:
                        try:
                            # Split only the stable ASC prefix.  The final
                            # element retains payload + trailing CAN-FD
                            # columns as one bytes object.
                            fields = line.split(None, 9)
                            if len(fields) < 2:
                                continue
                            timestamp = float(fields[0])
                        except (ValueError, TypeError):
                            continue

                        is_fd = fields[1].upper() == b'CANFD'
                        try:
                            if is_fd:
                                # ts CANFD channel Rx id brs esi dlc data_len data...
                                if len(fields) < 10:
                                    continue
                                channel = int(fields[2])
                                direction_token = fields[3].lower()
                                id_token = fields[4]
                                # Some CANalyzer exports include a symbolic
                                # message name after the ID.
                                if fields[5].isdigit():
                                    data_len = min(int(fields[8]), 64)
                                    payload_tail = fields[9]
                                else:
                                    length_and_payload = fields[9].split(None, 1)
                                    if len(length_and_payload) != 2:
                                        continue
                                    data_len = min(int(length_and_payload[0]), 64)
                                    payload_tail = length_and_payload[1]
                            else:
                                # ts channel id [symbolic-name] Rx d|r dlc data...
                                classic = line.split(None, 6)
                                if len(classic) < 6:
                                    continue
                                channel = int(classic[1])
                                id_token = classic[2]
                                direction_index = 3 if classic[3].lower() in (b'rx', b'tx') else 4
                                direction_token = classic[direction_index].lower()
                                frame_kind = classic[direction_index + 1].lower()
                                if frame_kind not in (b'd', b'r'):
                                    continue
                                if frame_kind == b'r':
                                    data_len = 0
                                    payload_tail = b''
                                elif direction_index == 3:
                                    data_len = min(int(classic[5]), 64)
                                    payload_tail = classic[6] if len(classic) > 6 else b''
                                else:
                                    length_and_payload = classic[6].split(None, 1)
                                    data_len = min(int(length_and_payload[0]), 64)
                                    payload_tail = length_and_payload[1] if len(length_and_payload) > 1 else b''

                            extended = id_token[-1:].lower() == b'x'
                            if extended:
                                id_token = id_token[:-1]
                            arb_id = int(id_token, numeric_base)
                        except (ValueError, IndexError):
                            continue

                        try:
                            payload = _parse_payload_tail(
                                payload_tail, data_len, numeric_base,
                            )
                        except (ValueError, OverflowError, binascii.Error):
                            continue
                        direction_value = (
                            0 if direction_token == b'rx'
                            else 1 if direction_token == b'tx'
                            else 2
                        )

                    if base_ts is None:
                        base_ts = timestamp
                    slot = len(timestamps)
                    timestamps.append(timestamp - base_ts)
                    channels.append(channel if 0 <= channel < 255 else 255)
                    arb_ids.append(arb_id & 0xFFFF_FFFF)
                    dlcs.append(data_len)
                    directions.append(direction_value)
                    flags.append((1 if extended else 0) | (2 if is_fd else 0))
                    offset = slot * 64
                    if payload:
                        data_block[offset:offset + len(payload)] = payload

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
        except ASCReadError:
            raise
        except Exception as exc:
            raise ASCReadError(
                f"Failed to parse ASC file '{self._path}': {exc}"
            ) from exc

    @property
    def decoder(self) -> DBCDecoder:
        return self._decoder
