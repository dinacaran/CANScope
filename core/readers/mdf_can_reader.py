"""
MDFCANReader — reads ASAM MDF4 bus logging files (.mf4 / .mdf) that store
raw CAN frames using the ``CAN_DataFrame.*`` channel group structure.

python-can's ``MF4Reader`` wraps asammdf internally and yields
``can.Message`` objects — the exact same interface as ``BLFReader`` and
``ASCReader``.  This means the entire existing DBC decode pipeline
(DBCDecoder, LoadWorker, RawFrameStore, DBC Manager) works without any
changes.

Requires both ``asammdf>=7.0`` and the MF4 support in ``python-can>=4.6``.

Usage in ``reader_factory``::

    if MDFReader.is_bus_logging(path):          # fast probe, < 50 ms
        return MDFCANReader(path, DBCDecoder(dbc_path))
    else:
        return MDFReader(path)                   # pre-decoded path

``BusChannel`` field maps to CAN channel numbers directly — 1-indexed
as stored in the MDF file (Vector / ASAM convention is 1-based).
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

import can

from core.models import RawFrame, DecodedSignalSample
from core.dbc_decoder import DBCDecoder


class MDFCANReadError(RuntimeError):
    pass


class MDFCANReader:
    """
    Reads an ASAM MDF4 bus logging file and decodes signals with a
    :class:`DBCDecoder`.

    ``python-can`` ``MF4Reader`` yields ``can.Message`` objects, so the
    frame-to-RawFrame conversion is identical to :class:`ASCCANReader`.

    Attributes
    ----------
    source_description : str
    has_raw_frames : bool  — always ``True``
    """

    has_raw_frames: bool = True

    def __init__(self, mdf_path: str | Path, decoder: DBCDecoder) -> None:
        self._path    = Path(mdf_path)
        self._decoder = decoder
        fmt = "MF4" if self._path.suffix.lower() == ".mf4" else "MDF"
        self.source_description = (
            f"{fmt} bus log + DBC  ({self._path.name} / {self._decoder.dbc_path.name})"
        )
        self.load_messages: list[str] = [
            f"MDF bus logging: opening {fmt} file (raw CAN frames)…",
            f"DBC: {self._decoder.dbc_path.name}",
        ] + list(decoder.load_messages)

    # ── Protocol iterator ─────────────────────────────────────────────────

    def __iter__(self) -> Iterator[DecodedSignalSample]:
        for _frame, samples in self.iter_with_frames():
            yield from samples

    # ── Extended iterator (used by LoadWorker) ────────────────────────────

    def iter_with_frames(self) -> Iterator[tuple[RawFrame, list[DecodedSignalSample]]]:
        """
        Yield (RawFrame, decoded_samples) pairs — identical contract to
        BLFCANReader and ASCCANReader.

        python-can's MF4Reader handles the asammdf call internally.
        ``BusChannel`` is already 1-indexed in the MDF4 bus logging standard
        (unlike BLF/ASC which use 0-indexed and need +1).
        """
        if not self._path.exists():
            raise MDFCANReadError(f"MDF file not found: {self._path}")

        try:
            # python-can MF4Reader requires asammdf installed
            reader = can.MF4Reader(str(self._path))
        except AttributeError:
            raise MDFCANReadError(
                "python-can's MF4Reader is not available.\n"
                "Ensure python-can >= 4.6 and asammdf >= 7.0 are installed."
            )
        except Exception as exc:
            raise MDFCANReadError(
                f"Failed to open MDF bus log '{self._path}': {exc}"
            ) from exc

        try:
            with reader:
                for msg in reader:
                    if not hasattr(msg, "arbitration_id"):
                        continue

                    data = bytes(msg.data or b"")
                    is_rx = getattr(msg, "is_rx", None)
                    direction = (
                        "Rx"      if is_rx is True  else
                        "Tx"      if is_rx is False else
                        "Unknown"
                    )

                    # MDF bus logging BusChannel is already 1-indexed
                    raw_ch = getattr(msg, "channel", None)
                    channel = int(raw_ch) if isinstance(raw_ch, (int, float)) else None

                    frame = RawFrame(
                        timestamp      = float(msg.timestamp),
                        channel        = channel,
                        arbitration_id = int(msg.arbitration_id),
                        is_extended_id = bool(getattr(msg, "is_extended_id", False)),
                        is_fd          = bool(getattr(msg, "is_fd", False)),
                        dlc            = int(getattr(msg, "dlc", len(data))),
                        data           = data,
                        direction      = direction,
                    )
                    samples = self._decoder.decode_frame(frame)
                    yield frame, samples

        except MDFCANReadError:
            raise
        except Exception as exc:
            raise MDFCANReadError(
                f"Error reading MDF bus log '{self._path}': {exc}"
            ) from exc

    @property
    def decoder(self) -> DBCDecoder:
        return self._decoder
