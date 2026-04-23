"""
On-disk raw CAN frame store.

Design goals
------------
* No per-frame Python objects — all per-frame metadata stored in compact
  ``array.array`` buffers (18 bytes/frame total in RAM).
* Raw data bytes stored in a temporary file on disk (64 bytes/frame).
  The file is mmap-based for O(1) random access by frame index.
* No cap on the number of frames — supports 200 MB+ BLF files (3M+ frames)
  with ~54 MB RAM + ~192 MB disk.
* Temp file is auto-deleted when ``close()`` is called or the object is GC'd.

Memory layout (in-process)
--------------------------
Per frame, 18 bytes total:
    timestamps  float64  8 B   normalised to t=0
    channels    uint8    1 B   physical channel (1-indexed; 255 = unknown)
    arb_ids     uint32   4 B   CAN arbitration ID
    dlcs        uint8    1 B   data length code
    directions  uint8    1 B   0=Rx, 1=Tx, 2=Unknown
    flags       uint8    1 B   bit0=is_extended, bit1=is_fd, bit2=decoded
    name_ids    uint16   2 B   index into name_table

name_table: list[str]  — unique message names (~100–500 entries, negligible)

Disk layout
-----------
Fixed 64-byte records (CAN + CAN-FD), one per frame.
Random access: ``offset = frame_index * _DATA_BYTES``

Filter interface
----------------
``build_match_mask(needle, channel_filter)`` returns a ``numpy`` bool array
using vectorised ops — no Python loop, no disk access.
For very large stores (> 500 k frames) data-hex search is disabled; all other
search fields (ID, name, channel, direction) remain vectorised.
"""
from __future__ import annotations

import array as _array
import mmap
import os
import struct
import tempfile
from dataclasses import dataclass, field
from typing import NamedTuple

import numpy as np


# ── Constants ─────────────────────────────────────────────────────────────
_DATA_BYTES        = 64          # bytes of raw data stored per frame (CAN FD max)
_DIR_RX,_DIR_TX,_DIR_UNK = 0, 1, 2
_LARGE_STORE_LIMIT = 500_000     # frames; disable data-hex search above this

_DIR_STRINGS = ['Rx', 'Tx', 'Unknown']


class RawFrameRecord(NamedTuple):
    """Lightweight record returned by get_window() — no heap allocation needed."""
    time_s:        float
    channel:       int | None     # None when channel field was 255
    arbitration_id: int
    dlc:           int
    direction:     str
    is_extended:   bool
    is_fd:         bool
    decoded:       bool
    data:          bytes          # up to _DATA_BYTES bytes
    frame_name:    str            # from name_table


class RawFrameStore:
    """
    Stores every raw CAN frame with near-zero RAM overhead.

    Call ``append()`` during BLF/ASC decode, then ``seal()`` once to open the
    mmap.  After sealing, call ``get_window(indices)`` to retrieve frames by
    index for display.  Call ``close()`` to release the temp file.
    """

    def __init__(self) -> None:
        # ── In-memory compact arrays ──────────────────────────────────────
        self.timestamps:  _array.array = _array.array('d')   # float64
        self.channels:    _array.array = _array.array('B')   # uint8
        self.arb_ids:     _array.array = _array.array('I')   # uint32
        self.dlcs:        _array.array = _array.array('B')   # uint8
        self.directions:  _array.array = _array.array('B')   # uint8
        self.flags:       _array.array = _array.array('B')   # uint8
        self.name_ids:    _array.array = _array.array('H')   # uint16

        # ── Name table ────────────────────────────────────────────────────
        self.name_table:  list[str] = ['']    # index 0 = no name
        self._name_to_id: dict[str, int] = {'': 0}

        # ── Decoder reference (set after decode for on-demand signal fetch)
        self.decoder = None   # DBCDecoder | None

        # ── Temp data file ────────────────────────────────────────────────
        self._data_path: str | None = None
        self._data_file = None          # file handle (write phase)
        self._mmap:      mmap.mmap | None = None   # read phase (after seal)
        self._sealed = False

        # Create temp file immediately
        fd, path = tempfile.mkstemp(prefix='canscope_', suffix='.rawdata')
        self._data_path = path
        self._data_file = os.fdopen(fd, 'w+b', buffering=0)

    # ── Write phase (during decode) ───────────────────────────────────────

    def append(self, timestamp: float, channel: int | None,
               arb_id: int, dlc: int, direction: str,
               is_extended: bool, is_fd: bool,
               data: bytes, frame_name: str, decoded: bool) -> None:
        """Append one frame.  O(1) amortised — no per-frame Python objects."""
        self.timestamps.append(timestamp)
        self.channels.append(channel if channel is not None else 255)
        self.arb_ids.append(arb_id & 0xFFFF_FFFF)
        self.dlcs.append(min(dlc, 255))
        self.directions.append(
            _DIR_RX if direction == 'Rx' else
            _DIR_TX if direction == 'Tx' else _DIR_UNK
        )
        flags = (
            (1 if is_extended else 0) |
            (2 if is_fd       else 0) |
            (4 if decoded     else 0)
        )
        self.flags.append(flags)

        # Name table lookup / insert
        nid = self._name_to_id.get(frame_name)
        if nid is None:
            nid = len(self.name_table)
            if nid > 65535:
                nid = 0   # overflow guard
            else:
                self.name_table.append(frame_name)
                self._name_to_id[frame_name] = nid
        self.name_ids.append(nid)

        # Write data bytes to disk (pad to _DATA_BYTES)
        raw = bytes(data)[:_DATA_BYTES]
        self._data_file.write(raw + b'\x00' * (_DATA_BYTES - len(raw)))

    def seal(self) -> None:
        """
        Called once after all frames have been appended.
        Flushes the data file and opens the mmap for random reads.
        Safe to call multiple times.
        """
        if self._sealed:
            return
        self._sealed = True
        if self._data_file:
            self._data_file.flush()
        n = len(self.timestamps)
        if n == 0:
            return
        # Re-open read-only handle for mmap; keep original open for ownership
        try:
            self._mmap = mmap.mmap(
                self._data_file.fileno(),
                length=0,          # map entire file
                access=mmap.ACCESS_READ,
            )
        except Exception:
            self._mmap = None   # fallback: read via file.seek/read

    def __len__(self) -> int:
        return len(self.timestamps)

    def close(self) -> None:
        """Release mmap and delete temp file."""
        if self._mmap:
            try:
                self._mmap.close()
            except Exception:
                pass
            self._mmap = None
        if self._data_file:
            try:
                self._data_file.close()
            except Exception:
                pass
            self._data_file = None
        if self._data_path and os.path.exists(self._data_path):
            try:
                os.unlink(self._data_path)
            except Exception:
                pass
            self._data_path = None

    def __del__(self) -> None:
        self.close()

    # ── Random-access read (after seal) ──────────────────────────────────

    def get_window(self, indices) -> list[RawFrameRecord]:
        """
        Return RawFrameRecord objects for the given frame indices.
        indices may be a list, range, or array.array of int frame indices.
        Reads data bytes from the mmap'd temp file.
        """
        result: list[RawFrameRecord] = []
        n = len(self.timestamps)
        for idx in indices:
            if idx < 0 or idx >= n:
                continue

            # In-memory metadata
            ts  = self.timestamps[idx]
            raw_ch = self.channels[idx]
            ch  = raw_ch if raw_ch != 255 else None
            aid = self.arb_ids[idx]
            dlc = self.dlcs[idx]
            fl  = self.flags[idx]
            nid = self.name_ids[idx]

            is_extended = bool(fl & 1)
            is_fd       = bool(fl & 2)
            decoded     = bool(fl & 4)
            direction   = _DIR_STRINGS[min(self.directions[idx], 2)]
            name        = self.name_table[nid] if nid < len(self.name_table) else ''

            # Disk data
            data = self._read_data(idx, min(dlc, _DATA_BYTES))

            result.append(RawFrameRecord(
                time_s=ts, channel=ch, arbitration_id=aid,
                dlc=dlc, direction=direction,
                is_extended=is_extended, is_fd=is_fd,
                decoded=decoded, data=data, frame_name=name,
            ))
        return result

    def _read_data(self, idx: int, length: int) -> bytes:
        """Read up to `length` raw data bytes for frame `idx`."""
        offset = idx * _DATA_BYTES
        if self._mmap:
            try:
                return bytes(self._mmap[offset: offset + length])
            except Exception:
                pass
        if self._data_file:
            try:
                self._data_file.seek(offset)
                return self._data_file.read(length)
            except Exception:
                pass
        return b''

    # ── Vectorised filter ─────────────────────────────────────────────────

    def build_match_mask(
        self,
        needle: str,
        channel_filter: int | None,
    ) -> np.ndarray | None:
        """
        Return a boolean numpy array of length len(self) indicating which
        frames pass the filter.  Returns None when all frames match (no
        filter active) — callers treat None as "full range, zero extra RAM".

        Uses numpy vectorised ops on in-memory arrays — no disk access,
        no Python per-frame loop for the common cases.
        """
        n = len(self.timestamps)
        if n == 0:
            return np.zeros(0, dtype=bool)

        any_filter = (needle != '') or (channel_filter is not None)
        if not any_filter:
            return None   # sentinel: all match

        mask = np.ones(n, dtype=bool)

        # ── Channel filter (vectorised uint8 comparison) ──────────────────
        if channel_filter is not None:
            chs = np.frombuffer(self.channels, dtype=np.uint8)
            target = channel_filter if channel_filter is not None else 255
            mask &= (chs == target)

        # ── Text needle filter ────────────────────────────────────────────
        if needle:
            needle_lower = needle.lower()

            # 1. ID hex match (vectorised)
            aids = np.frombuffer(self.arb_ids, dtype=np.uint32)
            # Convert IDs to hex strings in bulk — fast enough for 3M frames
            # by checking if needle is a valid hex substring
            id_mask = np.zeros(n, dtype=bool)
            try:
                # Try needle as a hex value — match on numeric range
                hex_val = int(needle_lower.replace(' ', '').lstrip('0x'), 16)
                id_mask = (aids == hex_val)
            except ValueError:
                pass
            # Also check hex string contains needle (e.g. "FECA")
            # For large stores: vectorised via format strings is slow;
            # use byte-level tricks instead
            if not id_mask.any():
                # Fallback: check for 4-char hex substrings (fast for short needles)
                if len(needle_lower) <= 8 and all(c in '0123456789abcdef' for c in needle_lower):
                    # Vectorised hex digit matching
                    aid_hex_arr = np.vectorize(lambda x: f'{x:X}'.lower())(aids)
                    id_mask = np.vectorize(lambda s: needle_lower in s)(aid_hex_arr)

            # 2. Name match (vectorised via name_table lookup)
            name_mask = np.zeros(n, dtype=bool)
            matching_nids = np.array(
                [i for i, nm in enumerate(self.name_table)
                 if needle_lower in nm.lower()],
                dtype=np.uint16
            )
            if len(matching_nids) > 0:
                nids = np.frombuffer(self.name_ids, dtype=np.uint16)
                name_mask = np.isin(nids, matching_nids)

            # 3. Direction match
            dirs_arr = np.frombuffer(self.directions, dtype=np.uint8)
            dir_mask = np.zeros(n, dtype=bool)
            if needle_lower in 'rx':
                dir_mask |= (dirs_arr == _DIR_RX)
            if needle_lower in 'tx':
                dir_mask |= (dirs_arr == _DIR_TX)

            # 4. Timestamp match (string prefix)
            ts_mask = np.zeros(n, dtype=bool)
            try:
                # Try interpreting needle as a float timestamp prefix
                ts_val = float(needle_lower)
                ts_arr = np.frombuffer(self.timestamps, dtype=np.float64)
                ts_mask = np.abs(ts_arr - ts_val) < 0.001
            except ValueError:
                pass

            # 5. Data hex — only for small stores (avoids disk scan)
            data_mask = np.zeros(n, dtype=bool)
            if n <= _LARGE_STORE_LIMIT and self._mmap and len(needle_lower) >= 2:
                # Check if needle looks like hex bytes (e.g. "FF 00")
                hex_needle = needle_lower.replace(' ', '')
                if all(c in '0123456789abcdef' for c in hex_needle) and len(hex_needle) >= 2:
                    needle_bytes = bytes.fromhex(hex_needle)
                    # Scan data file using mmap as a numpy array
                    try:
                        data_arr = np.frombuffer(self._mmap, dtype=np.uint8)
                        data_arr = data_arr[: n * _DATA_BYTES].reshape(n, _DATA_BYTES)
                        nb = len(needle_bytes)
                        for offset in range(_DATA_BYTES - nb + 1):
                            match = np.all(
                                data_arr[:, offset:offset + nb] ==
                                np.frombuffer(needle_bytes, dtype=np.uint8),
                                axis=1
                            )
                            data_mask |= match
                    except Exception:
                        pass

            # Combine: frame matches if ANY field matches the needle
            text_mask = id_mask | name_mask | dir_mask | ts_mask | data_mask

            # If nothing matched at all (e.g. random text), do Python fallback
            if not text_mask.any():
                # General Python loop fallback for unrecognised patterns
                chs = np.frombuffer(self.channels, dtype=np.uint8)
                aids_py = np.frombuffer(self.arb_ids, dtype=np.uint32)
                dirs_py = np.frombuffer(self.directions, dtype=np.uint8)
                dlcs_py = np.frombuffer(self.dlcs, dtype=np.uint8)
                nids_py = np.frombuffer(self.name_ids, dtype=np.uint16)
                for i in np.where(mask)[0]:
                    ch_str    = f'can {chs[i]}' if chs[i] != 255 else 'can ?'
                    aid_str   = f'{aids_py[i]:x}'
                    name_str  = (self.name_table[nids_py[i]]
                                 if nids_py[i] < len(self.name_table) else '').lower()
                    dir_str   = _DIR_STRINGS[min(dirs_py[i], 2)].lower()
                    dlc_str   = str(dlcs_py[i])
                    hay       = f'{ch_str} {aid_str} {name_str} {dir_str} {dlc_str}'
                    if needle_lower in hay:
                        text_mask[i] = True

            mask &= text_mask

        return mask
