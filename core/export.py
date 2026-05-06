from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable

from core.signal_store import SignalSeries


class ExportService:
    @staticmethod
    def export_series_to_csv(series_items: Iterable[SignalSeries], path: str | Path) -> None:
        path = Path(path)
        with path.open("w", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["channel", "message_name", "message_id", "signal_name", "unit", "timestamp", "value", "raw_value"])
            for series in series_items:
                channel_value = "" if series.channel is None else series.channel
                # raw_values is only populated for label-bearing signals; for
                # plain numeric signals fall back to the numeric value so the
                # exported "raw_value" column is never blank.
                has_labels = series.has_labels and bool(series.raw_values)
                for idx, (timestamp, value) in enumerate(zip(series.timestamps, series.values)):
                    raw = series.raw_values[idx] if has_labels else value
                    writer.writerow([
                        channel_value,
                        series.message_name,
                        hex(series.message_id),
                        series.signal_name,
                        series.unit,
                        timestamp,
                        value,
                        raw,
                    ])
