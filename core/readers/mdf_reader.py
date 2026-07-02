from __future__ import annotations

from pathlib import Path
from typing import Iterator

import numpy as np

from core.models import DecodedSignalSample


class MDFImportError(RuntimeError):
    """Raised when asammdf is not installed."""


class MDFReadError(RuntimeError):
    pass


def _is_text(arr) -> bool:
    """Return True when *arr* holds string/bytes samples (enum/text channel)."""
    if not hasattr(arr, "dtype"):
        return False
    if arr.dtype.kind in ("U", "S"):
        return True
    if arr.dtype.kind == "O" and len(arr) > 0:
        first = arr.flat[0]
        return isinstance(first, (str, bytes, bytearray, np.bytes_))
    return False


def _decode_str_arr(arr) -> list[str]:
    """Convert a string/bytes numpy array to a plain Python list of str."""
    return [
        v.decode("utf-8", errors="replace").strip()
        if isinstance(v, (bytes, bytearray, np.bytes_))
        else (v.strip() if isinstance(v, str) else str(v))
        for v in arr
    ]


class MDFReader:
    """
    Reads ASAM MDF version 3 (.mdf) and version 4 (.mf4) measurement files.

    Signals are already decoded to engineering units — **no database required**.

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

    Batch I/O (Bottleneck 1)
    ------------------------
    asammdf stores channels in "channel groups" that share a single compressed
    data block on disk.  The old code called ``mdf.get()`` once per channel,
    which decompressed the block each time — O(channels_per_group) wasted work.
    The new code uses ``mdf.select()`` to batch-fetch all channels from the same
    group in a single file pass, then a second ``mdf.select(raw=True)`` pass for
    enum channels only.  Typical speedup: 5–20× for groups with many channels.

    Enum / text channels (Bottleneck 5)
    ------------------------------------
    ``raw=False`` → string labels (display / cursor table value).
    ``raw=True``  → integer keys  (numeric / plotted as step function).
    Enum channels in the same group are batched into one ``select(raw=True)``
    call instead of individual ``get(raw=True)`` calls.

    Numeric disp_list (Bottleneck 2)
    ---------------------------------
    Pure numeric channels yield ``disp_list=[]`` and the LoadWorker passes
    ``has_labels=False``, skipping the O(n) Python list allocation entirely.

    is_bus_logging cache (Bottleneck 4)
    ------------------------------------
    ``is_bus_logging()`` results are cached by resolved path so repeated calls
    from ``dbc_required_for()``, ``reader_factory()``, and ``prescan_measurement()``
    open the file header only once.

    Memory
    ------
    asammdf uses lazy / memory-mapped channel loading internally.
    Yielding one channel at a time with explicit ``del`` bounds peak heap RAM
    to ~1 channel group at a time regardless of file size.

    Attributes
    ----------
    has_raw_frames : bool  — always ``False``
    has_channel_arrays : bool — always ``True``; signals the LoadWorker
        to take the vectorised fast path.
    """

    has_raw_frames:    bool = False
    has_channel_arrays: bool = True

    # Cached results of is_bus_logging() keyed by resolved absolute path.
    # Eliminates repeated header opens when dbc_required_for / reader_factory /
    # prescan_measurement all call is_bus_logging for the same file.
    _bus_logging_cache: dict[str, bool] = {}

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
            "No database required — signals are pre-decoded.",
            "Using vectorised channel-array fast path (select-batched).",
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
        display values (strings for enum channels, empty list for numeric).
        Timestamps are **not** yet normalised — LoadWorker subtracts base_ts.

        Uses ``mdf.select()`` to batch-fetch all channels within a channel
        group in one file pass (instead of one ``mdf.get()`` per channel).
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
    def is_bus_logging(mdf_path: str | Path) -> bool:
        """
        Probe an MDF file to determine if it contains raw CAN bus frames
        (ASAM MDF bus logging format) rather than pre-decoded signals.

        Bus logging MDF files store frames as ``CAN_DataFrame.*`` channels
        per the ASAM MDF bus logging standard.  The probe reads only channel
        group metadata — no sample data loaded.  Cost: < 50 ms on first call;
        subsequent calls for the same path return the cached result instantly.

        Returns True  → file has CAN_DataFrame channels → needs DBC.
        Returns False → file has pre-decoded signals   → no DBC needed.
        """
        resolved = str(Path(mdf_path).resolve())
        if resolved in MDFReader._bus_logging_cache:
            return MDFReader._bus_logging_cache[resolved]

        result = False
        try:
            import asammdf
            mdf = asammdf.MDF(str(mdf_path))
            try:
                for group in mdf.groups:
                    for ch in group.channels:
                        name = getattr(ch, "name", "") or ""
                        if name.startswith("CAN_DataFrame."):
                            result = True
                            break
                    if result:
                        break
            finally:
                try:
                    mdf.close()
                except Exception:
                    pass
        except Exception:
            pass

        MDFReader._bus_logging_cache[resolved] = result
        return result

    @staticmethod
    def _iter_arrays(mdf):
        """
        Core vectorised channel iterator using ``mdf.select()`` for batch I/O.

        Strategy per channel group:
          1. Scan channel metadata (no data load) to build the select spec list.
          2. ``mdf.select(all_channels, raw=False)`` — one file pass for the
             entire group; returns engineering values (numbers or strings).
          3. Identify enum/text channels from the returned dtype.
          4. ``mdf.select(enum_channels, raw=True)`` — one file pass for raw
             integer keys of enum channels only.  (Skipped for all-numeric groups.)
          5. Yield one tuple per channel; delete arrays to free memory early.

        Falls back to individual ``mdf.get()`` calls if ``select()`` raises.
        """
        # ── Phase 1: collect channel specs per group (metadata only) ─────
        # groups_channels: group_idx → [(ch_idx, ch_name), ...]
        groups_channels: dict[int, list[tuple[int, str]]] = {}
        for group_idx in range(len(mdf.groups)):
            group   = mdf.groups[group_idx]
            ch_list = []
            for ch_idx in range(len(group.channels)):
                ch      = group.channels[ch_idx]
                ch_name = getattr(ch, "name", None) or f"Ch{ch_idx}"
                if getattr(ch, "channel_type", -1) == 1:
                    continue
                if ch_name.lower() in ("time", "t", "timestamps"):
                    continue
                ch_list.append((ch_idx, ch_name))
            if ch_list:
                groups_channels[group_idx] = ch_list

        # ── Phase 2: batch-fetch one group at a time ──────────────────────
        for group_idx, ch_list in groups_channels.items():
            grp_name     = MDFReader._group_name(mdf, group_idx)
            select_specs = [(ch_name, group_idx, ch_idx)
                            for ch_idx, ch_name in ch_list]

            # One file-block read for all channels in this group (raw=False).
            try:
                sigs: list = mdf.select(select_specs, raw=False)
            except Exception:
                # Fall back to per-channel get on select failure.
                sigs = []
                for ch_idx, ch_name in ch_list:
                    try:
                        sigs.append(
                            mdf.get(ch_name, group=group_idx,
                                    index=ch_idx, raw=False)
                        )
                    except Exception:
                        sigs.append(None)

            # Classify each channel as enum or numeric.
            enum_mask = [
                False if sig is None else _is_text(sig.samples)
                for sig in sigs
            ]

            # One file-block read for all enum channels in this group (raw=True).
            # Batched to avoid double-decompress per enum channel (Bottleneck 5).
            raw_map: dict[int, object] = {}   # position → raw Signal
            enum_positions = [i for i, is_e in enumerate(enum_mask) if is_e]
            if enum_positions:
                raw_specs = [
                    (ch_list[i][1], group_idx, ch_list[i][0])
                    for i in enum_positions
                ]
                try:
                    raw_sigs = mdf.select(raw_specs, raw=True)
                    for j, raw_sig in enumerate(raw_sigs):
                        raw_map[enum_positions[j]] = raw_sig
                except Exception:
                    # Fallback: individual get for each enum channel.
                    for i in enum_positions:
                        ch_idx, ch_name = ch_list[i]
                        try:
                            raw_map[i] = mdf.get(
                                ch_name, group=group_idx,
                                index=ch_idx, raw=True
                            )
                        except Exception:
                            pass

            # ── Phase 3: yield one channel at a time ─────────────────────
            for i, (sig, (_ch_idx, ch_name)) in enumerate(zip(sigs, ch_list)):
                if sig is None:
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

                ts_arr = np.asarray(ts_arr, dtype=np.float64)

                if enum_mask[i]:
                    # ── Enum / text channel ───────────────────────────────
                    raw_sig = raw_map.get(i)
                    raw_int = None
                    if raw_sig is not None:
                        raw_int = raw_sig.samples
                        if raw_int is None or len(raw_int) != len(ts_arr):
                            raw_int = None

                    if raw_int is not None:
                        try:
                            num_arr = np.asarray(raw_int, dtype=np.float64)
                        except (TypeError, ValueError):
                            num_arr = np.arange(len(ts_arr), dtype=np.float64)
                    else:
                        num_arr = np.zeros(len(ts_arr), dtype=np.float64)

                    disp_list = _decode_str_arr(eng_arr)

                else:
                    # ── Pure numeric channel ──────────────────────────────
                    try:
                        num_arr   = np.asarray(eng_arr, dtype=np.float64)
                        disp_list = []  # has_labels=False skips raw_values alloc
                    except (TypeError, ValueError):
                        # Cast failed — treat as text (missed by dtype probe).
                        num_arr   = np.arange(len(ts_arr), dtype=np.float64)
                        disp_list = _decode_str_arr(eng_arr)

                yield (grp_name, ch_name, unit), ts_arr, num_arr, disp_list

                # Explicit delete so GC can reclaim before the next channel.
                del ts_arr, num_arr, eng_arr, disp_list

            # Release the batch of Signal objects before the next group.
            del sigs

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
