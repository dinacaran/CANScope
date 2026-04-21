from __future__ import annotations

from core.models import RawFrame  # noqa: F401 — re-exported

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

import can



class BLFReadError(RuntimeError):
    pass


class BLFReaderService:
    """Read Vector BLF files through python-can."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def __iter__(self) -> Iterator[RawFrame]:
        if not self.path.exists():
            raise BLFReadError(f"BLF file not found: {self.path}")

        try:
            with can.BLFReader(str(self.path)) as reader:
                for msg in reader:
                    data = bytes(msg.data or b"")
                    yield RawFrame(
                        timestamp=float(msg.timestamp),
                        channel=getattr(msg, "channel", None),
                        arbitration_id=int(msg.arbitration_id),
                        is_extended_id=bool(getattr(msg, "is_extended_id", False)),
                        is_fd=bool(getattr(msg, "is_fd", False)),
                        dlc=int(getattr(msg, "dlc", len(data))),
                        data=data,
                        direction=self._direction(msg),
                    )
        except Exception as exc:  # pragma: no cover - runtime protection
            raise BLFReadError(f"Failed to read BLF file '{self.path}': {exc}") from exc

    @staticmethod
    def _direction(msg: can.Message) -> str:
        is_rx = getattr(msg, "is_rx", None)
        if is_rx is True:
            return "Rx"
        if is_rx is False:
            return "Tx"
        return "Unknown"
