from __future__ import annotations

from pathlib import Path
from typing import Callable

from core.readers.base import MeasurementReader, UnsupportedFormatError


# ── Formats that always require DBC (regardless of file content) ──────────
CAN_RAW_SUFFIXES = {'.blf', '.asc'}

# ── All supported measurement file suffixes ───────────────────────────────
ALL_SUFFIXES = {'.blf', '.asc', '.mf4', '.mdf', '.csv'}

# Maximum frames to read during a lightweight pre-scan (keeps it < 1 s)
_PRESCAN_LIMIT = 50_000


def prescan_measurement(
    path: str,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[int], dict[int, set[int]]]:
    """
    Lightweight pre-scan: read up to *_PRESCAN_LIMIT* raw CAN frames from
    *path* and return ``(channels, ids_per_channel)`` without decoding signals.

    Works for BLF, ASC, MDF bus logging, and raw CAN CSV exports. Pre-decoded
    CSV/MDF files return empty results (channels come from decoded signals).

    Typically finishes in < 1 second even for 100 MB+ files.
    """
    suffix = Path(path).suffix.lower()
    channels: set[int] = set()
    ids_per_channel: dict[int, set[int]] = {}

    if suffix == '.csv':
        from core.readers.csv_reader import (
            is_can_bus_logging_csv,
            prescan_can_bus_logging_csv,
        )
        if not is_can_bus_logging_csv(path):
            return [], {}
        if progress:
            progress("Scanning measurement for channels…")
        try:
            return prescan_can_bus_logging_csv(path, _PRESCAN_LIMIT)
        except Exception:
            return [], {}

    if suffix not in CAN_RAW_SUFFIXES and suffix not in ('.mf4', '.mdf'):
        return [], {}

    if suffix in ('.mf4', '.mdf'):
        from core.readers.mdf_reader import MDFReader
        if not MDFReader.is_bus_logging(path):
            return [], {}

    if progress:
        progress("Scanning measurement for channels…")

    try:
        _prescan_can_messages(path, suffix, channels, ids_per_channel)
    except Exception:
        pass

    sorted_chs = sorted(channels)
    return sorted_chs, ids_per_channel


def _prescan_can_messages(
    path: str,
    suffix: str,
    channels: set[int],
    ids_per_channel: dict[int, set[int]],
) -> None:
    """Iterate raw CAN messages from *path* using python-can readers."""
    import can

    if suffix == '.blf':
        reader = can.BLFReader(str(path))
    elif suffix == '.asc':
        reader = can.ASCReader(str(path))
    else:
        reader = can.MF4Reader(str(path))

    is_mdf = suffix in ('.mf4', '.mdf')
    count = 0
    with reader:
        for msg in reader:
            if not hasattr(msg, 'arbitration_id'):
                continue
            raw_ch = getattr(msg, 'channel', None)
            if is_mdf:
                ch = int(raw_ch) if isinstance(raw_ch, (int, float)) else None
            else:
                ch = (int(raw_ch) + 1) if isinstance(raw_ch, (int, float)) else None
            if ch is not None:
                channels.add(ch)
                ids_per_channel.setdefault(ch, set()).add(
                    int(msg.arbitration_id)
                )
            count += 1
            if count >= _PRESCAN_LIMIT:
                break


def dbc_required_for(path: str) -> bool:
    """
    Return True when the measurement file needs a DBC to decode signals.

    For .blf / .asc: always True.
    For .mf4 / .mdf: True only for raw-only CAN bus recordings. Mixed MDFs
        containing existing decoded signals can load those signals without a
        database, so they return False.
    For .csv: True for raw CAN-frame exports; False for decoded signal CSV.
    """
    suffix = Path(path).suffix.lower()
    if suffix in CAN_RAW_SUFFIXES:
        return True
    if suffix in ('.mf4', '.mdf'):
        from core.readers.mdf_reader import MDFReader
        content = MDFReader.content_info(path)
        return content.has_raw_can and not content.has_decoded_signals
    if suffix == '.csv':
        from core.readers.csv_reader import is_can_bus_logging_csv
        return is_can_bus_logging_csv(path)
    return False


def has_mixed_mdf_content(path: str) -> bool:
    """Return whether an MDF contains decoded signals and raw CAN frames."""
    if Path(path).suffix.lower() not in ('.mf4', '.mdf'):
        return False
    from core.readers.mdf_reader import MDFReader
    return MDFReader.content_info(path).is_mixed


def reader_factory(
    measurement_path: str,
    dbc_path: str | None = None,
) -> MeasurementReader:
    """
    Return the appropriate MeasurementReader for *measurement_path*.

    MDF4 / MDF routing
    ------------------
    A metadata-only probe distinguishes decoded, raw-only, and mixed MDFs:
    - Raw-only MDF → ``MDFCANReader`` and a database is required.
    - Decoded MDF → ``MDFReader`` without a database.
    - Mixed MDF without a database → ``MDFReader`` (decoded signals win).
    - Mixed MDF with a database → ``MDFCANReader`` for CAN decode/trace.

    Raises
    ------
    UnsupportedFormatError  – file extension is not recognised
    ValueError              – DBC or ARXML required for format but not supplied
    """
    suffix = Path(measurement_path).suffix.lower()

    if suffix == '.blf':
        if not dbc_path:
            raise ValueError("A DBC or ARXML file is required for BLF files.")
        from core.readers.blf_can_reader import BLFCANReader
        from core.dbc_decoder import DBCDecoder
        return BLFCANReader(measurement_path, DBCDecoder(dbc_path))

    if suffix == '.asc':
        if not dbc_path:
            raise ValueError("A DBC or ARXML file is required for ASC files.")
        from core.readers.asc_can_reader import ASCCANReader
        from core.dbc_decoder import DBCDecoder
        return ASCCANReader(measurement_path, DBCDecoder(dbc_path))

    if suffix in ('.mf4', '.mdf'):
        from core.readers.mdf_reader import MDFReader
        content = MDFReader.content_info(measurement_path)
        if content.has_raw_can and (
            not content.has_decoded_signals or dbc_path
        ):
            # Raw-only, or mixed with an explicitly configured database.
            if not dbc_path:
                raise ValueError(
                    "This MDF file contains raw CAN bus frames.\n"
                    "A DBC or ARXML file is required for signal decoding.\n"
                    "Please configure channel → database mapping via 'Open Database'."
                )
            from core.readers.mdf_can_reader import MDFCANReader
            return MDFCANReader(measurement_path, dbc_path)
        else:
            # Decoded-only, or mixed with no database: prefer existing signals.
            return MDFReader(measurement_path)

    if suffix == '.csv':
        from core.readers.csv_reader import (
            CSVRawCANReader,
            CSVSignalReader,
            is_can_bus_logging_csv,
        )
        if is_can_bus_logging_csv(measurement_path):
            if not dbc_path:
                raise ValueError(
                    "This CSV file contains raw CAN bus frames.\n"
                    "A DBC or ARXML file is required for signal decoding.\n"
                    "Please configure channel → database mapping via 'Open Database'."
                )
            from core.dbc_decoder import DBCDecoder
            return CSVRawCANReader(
                measurement_path, DBCDecoder(dbc_path)
            )
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
    "has_mixed_mdf_content",
    "prescan_measurement",
    "CAN_RAW_SUFFIXES",
    "ALL_SUFFIXES",
]
