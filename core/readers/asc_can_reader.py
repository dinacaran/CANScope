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
            "Fast path: direct ASC column-array extraction."
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
        channels: list[int] = []
        arb_ids: list[int] = []
        dlcs: list[int] = []
        directions: list[int] = []
        flags: list[int] = []
        data_block = bytearray(batch_size * 64)
        base_ts: float | None = None
        numeric_base = 16

        try:
            with self._path.open('rb', buffering=4 << 20) as stream:
                for line in stream:
                    fields = line.split()
                    if len(fields) >= 2 and fields[0].lower() == b'base':
                        numeric_base = 10 if fields[1].lower() == b'dec' else 16
                        continue
                    if len(fields) < 6:
                        continue
                    try:
                        timestamp = float(fields[0])
                    except (ValueError, TypeError):
                        continue

                    is_fd = fields[1].upper() == b'CANFD'
                    try:
                        if is_fd:
                            # ts CANFD channel Rx id brs esi dlc data_len data...
                            if len(fields) < 9:
                                continue
                            channel = int(fields[2])
                            direction_token = fields[3].lower()
                            id_token = fields[4]
                            # Some CANalyzer exports include a symbolic message
                            # name after the ID.  The BRS/ESI/DLC fields are
                            # numeric, so detect and skip that optional token.
                            fd_offset = 1 if not fields[5].isdigit() else 0
                            data_len = min(int(fields[8 + fd_offset]), 64)
                            data_start = 9 + fd_offset
                        else:
                            # ts channel id [symbolic-name] Rx d|r dlc data...
                            channel = int(fields[1])
                            id_token = fields[2]
                            direction_index = (
                                3 if fields[3].lower() in (b'rx', b'tx') else 4
                            )
                            direction_token = fields[direction_index].lower()
                            frame_kind = fields[direction_index + 1].lower()
                            if frame_kind not in (b'd', b'r'):
                                continue
                            data_len = (
                                0 if frame_kind == b'r'
                                else min(int(fields[direction_index + 2]), 64)
                            )
                            data_start = direction_index + 3

                        extended = id_token[-1:].lower() == b'x'
                        if extended:
                            id_token = id_token[:-1]
                        arb_id = int(id_token, numeric_base)
                    except (ValueError, IndexError):
                        continue

                    available = min(data_len, max(0, len(fields) - data_start))
                    try:
                        if not available:
                            payload = b''
                        elif numeric_base == 16:
                            payload = binascii.unhexlify(
                                b''.join(fields[data_start:data_start + available])
                            )
                        else:
                            payload = bytes(
                                int(value, 10)
                                for value in fields[data_start:data_start + available]
                            )
                    except (ValueError, OverflowError, binascii.Error):
                        continue

                    if base_ts is None:
                        base_ts = timestamp
                    slot = len(timestamps)
                    timestamps.append(timestamp - base_ts)
                    channels.append(channel if 0 <= channel < 255 else 255)
                    arb_ids.append(arb_id & 0xFFFF_FFFF)
                    dlcs.append(data_len)
                    directions.append(
                        0 if direction_token == b'rx'
                        else 1 if direction_token == b'tx'
                        else 2
                    )
                    flags.append((1 if extended else 0) | (2 if is_fd else 0))
                    offset = slot * 64
                    data_block[offset:offset + 64] = b'\x00' * 64
                    if payload:
                        data_block[offset:offset + len(payload)] = payload

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
        except ASCReadError:
            raise
        except Exception as exc:
            raise ASCReadError(
                f"Failed to parse ASC file '{self._path}': {exc}"
            ) from exc

    @property
    def decoder(self) -> DBCDecoder:
        return self._decoder
