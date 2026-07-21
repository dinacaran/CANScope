from __future__ import annotations

import csv
import binascii
import re
from enum import Enum, auto
from pathlib import Path
from typing import Iterator

from core.models import DecodedSignalSample, RawFrame


class CSVReadError(RuntimeError):
    pass


class _CSVFormat(Enum):
    NARROW   = auto()   # Our export: channel,message_name,...,timestamp,value
    WIDE     = auto()   # Columnar: Timestamps,Signal1 [unit],Signal2 [unit],...
    CAN_RAW  = auto()   # asammdf CAN-frame export: TimestampEpoch;BusChannel;...
    UNKNOWN  = auto()


# ── column-header patterns ────────────────────────────────────────────────
_NARROW_REQUIRED = {"channel", "message_name", "signal_name", "timestamp", "value"}
_WIDE_TS_PATTERNS = re.compile(
    r"^(timestamps?|time|t)\s*(\[.*?\])?$", re.IGNORECASE
)
# Parse unit from column headers like "EngSpeed [rpm]" or "Speed(km/h)"
_UNIT_RE = re.compile(r"[\[(]([^\])]*)[\])]")

_CAN_RAW_REQUIRED = {"timestampepoch", "buschannel", "id", "databytes"}


def _normalise_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.strip().lower())


def _header_info(path: Path) -> tuple[_CSVFormat, str, list[str]]:
    """Return ``(format, delimiter, headers)`` from the first non-empty row."""
    try:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            for line in fh:
                if not line.strip():
                    continue
                delimiter = max((";", ",", "\t"), key=line.count)
                row = next(csv.reader([line], delimiter=delimiter))
                normalised = {_normalise_header(column) for column in row}
                if _CAN_RAW_REQUIRED <= normalised:
                    return _CSVFormat.CAN_RAW, delimiter, row
                headers = {column.strip().lower() for column in row}
                if _NARROW_REQUIRED <= headers:
                    return _CSVFormat.NARROW, delimiter, row
                first = row[0].strip() if row else ""
                if _WIDE_TS_PATTERNS.match(first) and len(row) >= 2:
                    return _CSVFormat.WIDE, delimiter, row
                return _CSVFormat.UNKNOWN, delimiter, row
    except Exception:
        pass
    return _CSVFormat.UNKNOWN, ",", []


def is_can_bus_logging_csv(path: str | Path) -> bool:
    """Return whether *path* is an asammdf-style raw CAN-frame CSV export."""
    return _header_info(Path(path))[0] == _CSVFormat.CAN_RAW


def prescan_can_bus_logging_csv(
    path: str | Path,
    limit: int = 50_000,
) -> tuple[list[int], dict[int, set[int]]]:
    """Collect channels and CAN IDs without decoding or materialising frames."""
    csv_path = Path(path)
    fmt, delimiter, headers = _header_info(csv_path)
    if fmt != _CSVFormat.CAN_RAW:
        return [], {}
    positions = {
        _normalise_header(header): index for index, header in enumerate(headers)
    }
    channel_index = positions["buschannel"]
    id_index = positions["id"]
    delimiter_bytes = delimiter.encode("ascii")
    channels: set[int] = set()
    ids_per_channel: dict[int, set[int]] = {}
    count = 0
    with csv_path.open("rb", buffering=4 << 20) as stream:
        header_seen = False
        for line in stream:
            if not line.strip():
                continue
            if not header_seen:
                header_seen = True
                continue
            fields = line.rstrip(b"\r\n").split(delimiter_bytes)
            try:
                channel = int(fields[channel_index])
                arbitration_id = int(fields[id_index].removeprefix(b"0x"), 16)
            except (IndexError, ValueError):
                continue
            channels.add(channel)
            ids_per_channel.setdefault(channel, set()).add(arbitration_id)
            count += 1
            if count >= limit:
                break
    return sorted(channels), ids_per_channel


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
        elif fmt == _CSVFormat.CAN_RAW:
            raise CSVReadError(
                f"'{self._path.name}' contains raw CAN frames, not pre-decoded "
                "signals. Configure a DBC or ARXML database and load it again."
            )
        else:
            raise CSVReadError(
                f"Cannot determine CSV format for '{self._path.name}'.\n"
                "Expected either:\n"
                "  Narrow: channel,message_name,signal_name,timestamp,value,…\n"
                "  Wide:   Timestamps [s], Signal1 [unit], Signal2 [unit],…"
            )

    # ── Format detection ──────────────────────────────────────────────────

    def _detect_format(self) -> _CSVFormat:
        return _header_info(self._path)[0]

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


class CSVRawCANReader:
    """Read an asammdf raw CAN CSV export as packed frame-column batches."""

    has_raw_frames: bool = True

    def __init__(self, csv_path: str | Path, decoder) -> None:
        self._path = Path(csv_path)
        if not self._path.exists():
            raise CSVReadError(f"CSV file not found: {self._path}")
        if not is_can_bus_logging_csv(self._path):
            raise CSVReadError(
                f"'{self._path.name}' is not a supported raw CAN CSV export."
            )
        self._decoder = decoder
        self.source_description = (
            f"CSV CAN + DBC  ({self._path.name} / {decoder.dbc_path.name})"
        )
        self.load_messages: list[str] = list(decoder.load_messages) + [
            "Fast path: direct CSV CAN column-array extraction."
        ]

    def __iter__(self) -> Iterator[DecodedSignalSample]:
        for frame in self.iter_frames_only():
            yield from self._decoder.decode_frame(frame)

    def iter_frames_only(self) -> Iterator[RawFrame]:
        """Compatibility iterator; normal loading uses ``iter_raw_batches``."""
        directions = ("Rx", "Tx", "Unknown")
        for (
            base_ts, timestamps, channels, arb_ids, dlcs,
            direction_codes, flags, data_block,
        ) in self.iter_raw_batches():
            for index, timestamp in enumerate(timestamps):
                offset = index * 64
                dlc = int(dlcs[index])
                yield RawFrame(
                    timestamp=float(base_ts) + float(timestamp),
                    channel=None if channels[index] == 255 else int(channels[index]),
                    arbitration_id=int(arb_ids[index]),
                    is_extended_id=bool(flags[index] & 1),
                    is_fd=bool(flags[index] & 2),
                    dlc=dlc,
                    data=bytes(data_block[offset:offset + min(dlc, 64)]),
                    direction=directions[min(int(direction_codes[index]), 2)],
                )

    def iter_raw_batches(self, batch_size: int = 16_384):
        """Parse raw CAN rows directly into LoadWorker's packed batch layout."""
        fmt, delimiter, headers = _header_info(self._path)
        if fmt != _CSVFormat.CAN_RAW:
            raise CSVReadError(
                f"Cannot determine raw CAN CSV columns for '{self._path.name}'."
            )
        positions = {
            _normalise_header(header): index for index, header in enumerate(headers)
        }
        timestamp_index = positions["timestampepoch"]
        channel_index = positions["buschannel"]
        id_index = positions["id"]
        data_index = positions["databytes"]
        data_length_index = positions.get("datalength")
        direction_index = positions.get("dir")
        ide_index = positions.get("ide")
        edl_index = positions.get("edl")
        delimiter_bytes = delimiter.encode("ascii")

        timestamps: list[float] = []
        channels: list[int] = []
        arb_ids: list[int] = []
        dlcs: list[int] = []
        directions: list[int] = []
        flags: list[int] = []
        data_block = bytearray(batch_size * 64)
        base_ts: float | None = None

        try:
            with self._path.open("rb", buffering=4 << 20) as stream:
                header_seen = False
                for line in stream:
                    if not line.strip():
                        continue
                    if not header_seen:
                        header_seen = True
                        continue
                    fields = line.rstrip(b"\r\n").split(delimiter_bytes)
                    try:
                        timestamp = float(fields[timestamp_index])
                        channel = int(fields[channel_index])
                        id_token = fields[id_index].strip().lower()
                        arbitration_id = int(id_token.removeprefix(b"0x"), 16)
                        data_hex = fields[data_index].strip().replace(b" ", b"")
                        if data_hex.lower().startswith(b"0x"):
                            data_hex = data_hex[2:]
                        payload = binascii.unhexlify(data_hex) if data_hex else b""
                    except (IndexError, ValueError, binascii.Error):
                        continue

                    data_length = len(payload)
                    if data_length_index is not None:
                        try:
                            data_length = int(fields[data_length_index])
                        except (IndexError, ValueError):
                            pass
                    data_length = min(max(data_length, 0), len(payload), 64)

                    direction = 2
                    if direction_index is not None:
                        try:
                            direction_token = fields[direction_index].strip().lower()
                            direction = (
                                0 if direction_token in (b"0", b"rx")
                                else 1 if direction_token in (b"1", b"tx")
                                else 2
                            )
                        except IndexError:
                            pass

                    is_extended = arbitration_id > 0x7FF
                    if ide_index is not None:
                        try:
                            is_extended = bool(int(fields[ide_index]))
                        except (IndexError, ValueError):
                            pass
                    is_fd = data_length > 8
                    if edl_index is not None:
                        try:
                            is_fd = bool(int(fields[edl_index]))
                        except (IndexError, ValueError):
                            pass

                    if base_ts is None:
                        base_ts = timestamp
                    slot = len(timestamps)
                    timestamps.append(timestamp - base_ts)
                    channels.append(channel if 0 <= channel < 255 else 255)
                    arb_ids.append(arbitration_id & 0xFFFF_FFFF)
                    dlcs.append(data_length)
                    directions.append(direction)
                    flags.append((1 if is_extended else 0) | (2 if is_fd else 0))
                    offset = slot * 64
                    if data_length:
                        data_block[offset:offset + data_length] = payload[:data_length]

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
        except CSVReadError:
            raise
        except Exception as exc:
            raise CSVReadError(
                f"Failed to parse raw CAN CSV '{self._path}': {exc}"
            ) from exc

    @property
    def decoder(self):
        return self._decoder
