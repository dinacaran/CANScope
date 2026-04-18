from __future__ import annotations

from pathlib import Path
from typing import Iterator

from core.dbc_decoder import DecodedSignalSample


class MDFImportError(RuntimeError):
    """Raised when asammdf is not installed."""


class MDFReadError(RuntimeError):
    pass


class MDFReader:
    """
    Reads ASAM MDF version 3 (.mdf) and version 4 (.mf4) measurement files.

    Signals are already decoded to engineering units inside MDF files, so
    **no DBC is required**.  Channel groups map to ``message_name`` in the
    resulting :class:`DecodedSignalSample` objects, preserving the
    ``CH?::GroupName::SignalName`` tree structure used by the rest of the app.

    Requires the ``asammdf`` package (``pip install asammdf>=7.0``).
    If not installed, raises :class:`MDFImportError` on instantiation.

    Attributes
    ----------
    source_description : str
    has_raw_frames : bool
        Always ``False`` — MDF contains pre-decoded engineering values only.
    """

    has_raw_frames: bool = False

    def __init__(self, mdf_path: str | Path) -> None:
        # Fail fast with a clear message if asammdf is missing
        try:
            import asammdf  # noqa: F401
        except ImportError as exc:
            raise MDFImportError(
                "The 'asammdf' package is required to read MF4/MDF files.\n"
                "Install it with:  pip install asammdf>=7.0"
            ) from exc

        self._path = Path(mdf_path)
        if not self._path.exists():
            raise MDFReadError(f"MDF file not found: {self._path}")

        fmt = "MF4" if self._path.suffix.lower() == ".mf4" else "MDF"
        self.source_description = f"{fmt}  ({self._path.name}) — asammdf"
        self.load_messages: list[str] = [
            f"asammdf: opening {fmt} file…",
            f"No DBC required — signals are pre-decoded.",
        ]

    # ── Protocol-required iterator ────────────────────────────────────────

    def __iter__(self) -> Iterator[DecodedSignalSample]:
        """
        Yield one :class:`DecodedSignalSample` per (channel, timestamp) point.

        asammdf loads one channel group at a time.  Each ``Signal`` object
        carries a name, unit, samples array, and timestamps array aligned by
        index.  We stream them to avoid loading the entire file into RAM at once.
        """
        try:
            import asammdf
        except ImportError as exc:
            raise MDFImportError(
                "asammdf not installed.  Run: pip install asammdf>=7.0"
            ) from exc

        try:
            mdf = asammdf.MDF(str(self._path))
        except Exception as exc:
            raise MDFReadError(
                f"Failed to open MDF file '{self._path}': {exc}"
            ) from exc

        try:
            yield from self._iter_channels(mdf)
        finally:
            try:
                mdf.close()
            except Exception:
                pass

    # ── Internal ──────────────────────────────────────────────────────────

    @staticmethod
    def _iter_channels(mdf) -> Iterator[DecodedSignalSample]:
        """
        Iterate over every channel in every group, yielding samples.

        Group name  → message_name   (preserves tree grouping in SignalTree)
        Channel     → signal_name
        Source info → channel field (None when absent)
        """
        for group_idx in range(len(mdf.groups)):
            group     = mdf.groups[group_idx]
            grp_name  = MDFReader._group_name(mdf, group_idx)

            for ch_idx in range(len(group.channels)):
                ch = group.channels[ch_idx]
                ch_name = getattr(ch, "name", None) or f"Ch{ch_idx}"

                # Skip the master (time) channel itself
                if getattr(ch, "channel_type", -1) == 1:   # MASTER
                    continue
                if ch_name.lower() in ("time", "t", "timestamps"):
                    continue

                try:
                    signal = mdf.get(
                        ch_name,
                        group=group_idx,
                        index=ch_idx,
                        raw=False,       # engineering values
                    )
                except Exception:
                    continue

                ts_arr   = signal.timestamps
                val_arr  = signal.samples
                unit     = str(getattr(signal, "unit", "") or "")

                if ts_arr is None or val_arr is None:
                    continue
                if len(ts_arr) != len(val_arr):
                    continue

                for ts, raw_val in zip(ts_arr, val_arr):
                    try:
                        num = float(raw_val)
                    except (TypeError, ValueError):
                        num = float("nan")

                    # Enum/text signals: val_arr may hold bytes or str
                    if isinstance(raw_val, (bytes, bytearray)):
                        display = raw_val.decode("utf-8", errors="replace").strip()
                    elif isinstance(raw_val, str):
                        display = raw_val.strip()
                    else:
                        display = num

                    yield DecodedSignalSample(
                        timestamp      = float(ts),
                        channel        = None,
                        message_id     = 0,
                        message_name   = grp_name,
                        signal_name    = ch_name,
                        value          = display,
                        unit           = unit,
                        is_extended_id = False,
                        direction      = "Unknown",
                        numeric_value  = num,
                    )

    @staticmethod
    def _group_name(mdf, group_idx: int) -> str:
        """Best-effort group label: acq_name, source name, or Group_N fallback."""
        try:
            grp = mdf.groups[group_idx]
            # asammdf 7.x: acq_name is the preferred label
            acq = getattr(grp, "channel_group", None)
            if acq:
                name = getattr(acq, "acq_name", None)
                if name:
                    return str(name)
            # Try source info
            src = getattr(grp, "source", None)
            if src:
                name = getattr(src, "name", None)
                if name:
                    return str(name)
        except Exception:
            pass
        return f"Group_{group_idx}"
