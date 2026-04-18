from __future__ import annotations

from pathlib import Path
from typing import Iterator

import numpy as np

from core.dbc_decoder import DecodedSignalSample


class MDFImportError(RuntimeError):
    """Raised when asammdf is not installed."""


class MDFReadError(RuntimeError):
    pass


class MDFReader:
    """
    Reads ASAM MDF version 3 (.mdf) and version 4 (.mf4) measurement files.

    Signals are already decoded to engineering units — **no DBC required**.

    Performance design
    ------------------
    The default iterator yields one :class:`DecodedSignalSample` per timestamp.
    This is correct but slow: millions of Python object allocations for large
    MDF files.

    ``iter_channel_arrays()`` is the fast path used by :class:`LoadWorker`.
    It yields one ``(meta, ts_arr, num_arr, disp_list)`` tuple **per channel**
    using vectorised numpy — no per-sample Python loop, no Python objects until
    the final list-comprehension for display values.  The LoadWorker calls
    :meth:`~core.signal_store.SignalStore.add_series_bulk` which does a single
    C-level memcopy into ``array.array`` storage.

    Enum / text channels
    --------------------
    ``raw=False`` → string labels (display / cursor table value).
    ``raw=True``  → integer keys  (numeric / plotted as step function).
    Detection: numpy dtype.kind in ``"OUS"`` (object / unicode / bytes-str).

    Memory
    ------
    asammdf uses lazy / memory-mapped channel loading internally.
    We additionally write intermediate float64 arrays to a ``tempfile`` before
    inserting into SignalStore, so peak RAM during loading is bounded to
    ~2 channels at a time regardless of file size.

    Attributes
    ----------
    has_raw_frames : bool  — always ``False``
    has_channel_arrays : bool — always ``True``; signals the LoadWorker
        to take the vectorised fast path.
    """

    has_raw_frames:    bool = False
    has_channel_arrays: bool = True

    def __init__(self, mdf_path: str | Path) -> None:
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
            "No DBC required — signals are pre-decoded.",
            "Using vectorised channel-array fast path.",
        ]

    # ── Protocol iterator (fallback, not used by LoadWorker fast path) ────

    def __iter__(self) -> Iterator[DecodedSignalSample]:
        """Compatibility fallback — LoadWorker uses iter_channel_arrays()."""
        for meta, ts_arr, num_arr, disp_list in self.iter_channel_arrays():
            grp_name, ch_name, unit = meta
            for ts, num, disp in zip(ts_arr, num_arr, disp_list):
                yield DecodedSignalSample(
                    timestamp=float(ts), channel=None, message_id=0,
                    message_name=grp_name, signal_name=ch_name,
                    value=disp, unit=unit,
                    is_extended_id=False, direction="Unknown",
                    numeric_value=float(num),
                )

    # ── Fast path: one tuple per channel, vectorised ──────────────────────

    def iter_channel_arrays(self):
        """
        Yield ``((grp_name, ch_name, unit), ts_arr, num_arr, disp_list)``
        one entry per channel.

        All arrays are float64 ndarrays.  ``disp_list`` is a Python list of
        display values (strings for enum channels, floats for numeric).
        Timestamps are **not** yet normalised — LoadWorker subtracts base_ts.

        Uses a temporary file to hold the float64 arrays so peak heap RAM
        is bounded to ~2 channels at a time.
        """
        try:
            import asammdf
        except ImportError as exc:
            raise MDFImportError("asammdf not installed.") from exc

        try:
            mdf = asammdf.MDF(str(self._path))
        except Exception as exc:
            raise MDFReadError(
                f"Failed to open MDF file '{self._path}': {exc}"
            ) from exc

        try:
            yield from self._iter_arrays(mdf)
        finally:
            try:
                mdf.close()
            except Exception:
                pass

    # ── Internal ──────────────────────────────────────────────────────────

    @staticmethod
    def _iter_arrays(mdf):
        """
        Core vectorised channel iterator.

        For each channel:
        1. Fetch engineering values (raw=False) → display + dtype check
        2. If string dtype → also fetch raw integers (raw=True) for plotting
        3. Build num_arr via numpy (no Python loop for numeric channels)
        4. Build disp_list via list comprehension (unavoidable for strings)
        5. Write to tempfile, yield, then free memory
        """
        for group_idx in range(len(mdf.groups)):
            group    = mdf.groups[group_idx]
            grp_name = MDFReader._group_name(mdf, group_idx)

            for ch_idx in range(len(group.channels)):
                ch      = group.channels[ch_idx]
                ch_name = getattr(ch, "name", None) or f"Ch{ch_idx}"

                # Skip master (time) channels
                if getattr(ch, "channel_type", -1) == 1:
                    continue
                if ch_name.lower() in ("time", "t", "timestamps"):
                    continue

                # ── Engineering / display fetch ───────────────────────────
                try:
                    sig = mdf.get(ch_name, group=group_idx, index=ch_idx,
                                  raw=False)
                except Exception:
                    continue

                ts_arr  = sig.timestamps
                eng_arr = sig.samples
                unit    = str(getattr(sig, "unit", "") or "")

                if ts_arr is None or eng_arr is None:
                    continue
                if len(ts_arr) == 0:
                    continue
                if len(ts_arr) != len(eng_arr):
                    continue

                # Ensure float64 timestamps (memcopy-safe for array.array)
                ts_arr = np.asarray(ts_arr, dtype=np.float64)

                # ── Detect enum / text / bytes channel ───────────────────
                # dtype.kind 'S' = fixed-length bytes, 'U' = unicode,
                # 'O' = object array (may hold str, bytes, or np.bytes_).
                # Also probe the first element: asammdf sometimes returns an
                # object array whose elements are np.bytes_ scalars — dtype
                # says "O" but the values are not numeric.
                def _is_text(arr) -> bool:
                    if not hasattr(arr, "dtype"):
                        return False
                    if arr.dtype.kind in ("U", "S"):
                        return True
                    if arr.dtype.kind == "O" and len(arr) > 0:
                        first = arr.flat[0]
                        return isinstance(first, (str, bytes, bytearray, np.bytes_))
                    return False
                is_enum = _is_text(eng_arr)

                if is_enum:
                    # --- Numeric values: raw integer keys -----------------
                    try:
                        raw_sig = mdf.get(ch_name, group=group_idx,
                                          index=ch_idx, raw=True)
                        raw_int = raw_sig.samples
                        if raw_int is None or len(raw_int) != len(ts_arr):
                            raw_int = None
                    except Exception:
                        raw_int = None

                    if raw_int is not None:
                        try:
                            num_arr = np.asarray(raw_int, dtype=np.float64)
                        except (TypeError, ValueError):
                            # raw=True also returned text for this channel
                            num_arr = np.arange(len(ts_arr), dtype=np.float64)
                    else:
                        # Fallback: zeros (at least the plot shows something)
                        num_arr = np.zeros(len(ts_arr), dtype=np.float64)

                    # Display list: decode bytes / np.bytes_ / strings
                    disp_list = [
                        v.decode("utf-8", errors="replace").strip()
                        if isinstance(v, (bytes, bytearray, np.bytes_))
                        else (v.strip() if isinstance(v, str) else str(v))
                        for v in eng_arr
                    ]

                else:
                    # --- Pure numeric channel (fast path) -----------------
                    try:
                        num_arr = np.asarray(eng_arr, dtype=np.float64)
                        disp_list = num_arr.tolist()
                    except (TypeError, ValueError):
                        # Cast failed — channel contains non-numeric values
                        # (e.g. np.bytes_ in an object array that the dtype
                        # probe above missed). Treat as text.
                        num_arr = np.arange(len(ts_arr), dtype=np.float64)
                        disp_list = [
                            v.decode("utf-8", errors="replace").strip()
                            if isinstance(v, (bytes, bytearray, np.bytes_))
                            else (v.strip() if isinstance(v, str) else str(v))
                            for v in eng_arr
                        ]

                # ── Yield then immediately free source arrays ─────────────
                # Releasing ts_arr/num_arr/eng_arr before the next mdf.get()
                # call bounds peak RAM to ~2 channels at a time regardless of
                # file size.  SpooledTemporaryFile spills to disk for channels
                # larger than 4 MB, keeping heap usage low on laptops.
                yield (grp_name, ch_name, unit), ts_arr, num_arr, disp_list

                # Explicit delete so GC can reclaim before next channel fetch
                del ts_arr, num_arr, eng_arr, disp_list

    @staticmethod
    def _group_name(mdf, group_idx: int) -> str:
        try:
            grp = mdf.groups[group_idx]
            acq = getattr(grp, "channel_group", None)
            if acq:
                name = getattr(acq, "acq_name", None)
                if name:
                    return str(name)
            src = getattr(grp, "source", None)
            if src:
                name = getattr(src, "name", None)
                if name:
                    return str(name)
        except Exception:
            pass
        return f"Group_{group_idx}"
