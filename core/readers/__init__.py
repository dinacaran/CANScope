from __future__ import annotations

from pathlib import Path

from core.readers.base import MeasurementReader, UnsupportedFormatError


# ── Formats that always require DBC (regardless of file content) ──────────
CAN_RAW_SUFFIXES = {'.blf', '.asc'}

# ── All supported measurement file suffixes ───────────────────────────────
ALL_SUFFIXES = {'.blf', '.asc', '.mf4', '.mdf', '.csv'}


def dbc_required_for(path: str) -> bool:
    """
    Return True when the measurement file needs a DBC to decode signals.

    For .blf / .asc: always True.
    For .mf4 / .mdf: True only when the file contains raw CAN bus frames
        (ASAM MDF bus logging format, detected by CAN_DataFrame.* channels).
        Pre-decoded MDF files return False.
    For .csv: always False.
    """
    suffix = Path(path).suffix.lower()
    if suffix in CAN_RAW_SUFFIXES:
        return True
    if suffix in ('.mf4', '.mdf'):
        from core.readers.mdf_reader import MDFReader
        return MDFReader.is_bus_logging(path)
    return False


def reader_factory(
    measurement_path: str,
    dbc_path: str | None = None,
) -> MeasurementReader:
    """
    Return the appropriate MeasurementReader for *measurement_path*.

    MDF4 / MDF routing
    ------------------
    A fast probe (< 50 ms) checks for ``CAN_DataFrame.*`` channels:
    - Bus logging MDF  → ``MDFCANReader`` (raw frames + DBC, same as BLF/ASC)
    - Pre-decoded MDF  → ``MDFReader``    (vectorised channel arrays, no DBC)

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
        if MDFReader.is_bus_logging(measurement_path):
            # Bus logging — raw CAN frames → needs DBC
            if not dbc_path:
                raise ValueError(
                    "This MDF file contains raw CAN bus frames.\n"
                    "A DBC file is required for signal decoding.\n"
                    "Please configure channel → DBC mapping via 'Open DBC'."
                )
            from core.readers.mdf_can_reader import MDFCANReader
            from core.dbc_decoder import DBCDecoder
            return MDFCANReader(measurement_path, DBCDecoder(dbc_path))
        else:
            # Pre-decoded engineering values — no DBC needed
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
