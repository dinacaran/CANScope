from __future__ import annotations

import csv
import re
from enum import Enum, auto
from pathlib import Path
from typing import Iterator

from core.dbc_decoder import DecodedSignalSample


class CSVReadError(RuntimeError):
    pass


class _CSVFormat(Enum):
    NARROW   = auto()   # Our export: channel,message_name,...,timestamp,value
    WIDE     = auto()   # Columnar: Timestamps,Signal1 [unit],Signal2 [unit],...
    UNKNOWN  = auto()


# ── column-header patterns ────────────────────────────────────────────────
_NARROW_REQUIRED = {"channel", "message_name", "signal_name", "timestamp", "value"}
_WIDE_TS_PATTERNS = re.compile(
    r"^(timestamps?|time|t)\s*(\[.*?\])?$", re.IGNORECASE
)
# Parse unit from column headers like "EngSpeed [rpm]" or "Speed(km/h)"
_UNIT_RE = re.compile(r"[\[(]([^\])]*)[\])]")


class CSVSignalReader:
    """
    Reads pre-decoded signal CSV files and yields
    :class:`DecodedSignalSample` objects.

    Two formats are auto-detected from the header row:

    **Narrow (our own export format)**::

        channel,message_name,message_id,signal_name,unit,timestamp,value,raw_value
        0,EngineControl,0x18FEF100,EngSpeed,rpm,0.1234,600.0,600.0

    **Wide / columnar format** (INCA, CANalyzer CSV export, MDA, Vision)::

        Timestamps [s],EngSpeed [rpm],VehicleSpeed [km/h]
        0.001,650.5,80.0
        0.002,652.1,80.1

    Column headers may or may not contain units in ``[…]`` or ``(…)``
    brackets.  Units are stripped from the signal name automatically.

    Attributes
    ----------
    source_description : str
    has_raw_frames : bool
        Always ``False``.
    """

    has_raw_frames:     bool = False
    has_channel_arrays: bool = False  # uses sample loop

    def __init__(self, csv_path: str | Path) -> None:
        self._path = Path(csv_path)
        if not self._path.exists():
            raise CSVReadError(f"CSV file not found: {self._path}")
        self.source_description = f"CSV  ({self._path.name})"
        self.load_messages: list[str] = [
            f"CSV reader: opening {self._path.name}",
        ]

    # ── Protocol-required iterator ────────────────────────────────────────

    def __iter__(self) -> Iterator[DecodedSignalSample]:
        fmt = self._detect_format()
        self.load_messages.append(f"CSV format detected: {fmt.name}")
        if fmt == _CSVFormat.NARROW:
            yield from self._read_narrow()
        elif fmt == _CSVFormat.WIDE:
            yield from self._read_wide()
        else:
            raise CSVReadError(
                f"Cannot determine CSV format for '{self._path.name}'.\n"
                "Expected either:\n"
                "  Narrow: channel,message_name,signal_name,timestamp,value,…\n"
                "  Wide:   Timestamps [s], Signal1 [unit], Signal2 [unit],…"
            )

    # ── Format detection ──────────────────────────────────────────────────

    def _detect_format(self) -> _CSVFormat:
        try:
            with self._path.open(newline="", encoding="utf-8-sig") as fh:
                reader = csv.reader(fh)
                for row in reader:
                    if not any(c.strip() for c in row):
                        continue          # skip blank / comment lines
                    headers = {c.strip().lower() for c in row}
                    if _NARROW_REQUIRED <= headers:
                        return _CSVFormat.NARROW
                    # Wide: first non-empty column looks like a timestamp header
                    first = row[0].strip() if row else ""
                    if _WIDE_TS_PATTERNS.match(first) and len(row) >= 2:
                        return _CSVFormat.WIDE
                    return _CSVFormat.UNKNOWN
        except Exception:
            return _CSVFormat.UNKNOWN
        return _CSVFormat.UNKNOWN

    # ── Narrow reader ─────────────────────────────────────────────────────

    def _read_narrow(self) -> Iterator[DecodedSignalSample]:
        """
        Expected columns (order-independent, extra columns ignored):
            channel, message_name, message_id, signal_name, unit,
            timestamp, value, raw_value
        """
        try:
            with self._path.open(newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    try:
                        ts  = float(row.get("timestamp", 0) or 0)
                        val_str = row.get("value", "") or ""
                        raw_str = row.get("raw_value", val_str) or val_str
                        try:
                            num = float(val_str)
                        except (ValueError, TypeError):
                            num = float("nan")

                        # Channel: stored as int or empty → None
                        ch_raw = (row.get("channel") or "").strip()
                        channel: int | None = int(ch_raw) if ch_raw.lstrip("-").isdigit() else None

                        msg_id_str = (row.get("message_id") or "0").strip()
                        try:
                            msg_id = int(msg_id_str, 0)
                        except (ValueError, TypeError):
                            msg_id = 0

                        yield DecodedSignalSample(
                            timestamp      = ts,
                            channel        = channel,
                            message_id     = msg_id,
                            message_name   = (row.get("message_name") or "").strip(),
                            signal_name    = (row.get("signal_name")  or "").strip(),
                            value          = raw_str if raw_str else val_str,
                            unit           = (row.get("unit") or "").strip(),
                            is_extended_id = False,
                            direction      = "Unknown",
                            numeric_value  = num,
                        )
                    except Exception:
                        continue   # skip malformed rows silently
        except Exception as exc:
            raise CSVReadError(
                f"Failed to read narrow CSV '{self._path}': {exc}"
            ) from exc

    # ── Wide reader ───────────────────────────────────────────────────────

    def _read_wide(self) -> Iterator[DecodedSignalSample]:
        """
        Expected format::

            Timestamps [s], Signal1 [rpm], Signal2 [km/h], …
            0.001, 650.5, 80.0, …

        * Units are parsed from ``[…]`` or ``(…)`` in column headers.
        * Signal names are the header text with the unit bracket stripped.
        * All signals are treated as belonging to message "CSV" on channel None.
        """
        try:
            with self._path.open(newline="", encoding="utf-8-sig") as fh:
                reader = csv.reader(fh)

                # ── Find the header row ───────────────────────────────────
                header_row: list[str] | None = None
                data_rows: list[list[str]] = []
                for row in reader:
                    if not any(c.strip() for c in row):
                        continue
                    if header_row is None:
                        header_row = row
                    else:
                        data_rows.append(row)

                if not header_row:
                    return

                # ── Parse column metadata ─────────────────────────────────
                # Index 0 is timestamps; indices 1+ are signals
                ts_col = 0
                signal_cols: list[tuple[int, str, str]] = []   # (col_idx, name, unit)

                for col_idx, hdr in enumerate(header_row):
                    hdr = hdr.strip()
                    if col_idx == ts_col:
                        continue
                    unit_match = _UNIT_RE.search(hdr)
                    unit = unit_match.group(1).strip() if unit_match else ""
                    name = _UNIT_RE.sub("", hdr).strip()
                    if not name:
                        name = f"Signal_{col_idx}"
                    signal_cols.append((col_idx, name, unit))

                if not signal_cols:
                    return

                # ── Stream rows → samples ─────────────────────────────────
                for row in data_rows:
                    if not any(c.strip() for c in row):
                        continue
                    try:
                        ts = float(row[ts_col])
                    except (IndexError, ValueError, TypeError):
                        continue

                    for col_idx, sig_name, unit in signal_cols:
                        try:
                            raw_str = row[col_idx].strip() if col_idx < len(row) else ""
                            if not raw_str:
                                continue
                            try:
                                num = float(raw_str)
                                display = num
                            except ValueError:
                                num = float("nan")
                                display = raw_str

                            yield DecodedSignalSample(
                                timestamp      = ts,
                                channel        = None,
                                message_id     = 0,
                                message_name   = "CSV",
                                signal_name    = sig_name,
                                value          = display,
                                unit           = unit,
                                is_extended_id = False,
                                direction      = "Unknown",
                                numeric_value  = num,
                            )
                        except Exception:
                            continue

        except Exception as exc:
            raise CSVReadError(
                f"Failed to read wide CSV '{self._path}': {exc}"
            ) from exc
