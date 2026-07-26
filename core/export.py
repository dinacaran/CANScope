from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Sequence

import numpy as np

from core.signal_store import SignalSeries

# The exported table is *wide*: column A is the selected shared "Time" axis and
# each following column is one signal, forward-filled onto that axis. Both the
# CSV and Excel writers consume the same grid + row generator below so the two
# formats can never diverge.

# Excel worksheets are capped at 1,048,576 rows (including the header row).
EXCEL_MAX_ROWS = 1_048_576

# Cap on the shared timebase length. The union of many multi-million-sample
# signals can be enormous; past this we fall back to the densest signal's own
# grid to bound memory. Mirrors _MAX_GRID_POINTS in
# core/diagnostics/rules/expression.py, which uses the same union + zero-order-
# hold approach to put CAN signals on a common timebase.
_MAX_GRID_POINTS = 5_000_000

_TIME_HEADER = "Time"


@dataclass(frozen=True, slots=True)
class ExportTimebase:
    """Explicit timestamp source selected by the user for an export."""

    reference_key: str | None = None
    recurrence_seconds: float | None = None

    @classmethod
    def from_reference_signal(cls, key: str) -> "ExportTimebase":
        return cls(reference_key=key)

    @classmethod
    def from_recurrence(cls, seconds: float) -> "ExportTimebase":
        return cls(recurrence_seconds=float(seconds))


def _legacy_union_grid(series_items: Sequence[SignalSeries]) -> np.ndarray:
    """Legacy shared time axis retained for non-GUI API compatibility."""
    grids = [
        s.numpy_timestamps() for s in series_items
        if len(s.timestamps) > 0
    ]
    if not grids:
        return np.empty(0, dtype=np.float64)
    union = np.unique(np.concatenate(grids))
    if union.size > _MAX_GRID_POINTS:
        return np.asarray(max(grids, key=len), dtype=np.float64)
    return union


def _regular_grid(
    series_items: Sequence[SignalSeries],
    recurrence_seconds: float,
    *,
    max_points: int | None = None,
) -> np.ndarray:
    """Regular grid spanning the complete timestamp range of plotted signals."""
    if not np.isfinite(recurrence_seconds) or recurrence_seconds <= 0:
        raise ValueError("Manual recurrence time must be greater than zero.")

    non_empty = [
        s.numpy_timestamps() for s in series_items
        if len(s.timestamps) > 0
    ]
    if not non_empty:
        return np.empty(0, dtype=np.float64)

    start = min(float(ts[0]) for ts in non_empty)
    stop = max(float(ts[-1]) for ts in non_empty)
    if not np.isfinite(start) or not np.isfinite(stop) or stop < start:
        raise ValueError("Signal timestamps are invalid or not sorted.")

    ratio = (stop - start) / recurrence_seconds
    rounded_ratio = round(ratio)
    if np.isclose(ratio, rounded_ratio, rtol=1e-12, atol=1e-12):
        step_count = int(rounded_ratio)
    else:
        step_count = int(np.floor(ratio))
    point_count = step_count + 1
    if max_points is not None:
        point_count = min(point_count, max_points)
    return start + np.arange(point_count, dtype=np.float64) * recurrence_seconds


def _regular_grid_size(
    series_items: Sequence[SignalSeries],
    recurrence_seconds: float,
) -> int:
    """Count a regular grid without allocating it."""
    if not np.isfinite(recurrence_seconds) or recurrence_seconds <= 0:
        raise ValueError("Manual recurrence time must be greater than zero.")
    non_empty = [
        s.numpy_timestamps() for s in series_items
        if len(s.timestamps) > 0
    ]
    if not non_empty:
        return 0
    start = min(float(ts[0]) for ts in non_empty)
    stop = max(float(ts[-1]) for ts in non_empty)
    if not np.isfinite(start) or not np.isfinite(stop) or stop < start:
        raise ValueError("Signal timestamps are invalid or not sorted.")
    ratio = (stop - start) / recurrence_seconds
    rounded_ratio = round(ratio)
    if np.isclose(ratio, rounded_ratio, rtol=1e-12, atol=1e-12):
        return int(rounded_ratio) + 1
    return int(np.floor(ratio)) + 1


def _build_grid(
    series_items: Sequence[SignalSeries],
    timebase: ExportTimebase | None = None,
    *,
    max_points: int | None = None,
) -> np.ndarray:
    """Build the selected shared time axis.

    ``None`` retains the original union behavior for callers outside the GUI.
    GUI exports always provide either a reference signal or manual recurrence.
    """
    if timebase is None:
        return _legacy_union_grid(series_items)

    has_reference = timebase.reference_key is not None
    has_recurrence = timebase.recurrence_seconds is not None
    if has_reference == has_recurrence:
        raise ValueError(
            "Select exactly one export timestamp source: "
            "a reference signal or a manual recurrence time."
        )

    if has_reference:
        reference = next(
            (series for series in series_items if series.key == timebase.reference_key),
            None,
        )
        if reference is None:
            raise ValueError(
                f"Reference signal is not plotted: {timebase.reference_key}"
            )
        grid = np.asarray(reference.numpy_timestamps(), dtype=np.float64)
        return grid if max_points is None else grid[:max_points]

    return _regular_grid(
        series_items,
        float(timebase.recurrence_seconds),
        max_points=max_points,
    )


def _column_headers(series_items: Sequence[SignalSeries]) -> List[str]:
    """One header per signal — the signal name, or its full key on collision."""
    names = [s.signal_name for s in series_items]
    seen: dict[str, int] = {}
    for n in names:
        seen[n] = seen.get(n, 0) + 1
    return [
        s.signal_name if seen[s.signal_name] == 1 else s.key
        for s in series_items
    ]


def _zoh_column(series: SignalSeries, grid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Zero-order-hold a signal onto ``grid``.

    Returns ``(values, valid)`` where ``values[k]`` is the signal's most recent
    value at or before ``grid[k]`` and ``valid[k]`` is False for grid points that
    precede the signal's first sample (blank in the output). Label-bearing
    signals return their label text; plain signals return the numeric value.
    """
    ts = series.numpy_timestamps()
    if ts.size == 0 or grid.size == 0:
        return np.empty(grid.size, dtype=object), np.zeros(grid.size, dtype=bool)
    idx = np.searchsorted(ts, grid, side="right") - 1
    valid = idx >= 0
    safe = np.clip(idx, 0, ts.size - 1)
    if series.has_labels and series.raw_values:
        raw = np.array(series.raw_values, dtype=object)
        safe = np.clip(idx, 0, raw.size - 1)
        values = raw[safe]
    else:
        values = series.numpy_values()[safe]
    return values, valid


def _iter_wide_rows(
    series_items: Sequence[SignalSeries],
    grid: np.ndarray,
    *,
    blank,
    max_rows: int | None = None,
) -> Iterator[list]:
    """Yield one row per grid point: [time, signal_0, signal_1, ...].

    Cells with no held value (before a signal's first sample) are ``blank`` —
    ``""`` for CSV, ``None`` for Excel.
    """
    columns = [_zoh_column(s, grid) for s in series_items]
    n = grid.size if max_rows is None else min(grid.size, max_rows)
    for k in range(n):
        row = [float(grid[k])]
        for values, valid in columns:
            if valid[k]:
                v = values[k]
                # numpy scalar -> native Python for csv / openpyxl.
                row.append(v.item() if isinstance(v, np.generic) else v)
            else:
                row.append(blank)
        yield row


def _col_letter(index_zero_based: int) -> str:
    """Excel column letter for a 0-based column index (0->A, 26->AA, ...)."""
    n = index_zero_based + 1
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


class ExportService:
    # Re-exported so callers can reference the limit without importing the module
    # constant directly (keeps the GUI's dependency surface to ExportService).
    EXCEL_MAX_ROWS = EXCEL_MAX_ROWS

    @staticmethod
    def typical_recurrence_seconds(series: SignalSeries) -> float | None:
        """Median positive sample interval, for an informative UI label."""
        timestamps = series.numpy_timestamps()
        if timestamps.size < 2:
            return None
        deltas = np.diff(timestamps)
        usable = deltas[np.isfinite(deltas) & (deltas > 0)]
        if usable.size == 0:
            return None
        return float(np.median(usable))

    @staticmethod
    def count_data_rows(
        series_items: Sequence[SignalSeries],
        *,
        timebase: ExportTimebase | None = None,
    ) -> int:
        """Number of data rows (header excluded) the wide export will hold.

        Equals the length of the selected time axis. Used by the GUI to decide
        whether the table exceeds Excel's row limit before writing.
        """
        if timebase is not None and timebase.reference_key is not None:
            reference = next(
                (
                    series for series in series_items
                    if series.key == timebase.reference_key
                ),
                None,
            )
            if reference is None:
                raise ValueError(
                    f"Reference signal is not plotted: {timebase.reference_key}"
                )
            if timebase.recurrence_seconds is not None:
                raise ValueError(
                    "Select exactly one export timestamp source: "
                    "a reference signal or a manual recurrence time."
                )
            return len(reference.timestamps)
        if timebase is not None and timebase.recurrence_seconds is not None:
            return _regular_grid_size(
                series_items, float(timebase.recurrence_seconds)
            )
        return int(_build_grid(series_items, timebase).size)

    @staticmethod
    def export_series_to_csv(
        series_items: Sequence[SignalSeries],
        path: str | Path,
        *,
        timebase: ExportTimebase | None = None,
    ) -> None:
        series_items = list(series_items)
        grid = _build_grid(series_items, timebase)
        path = Path(path)
        with path.open("w", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow([_TIME_HEADER, *_column_headers(series_items)])
            for row in _iter_wide_rows(series_items, grid, blank=""):
                writer.writerow(row)

    @staticmethod
    def export_series_to_excel(
        series_items: Sequence[SignalSeries],
        path: str | Path,
        *,
        max_data_rows: int | None = None,
        timebase: ExportTimebase | None = None,
    ) -> None:
        """Write the same wide, time-aligned table as the CSV export to .xlsx.

        Column A is the shared Time axis; each further column is one signal
        forward-filled onto it. Adds only a frozen header row and an autofilter.
        Uses openpyxl write-only mode so multi-million-row tables stream to disk.

        Data rows are capped at ``max_data_rows`` when given, and always hard-
        capped at ``EXCEL_MAX_ROWS - 1`` so an over-limit (corrupt) file can
        never be produced.
        """
        from openpyxl import Workbook

        hard_cap = EXCEL_MAX_ROWS - 1  # reserve one row for the header
        cap = hard_cap if max_data_rows is None else min(max_data_rows, hard_cap)

        series_items = list(series_items)
        grid = _build_grid(series_items, timebase, max_points=cap)

        path = Path(path)
        wb = Workbook(write_only=True)
        ws = wb.create_sheet()

        # NB: in write-only mode freeze_panes must be set BEFORE any row is
        # appended — the sheet view is serialised ahead of the row stream, so a
        # later assignment is silently dropped. auto_filter, by contrast, is
        # serialised after the rows and can be set once the count is known.
        ws.freeze_panes = "A2"

        header = [_TIME_HEADER, *_column_headers(series_items)]
        ws.append(header)
        written_rows = 1  # header
        for row in _iter_wide_rows(series_items, grid, blank=None, max_rows=cap):
            ws.append(row)
            written_rows += 1

        last_col = _col_letter(len(header) - 1)
        ws.auto_filter.ref = f"A1:{last_col}{written_rows}"

        wb.save(path)
