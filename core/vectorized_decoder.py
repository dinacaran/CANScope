"""
Vectorized DBC signal decoder.

The standard cantools `message.decode(payload)` path is pure Python and runs
once per frame. For a 50 MB BLF (~1–2 M frames × ~10 signals) this dominates
load time. This module replaces the per-frame Python work with numpy-vectorised
bit extraction across all frames sharing the same arbitration ID.

Coverage
--------
**Fast path** (vectorised, ≥ 50× faster):
  * little-endian integer signals, signed or unsigned, 1–64 bits
  * byte-aligned little-endian float32 / float64
  * arbitrary `start_bit` (need not be byte-aligned)

**Slow path** (per-row cantools fallback, same speed as the old decoder):
  * big-endian (Motorola sawtooth) signals
  * non-byte-aligned floats
  * multiplexed signals
  * anything that fails the per-message correctness check (compared against
    `message.decode()` on the first row)

Correctness
-----------
For each cantools message, the vectorized decoder runs once on the first
frame and compares its output against `cantools.message.decode()`. If any
signal disagrees beyond a tiny tolerance, the entire message falls back to
the per-row cantools decoder for the rest of the frames in that group.
"""
from __future__ import annotations

from typing import Any

import numpy as np


# ── Single-signal extractor ──────────────────────────────────────────────


class SignalExtractor:
    """Extracts one DBC signal's numeric values from a [N, MAX_BYTES] uint8 array."""

    __slots__ = (
        'name', 'unit', 'choices', 'scale', 'offset',
        'start_bit', 'length', 'byte_order', 'is_signed', 'is_float',
        'fast_path',
    )

    def __init__(self, signal: Any) -> None:
        self.name       = signal.name
        self.unit       = signal.unit or ""
        self.choices    = dict(getattr(signal, 'choices', None) or {})
        self.scale      = float(getattr(signal, 'scale', 1.0) or 1.0)
        self.offset     = float(getattr(signal, 'offset', 0.0) or 0.0)
        self.start_bit  = int(signal.start)
        self.length     = int(signal.length)
        self.byte_order = signal.byte_order   # 'little_endian' | 'big_endian'
        self.is_signed  = bool(signal.is_signed)
        self.is_float   = bool(getattr(signal, 'is_float', False))

        is_muxed = bool(getattr(signal, 'multiplexer_ids', None))
        is_mux   = bool(getattr(signal, 'is_multiplexer', False))

        if self.byte_order != 'little_endian' or is_muxed or is_mux:
            self.fast_path = False
        elif self.is_float:
            # Only byte-aligned LE floats of 4 or 8 bytes are fast.
            self.fast_path = (
                (self.start_bit % 8 == 0)
                and (self.length in (32, 64))
            )
        elif 1 <= self.length <= 64:
            self.fast_path = True
        else:
            self.fast_path = False

    # ── Fast vectorised extraction ────────────────────────────────────────

    def extract_fast(self, data_arr: np.ndarray) -> np.ndarray:
        if self.is_float:
            return self._extract_float(data_arr)
        return self._extract_int(data_arr)

    def _extract_int(self, data_arr: np.ndarray) -> np.ndarray:
        L        = self.length
        start    = self.start_bit
        byte_off = start // 8
        bit_off  = start % 8
        nbytes   = (L + bit_off + 7) // 8
        max_b    = data_arr.shape[1]
        n        = data_arr.shape[0]

        # Accumulate into uint64 — endianness-independent byte assembly
        raw = np.zeros(n, dtype=np.uint64)
        for i in range(nbytes):
            bidx = byte_off + i
            if bidx >= max_b:
                break
            raw |= data_arr[:, bidx].astype(np.uint64) << np.uint64(i * 8)

        if bit_off:
            raw = raw >> np.uint64(bit_off)
        if L < 64:
            mask = (np.uint64(1) << np.uint64(L)) - np.uint64(1)
            raw = raw & mask

        if self.is_signed:
            if L == 64:
                result = raw.view(np.int64).astype(np.float64)
            else:
                threshold = np.uint64(1) << np.uint64(L - 1)
                offset_2L = np.int64(1) << np.int64(L)
                signed = raw.astype(np.int64)
                signed = np.where(raw >= threshold, signed - offset_2L, signed)
                result = signed.astype(np.float64)
        else:
            result = raw.astype(np.float64)

        if self.scale != 1.0 or self.offset != 0.0:
            result = result * self.scale + self.offset
        return result

    def _extract_float(self, data_arr: np.ndarray) -> np.ndarray:
        nbytes = self.length // 8
        byte_off = self.start_bit // 8
        slice_arr = np.ascontiguousarray(data_arr[:, byte_off:byte_off + nbytes])
        dtype = '<f4' if nbytes == 4 else '<f8'
        floats = slice_arr.view(dtype).reshape(-1).astype(np.float64)
        if self.scale != 1.0 or self.offset != 0.0:
            floats = floats * self.scale + self.offset
        return floats


# ── Per-message decoder (composes signal extractors) ─────────────────────


class MessageVectorDecoder:
    """
    Vectorized decoder for one cantools Message.

    Falls back to per-row `message.decode()` for messages that contain any
    non-fast-path signal, OR when the post-build correctness check fails.
    """

    __slots__ = (
        'message', 'signals', 'extractors', 'fully_fast',
        'expected_length', '_decode_kwargs', '_verified',
    )

    def __init__(self, message: Any, decode_kwargs: dict | None = None) -> None:
        self.message     = message
        self.signals     = list(getattr(message, 'signals', []))
        self.extractors  = [SignalExtractor(s) for s in self.signals]
        self.fully_fast  = bool(self.extractors) and all(e.fast_path for e in self.extractors)
        self.expected_length = int(getattr(message, 'length', 8) or 8)
        self._decode_kwargs = decode_kwargs or {
            'decode_choices': False, 'scaling': True,
            'allow_truncated': True, 'allow_excess': True,
        }
        self._verified = False   # per-message correctness check status

    # ── Public decode ─────────────────────────────────────────────────────

    def decode(self, data_arr: np.ndarray) -> dict[str, tuple[SignalExtractor, np.ndarray]]:
        """
        Decode every signal across all rows of *data_arr*.

        Parameters
        ----------
        data_arr : np.ndarray  uint8, shape ``(N, MAX_BYTES)`` (≥ expected_length)

        Returns
        -------
        dict mapping signal name → ``(SignalExtractor, numeric_arr float64[N])``
        """
        if self.fully_fast:
            if not self._verified:
                if not self._verify_fast_path(data_arr):
                    self.fully_fast = False
            if self.fully_fast:
                return self._decode_fast(data_arr)
        return self._decode_slow(data_arr)

    # ── Fast / slow implementations ──────────────────────────────────────

    def _decode_fast(self, data_arr: np.ndarray) -> dict[str, tuple]:
        out: dict[str, tuple] = {}
        view = data_arr[:, :self.expected_length]
        for ext in self.extractors:
            out[ext.name] = (ext, ext.extract_fast(view))
        return out

    def _decode_slow(self, data_arr: np.ndarray) -> dict[str, tuple]:
        n = data_arr.shape[0]
        per_sig = {ext.name: (ext, np.full(n, np.nan, dtype=np.float64))
                   for ext in self.extractors}
        kwargs = self._decode_kwargs
        msg = self.message
        ex_len = self.expected_length
        for i in range(n):
            payload = data_arr[i, :ex_len].tobytes()
            try:
                decoded = msg.decode(payload, **kwargs)
            except Exception:
                continue
            if not isinstance(decoded, dict):
                continue
            for sig_name, (_ext, arr) in per_sig.items():
                if sig_name in decoded:
                    try:
                        arr[i] = float(decoded[sig_name])
                    except (TypeError, ValueError):
                        pass
        return per_sig

    # ── First-row correctness check ──────────────────────────────────────

    def _verify_fast_path(self, data_arr: np.ndarray) -> bool:
        """Compare vectorised output against cantools on the first row."""
        self._verified = True
        if data_arr.shape[0] == 0:
            return True
        try:
            payload = data_arr[0, :self.expected_length].tobytes()
            cantools_decoded = self.message.decode(payload, **self._decode_kwargs)
        except Exception:
            return False
        if not isinstance(cantools_decoded, dict):
            return False
        view = data_arr[:1, :self.expected_length]
        for ext in self.extractors:
            try:
                vec_val = float(ext.extract_fast(view)[0])
            except Exception:
                return False
            ref = cantools_decoded.get(ext.name)
            if ref is None:
                # Signal not produced by cantools (e.g. multiplexed off-branch).
                continue
            try:
                ref_val = float(ref)
            except (TypeError, ValueError):
                return False
            if np.isnan(vec_val) and np.isnan(ref_val):
                continue
            if not np.isclose(vec_val, ref_val, rtol=1e-6, atol=1e-9):
                return False
        return True


# ── DBC-level decoder cache ──────────────────────────────────────────────


class VectorizedDBC:
    """
    Wraps an existing `DBCDecoder`, providing vectorised group decode.

    Instances are cheap: the heavy lifting (DBC parse, candidate index) is
    already done by the wrapped DBCDecoder.
    """

    __slots__ = ('dbc', '_msg_decoders')

    def __init__(self, dbc_decoder: Any) -> None:
        self.dbc = dbc_decoder
        self._msg_decoders: dict[int, MessageVectorDecoder] = {}

    def get_message_decoder(self, message: Any) -> MessageVectorDecoder:
        key = id(message)
        dec = self._msg_decoders.get(key)
        if dec is None:
            dec = MessageVectorDecoder(message, self.dbc._get_decode_kwargs())
            self._msg_decoders[key] = dec
        return dec

    def get_candidates(self, arb_id: int, is_extended: bool) -> list[Any]:
        """Mirror DBCDecoder._get_candidates without needing a RawFrame."""
        seen: set[tuple[str, int]] = set()
        candidates: list[Any] = []

        def add(msg: Any) -> None:
            key = (getattr(msg, 'name', ''), int(getattr(msg, 'frame_id', -1)))
            if key not in seen:
                seen.add(key)
                candidates.append(msg)

        for lookup_id in (arb_id, arb_id & 0x1FFFFFFF, arb_id & 0x7FF):
            for msg in self.dbc._messages_exact.get(lookup_id, []):
                add(msg)

        if is_extended or arb_id > 0x7FF:
            pgn = self.dbc._extract_j1939_pgn(arb_id)
            if pgn is not None:
                for msg in self.dbc._messages_pgn.get(pgn, []):
                    add(msg)
        return candidates
