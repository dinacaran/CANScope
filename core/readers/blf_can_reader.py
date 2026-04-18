from __future__ import annotations

from pathlib import Path
from typing import Iterator

from core.blf_reader import BLFReaderService, RawFrame
from core.dbc_decoder import DBCDecoder, DecodedSignalSample


class BLFCANReader:
    """
    Reads a Vector Binary Logging Format (.blf) file and decodes signals
    using an already-constructed :class:`DBCDecoder`.

    This reader yields *(frame, samples)* pairs internally and exposes
    them through two public interfaces used by :class:`LoadWorker`:

    * ``__iter__``  → ``DecodedSignalSample`` stream (protocol-compatible)
    * ``iter_with_frames`` → ``(RawFrame, list[DecodedSignalSample])`` pairs
      so the worker can also call ``store.note_frame`` / ``store.add_raw_frame``.

    Attributes
    ----------
    source_description : str
    has_raw_frames : bool
        Always ``True`` — BLF carries raw CAN bytes.
    """

    source_description: str
    has_raw_frames: bool = True

    def __init__(self, blf_path: str | Path, decoder: DBCDecoder) -> None:
        self._path    = Path(blf_path)
        self._decoder = decoder
        self.source_description = (
            f"BLF + DBC  ({self._path.name} / {self._decoder.dbc_path.name})"
        )
        # Expose decoder load messages for diagnostics
        self.load_messages: list[str] = list(decoder.load_messages)

    # ── Protocol-required iterator ────────────────────────────────────────

    def __iter__(self) -> Iterator[DecodedSignalSample]:
        """Yield decoded samples only (protocol-compatible path)."""
        for _frame, samples in self.iter_with_frames():
            yield from samples

    # ── Extended iterator used by LoadWorker ──────────────────────────────

    def iter_with_frames(self) -> Iterator[tuple[RawFrame, list[DecodedSignalSample]]]:
        """
        Yield (RawFrame, decoded_samples) pairs.

        * ``decoded_samples`` is empty when the frame matched no DBC entry.
        * The worker calls ``store.note_frame(frame)`` for every frame and
          ``store.add_raw_frame(frame, samples)`` when samples is non-empty.
        """
        reader = BLFReaderService(self._path)
        for frame in reader:
            samples = self._decoder.decode_frame(frame)
            yield frame, samples

    @property
    def decoder(self) -> DBCDecoder:
        """Expose the decoder so LoadWorker can call diagnostics_text()."""
        return self._decoder
