from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterator, List, Sequence

import numpy as np

from core.signal_store import SignalSeries

# The exported table is *wide*: column A is a shared "Time" axis (the union of
# every signal's sample timestamps) and each following column is one signal,
# forward-filled onto that axis. Both the CSV and Excel writers consume the same
# grid + row generator below so the two formats can never diverge.

# Excel worksheets are capped at 1,048,576 rows (including the header row).
EXCEL_MAX_ROWS = 1_048_576

# Cap on the shared timebase length. The union of many multi-million-sample
# signals can be enormous; past this we fall back to the densest signal's own
# grid to bound memory. Mirrors _MAX_GRID_POINTS in
# core/diagnostics/rules/expression.py, which uses the same union + zero-order-
# hold approach to put CAN signals on a common timebase.
_MAX_GRID_POINTS = 5_000_000

_TIME_HEADER = "Time"


def _build_grid(series_items: Sequence[SignalSeries]) -> np.ndarray:
    """Shared time axis: the sorted union of every signal's timestamps.

    Falls back to the densest signal's own timestamps when the union would
    exceed _MAX_GRID_POINTS, to keep memory bounded on huge measurements.
    """
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
    def count_data_rows(series_items: Sequence[SignalSeries]) -> int:
        """Number of data rows (header excluded) the wide export will hold.

        Equals the length of the shared time axis. Used by the GUI to decide
        whether the table exceeds Excel's row limit before writing.
        """
        return int(_build_grid(series_items).size)

    @staticmethod
    def export_series_to_csv(series_items: Sequence[SignalSeries], path: str | Path) -> None:
        series_items = list(series_items)
        grid = _build_grid(series_items)
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

        series_items = list(series_items)
        grid = _build_grid(series_items)

        hard_cap = EXCEL_MAX_ROWS - 1  # reserve one row for the header
        cap = hard_cap if max_data_rows is None else min(max_data_rows, hard_cap)

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
