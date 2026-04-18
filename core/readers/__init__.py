from __future__ import annotations

from pathlib import Path

from core.readers.base import MeasurementReader, UnsupportedFormatError


# ── Formats that require a DBC file for decoding ──────────────────────────
CAN_RAW_SUFFIXES = {'.blf', '.asc'}

# ── All supported measurement file suffixes ───────────────────────────────
ALL_SUFFIXES = {'.blf', '.asc', '.mf4', '.mdf', '.csv'}


def dbc_required_for(path: str) -> bool:
    """Return True when the given measurement file needs a DBC to decode."""
    return Path(path).suffix.lower() in CAN_RAW_SUFFIXES


def reader_factory(
    measurement_path: str,
    dbc_path: str | None = None,
) -> MeasurementReader:
    """
    Return the appropriate MeasurementReader for *measurement_path*.

    Raises
    ------
    UnsupportedFormatError  – file extension is not recognised
    ValueError              – DBC required for format but not supplied
    """
    suffix = Path(measurement_path).suffix.lower()

    if suffix == '.blf':
        if not dbc_path:
            raise ValueError("A DBC file is required for BLF files.")
        from core.readers.blf_can_reader import BLFCANReader
        from core.dbc_decoder import DBCDecoder
        return BLFCANReader(measurement_path, DBCDecoder(dbc_path))

    if suffix == '.asc':
        if not dbc_path:
            raise ValueError("A DBC file is required for ASC files.")
        from core.readers.asc_can_reader import ASCCANReader
        from core.dbc_decoder import DBCDecoder
        return ASCCANReader(measurement_path, DBCDecoder(dbc_path))

    if suffix in ('.mf4', '.mdf'):
        from core.readers.mdf_reader import MDFReader
        return MDFReader(measurement_path)

    if suffix == '.csv':
        from core.readers.csv_reader import CSVSignalReader
        return CSVSignalReader(measurement_path)

    raise UnsupportedFormatError(
        f"Unsupported measurement file format: '{suffix}'. "
        f"Supported: {', '.join(sorted(ALL_SUFFIXES))}"
    )


__all__ = [
    "MeasurementReader",
    "UnsupportedFormatError",
    "reader_factory",
    "dbc_required_for",
    "CAN_RAW_SUFFIXES",
    "ALL_SUFFIXES",
]
