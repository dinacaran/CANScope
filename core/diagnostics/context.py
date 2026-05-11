"""
DiagnosticContext — read-only view of a SignalStore for rule processors.

Rule processors do not import SignalStore directly — they receive a
DiagnosticContext.  This enforces decoupling:

* No mutation of the store.
* All signal access goes through canonical signal names defined in YAML
  domain configs, so rules are portable across DBCs / OEMs / ECUs.
* Numpy arrays are returned with deterministic dtype (float64).
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from core.signal_store import SignalStore, SignalSeries
    from core.diagnostics.config_loader import DomainConfig


class DiagnosticContext:
    """
    Read-only adapter passed to every rule processor.

    Parameters
    ----------
    store : SignalStore
        The currently loaded measurement.
    domain_config : DomainConfig | None
        Active domain configuration (provides the canonical signal map).
        ``None`` is allowed only for unit tests / generic checks.
    progress : Callable[[str], None] | None
        Optional callback for per-signal match logging.
    """

    def __init__(
        self,
        store: "SignalStore",
        domain_config: "DomainConfig | None" = None,
        progress: "Callable[[str], None] | None" = None,
    ) -> None:
        self._store = store
        self._domain = domain_config
        self._progress = progress
        # Cache: canonical-name → SignalSeries (resolved once per run)
        self._cache: dict[str, "SignalSeries | None"] = {}

    # ── Canonical lookup (preferred) ─────────────────────────────────────

    def get_canonical(self, canonical_name: str) -> "SignalSeries | None":
        """
        Look up a signal by its canonical (YAML-defined) name.

        Returns the first SignalSeries whose key matches any of the patterns
        listed under ``signal_map[canonical_name]`` in the active domain.

        Returns ``None`` if no match — the rule processor should skip.
        """
        if canonical_name in self._cache:
            return self._cache[canonical_name]
        if self._domain is None:
            self._cache[canonical_name] = None
            return None
        patterns = self._domain.signal_map.get(canonical_name) or [canonical_name]
        result = self._find_first(patterns)
        self._cache[canonical_name] = result
        if self._progress:
            if result is not None:
                n = len(result.values)
                self._progress(
                    f"  ✓ '{canonical_name}' → {result.key} ({n:,} samples)"
                )
            else:
                self._progress(
                    f"  ✗ '{canonical_name}' → NOT FOUND in measurement"
                )
        return result

    def has_canonical(self, canonical_name: str) -> bool:
        return self.get_canonical(canonical_name) is not None

    def resolve_signal_key(self, name: str) -> str | None:
        """Return the store key for *name* without emitting progress messages.

        Checks the resolution cache first (free if the signal was already used
        in a condition), then falls back to a regex search of the store.
        Returns None if not found.
        """
        if name in self._cache:
            series = self._cache[name]
        else:
            series = self._find_first([name])
        return series.key if series is not None else None

    # ── Numpy array helpers ──────────────────────────────────────────────

    def numpy(self, series: "SignalSeries") -> tuple[np.ndarray, np.ndarray]:
        """Return (timestamps, values) as float64 ndarrays."""
        return series.numpy_timestamps(), series.numpy_values()

    def values_in_window(
        self,
        series: "SignalSeries",
        t_start: float,
        t_end: float,
    ) -> tuple[np.ndarray, np.ndarray]:
        ts, vals = self.numpy(series)
        if len(ts) == 0:
            return ts, vals
        mask = (ts >= t_start) & (ts <= t_end)
        return ts[mask], vals[mask]

    # ── Recording metadata ───────────────────────────────────────────────

    @property
    def store(self) -> "SignalStore":
        return self._store

    @property
    def domain(self) -> "DomainConfig | None":
        return self._domain

    def duration_s(self) -> float:
        """Total recording duration (seconds, normalised to t=0)."""
        max_t = 0.0
        for series in self._store._series_by_key.values():
            if series.timestamps:
                last = series.timestamps[-1]
                if last > max_t:
                    max_t = last
        return max_t

    # ── Internal: pattern matching ───────────────────────────────────────

    def _find_first(self, patterns: list[str]) -> "SignalSeries | None":
        """First series whose key matches any of *patterns* (regex, case-insensitive)."""
        keys = sorted(self._store._series_by_key.keys())
        for pat in patterns:
            try:
                rx = re.compile(pat, re.IGNORECASE)
            except re.error:
                lower = pat.lower()
                for key in keys:
                    if lower in key.lower():
                        return self._store._series_by_key[key]
                continue
            for key in keys:
                if rx.search(key):
                    return self._store._series_by_key[key]
        return None
