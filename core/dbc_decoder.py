from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import inspect

import cantools
from cantools.database.errors import DecodeError

from core.blf_reader import RawFrame


@dataclass(slots=True)
class DecodedSignalSample:
    timestamp: float
    channel: int | None
    message_id: int
    message_name: str
    signal_name: str
    value: float | int | str      # display value (label string for enum signals)
    unit: str
    is_extended_id: bool
    direction: str
    numeric_value: float | None = None  # numeric key for plotting (always set)


class DBCLoadError(RuntimeError):
    pass


class DBCDecoder:
    def __init__(self, dbc_path: str | Path) -> None:
        self.dbc_path = Path(dbc_path)
        self.database, self.load_messages = self._load_database(self.dbc_path)
        self._decode_signature = None
        self._decode_kwargs_cache: dict[str, Any] | None = None   # perf: built once

        # Primary lookup: arbitration_id → [message, ...]
        self._messages_exact: dict[int, list[Any]] = {}
        self._messages_pgn:   dict[int, list[Any]] = {}

        # Perf: per-frame candidate cache (same ID seen repeatedly → reuse result)
        self._candidate_cache: dict[int, list[Any]] = {}

        # Perf: per-signal choices dict cached at build time (avoids getattr per sample)
        # key = (message_name, signal_name) → {int_key: label_str}
        self._choices_cache: dict[tuple[str, str], dict] = {}

        self._dbc_message_ids_preview: list[str] = []
        self.stats = {
            "candidate_exact": 0,
            "candidate_masked": 0,
            "candidate_pgn": 0,
            "decode_success": 0,
            "decode_fail": 0,
        }
        self._build_indexes()

    # ── Database loading ──────────────────────────────────────────────────

    @staticmethod
    def _load_database(path: Path):
        if not path.exists():
            raise DBCLoadError(f"DBC file not found: {path}")
        load_messages: list[str] = []
        try:
            db = cantools.database.load_file(str(path), strict=True)
            load_messages.append("DBC loaded in strict mode.")
            return db, load_messages
        except Exception as strict_exc:
            load_messages.append(
                "WARNING: Strict DBC validation failed. Retrying with compatibility mode."
            )
            load_messages.append(f"Strict mode details: {strict_exc}")
            try:
                db = cantools.database.load_file(str(path), strict=False)
                load_messages.append("DBC loaded in compatibility mode.")
                return db, load_messages
            except Exception as exc:
                raise DBCLoadError(f"Failed to load DBC file '{path}': {exc}") from exc

    # ── Index building ────────────────────────────────────────────────────

    def _build_indexes(self) -> None:
        self.load_messages.append(f"DBC messages available: {len(self.database.messages):,}")
        for message in self.database.messages:
            frame_id = int(getattr(message, "frame_id", -1))
            frame_id_text = (
                f"0x{frame_id:08X}" if frame_id > 0x7FF else f"0x{frame_id:03X}"
            )
            if len(self._dbc_message_ids_preview) < 20:
                self._dbc_message_ids_preview.append(
                    f"{message.name} | {frame_id_text} | len={getattr(message, 'length', '?')}"
                )
            # Register under all masked variants (exact, 29-bit, 11-bit)
            for fid in (frame_id, frame_id & 0x1FFFFFFF, frame_id & 0x7FF):
                if fid >= 0:
                    self._messages_exact.setdefault(fid, []).append(message)
            # J1939 PGN index
            is_extended = bool(getattr(message, "is_extended_frame", False)) or frame_id > 0x7FF
            if is_extended:
                pgn = self._extract_j1939_pgn(frame_id)
                if pgn is not None:
                    self._messages_pgn.setdefault(pgn, []).append(message)

            # Perf: pre-cache signal choices so decode_frame avoids getattr per sample
            for signal in getattr(message, "signals", []):
                choices = getattr(signal, "choices", None) or {}
                self._choices_cache[(message.name, signal.name)] = dict(choices)

    # ── Decode kwargs — built once, reused every frame ────────────────────

    def _get_decode_kwargs(self) -> dict[str, Any]:
        if self._decode_kwargs_cache is not None:
            return self._decode_kwargs_cache
        if self._decode_signature is None and self.database.messages:
            self._decode_signature = inspect.signature(
                self.database.messages[0].decode
            )
        kwargs: dict[str, Any] = {"decode_choices": False, "scaling": True}
        if self._decode_signature is not None:
            params = self._decode_signature.parameters
            if "allow_truncated" in params:
                kwargs["allow_truncated"] = True
            if "allow_excess" in params:
                kwargs["allow_excess"] = True
            if "decode_containers" in params:
                kwargs["decode_containers"] = False
        self._decode_kwargs_cache = kwargs
        return kwargs

    # ── Candidate lookup — cached per arbitration_id ─────────────────────

    @staticmethod
    def _extract_j1939_pgn(frame_id: int) -> int | None:
        can_id = frame_id & 0x1FFFFFFF
        if can_id <= 0x7FF:
            return None
        pf = (can_id >> 16) & 0xFF
        ps = (can_id >> 8) & 0xFF
        return (pf << 8) if pf < 240 else ((pf << 8) | ps)

    def _get_candidates(self, frame: RawFrame) -> list[Any]:
        """
        Return message candidates for this frame's arbitration_id.
        Result is cached after first lookup — same ID seen in every periodic frame.
        """
        arb_id = frame.arbitration_id
        cached = self._candidate_cache.get(arb_id)
        if cached is not None:
            return cached

        seen: set[tuple[str, int]] = set()
        candidates: list[Any] = []

        def add(msg: Any) -> None:
            key = (getattr(msg, "name", ""), int(getattr(msg, "frame_id", -1)))
            if key not in seen:
                seen.add(key)
                candidates.append(msg)

        # Exact + masked lookups
        for lookup_id in (arb_id, arb_id & 0x1FFFFFFF, arb_id & 0x7FF):
            for msg in self._messages_exact.get(lookup_id, []):
                add(msg)

        # J1939 PGN fallback
        if frame.is_extended_id or arb_id > 0x7FF:
            pgn = self._extract_j1939_pgn(arb_id)
            if pgn is not None:
                for msg in self._messages_pgn.get(pgn, []):
                    add(msg)

        self._candidate_cache[arb_id] = candidates
        return candidates

    # ── Frame decode ──────────────────────────────────────────────────────

    def decode_frame(self, frame: RawFrame) -> list[DecodedSignalSample]:
        kwargs = self._get_decode_kwargs()

        for message in self._get_candidates(frame):
            payload = frame.data
            expected_len = int(getattr(message, "length", len(payload)) or len(payload))
            if expected_len > 0 and len(payload) > expected_len:
                payload = payload[:expected_len]

            try:
                decoded = message.decode(payload, **kwargs)
            except TypeError:
                try:
                    decoded = message.decode(payload, decode_choices=False, scaling=True)
                except Exception:
                    self.stats["decode_fail"] += 1
                    continue
            except (DecodeError, Exception):
                self.stats["decode_fail"] += 1
                continue

            if not isinstance(decoded, dict) or not decoded:
                self.stats["decode_fail"] += 1
                continue

            msg_name = message.name
            samples: list[DecodedSignalSample] = []

            for signal in getattr(message, "signals", []):
                sig_name = signal.name
                if sig_name not in decoded:
                    continue

                raw = decoded[sig_name]  # always numeric (decode_choices=False)

                try:
                    numeric_value: float | None = float(raw)
                except (TypeError, ValueError):
                    numeric_value = None

                # Use pre-cached choices dict (avoids getattr per sample per frame)
                choices = self._choices_cache.get((msg_name, sig_name))
                if choices and numeric_value is not None:
                    label = choices.get(int(numeric_value))
                    display_value: Any = str(label) if label is not None else raw
                else:
                    display_value = raw

                samples.append(DecodedSignalSample(
                    timestamp=frame.timestamp,
                    channel=frame.channel,
                    message_id=frame.arbitration_id,
                    message_name=msg_name,
                    signal_name=sig_name,
                    value=display_value,
                    unit=signal.unit or "",
                    is_extended_id=frame.is_extended_id,
                    direction=frame.direction,
                    numeric_value=numeric_value,
                ))

            if samples:
                self.stats["decode_success"] += 1
                return samples
            self.stats["decode_fail"] += 1

        return []

    def diagnostics_text(self) -> str:
        lines = [
            f"DBC file: {self.dbc_path}",
            f"DBC messages: {len(self.database.messages):,}",
            "",
            "First DBC message IDs:",
        ]
        lines.extend(self._dbc_message_ids_preview or ["(none)"])
        lines.extend([
            "",
            "Decoder match counters:",
            f"  Exact candidates:   {self.stats['candidate_exact']:,}",
            f"  Masked candidates:  {self.stats['candidate_masked']:,}",
            f"  PGN candidates:     {self.stats['candidate_pgn']:,}",
            f"  Decode success:     {self.stats['decode_success']:,}",
            f"  Decode fail:        {self.stats['decode_fail']:,}",
            f"  ID cache entries:   {len(self._candidate_cache):,}",
        ])
        return "\n".join(lines)
