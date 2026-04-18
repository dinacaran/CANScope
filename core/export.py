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
                for timestamp, value, raw in zip(series.timestamps, series.values, series.raw_values):
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
