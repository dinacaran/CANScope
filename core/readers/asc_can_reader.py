from __future__ import annotations

from pathlib import Path
from typing import Iterator

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
        self.load_messages: list[str] = list(decoder.load_messages)

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

    @property
    def decoder(self) -> DBCDecoder:
        return self._decoder
