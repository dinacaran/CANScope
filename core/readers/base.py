from __future__ import annotations

from typing import Iterator, Protocol, runtime_checkable

from core.dbc_decoder import DecodedSignalSample


class UnsupportedFormatError(ValueError):
    """Raised when the measurement file extension is not supported."""


@runtime_checkable
class MeasurementReader(Protocol):
    """
    Protocol satisfied by every format-specific reader.

    A reader is an iterable that yields :class:`DecodedSignalSample` objects
    in timestamp order.  Readers are consumed once; create a new instance to
    re-read the same file.

    Attributes
    ----------
    source_description : str
        Human-readable description shown in the Log and Diagnostics tabs,
        e.g. ``"BLF + DBC"`` or ``"MDF4 (asammdf)"`` or ``"CSV (wide)"``.
    has_raw_frames : bool
        True only for CAN-raw readers (BLF, ASC) that also produce
        :class:`core.blf_reader.RawFrame` objects.  When False, the
        raw-frame dialog will show a "not available" notice.
    """

    source_description: str
    has_raw_frames: bool

    def __iter__(self) -> Iterator[DecodedSignalSample]:
        ...
