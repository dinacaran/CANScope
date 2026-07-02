"""
EvidenceBuilder — reduces SignalStore data to small text payloads for LLM.

Critical for keeping LLM costs and latency low: the full measurement is
millions of samples (tens of MB) but each Evidence packet is < 5 KB.
"""
from __future__ import annotations

import numpy as np

from core.diagnostics.context import DiagnosticContext
from core.diagnostics.models import Evidence, Finding


class EvidenceBuilder:
    """
    Builds compact Evidence per finding.

    Strategy
    --------
    For each finding:

    1. Determine a time window: ±2 s around the anomaly event.
    2. For every signal involved (≤ 6), extract values inside the window.
    3. Down-sample to ≤ 100 points per signal (numpy decimation).
    4. Compute summary statistics (min/max/mean/std/p5/p50/p95).
    5. Format as compact CSV-like text.

    Total payload per finding: ~3–5 KB.
    """

    MAX_POINTS_PER_SIGNAL: int = 100
    MAX_SIGNALS_PER_FINDING: int = 6
    WINDOW_PADDING_S: float = 2.0

    def build_for_finding(
        self,
        finding: Finding,
        ctx: DiagnosticContext,
    ) -> Evidence:
        t_start, t_end = finding.time_window
        padding = (
            ctx.domain.context_window_s
            if ctx.domain is not None
            else self.WINDOW_PADDING_S
        )
        win_start = max(0.0, t_start - padding)
        win_end   = t_end + padding

        signals_to_use = finding.signals[: self.MAX_SIGNALS_PER_FINDING]

        # Per-signal stats over the *whole recording* (not just the window)
        signals_summary: dict[str, dict[str, float]] = {}
        # Per-signal sampled arrays for the window
        per_signal_window: dict[str, tuple[np.ndarray, np.ndarray]] = {}

        for key in signals_to_use:
            series = ctx.store._series_by_key.get(key)
            if series is None:
                continue
            ts_full, vals_full = ctx.numpy(series)
            if len(vals_full) == 0:
                continue
            signals_summary[series.signal_name] = self._stats(vals_full)
            ts_win, vals_win = ctx.values_in_window(series, win_start, win_end)
            if len(ts_win) > 0:
                per_signal_window[series.signal_name] = (ts_win, vals_win)

        sample_window = self._format_window(per_signal_window)

        summary = (
            f"{finding.title} — {finding.description.splitlines()[0]}"
            if finding.description else finding.title
        )

        return Evidence(
            summary=summary,
            signals_summary=signals_summary,
            sample_window=sample_window,
            time_window=(win_start, win_end),
        )

    def build_manifest(self, ctx: DiagnosticContext) -> str:
        """
        One-paragraph summary of the loaded measurement, sent once at the
        start of the LLM prompt so the model knows what data exists.
        """
        store = ctx.store
        n_signals = len(store._series_by_key)
        duration = ctx.duration_s()
        channels = sorted(c for c in store.channels if c is not None)
        channel_str = ", ".join(f"CAN {c}" for c in channels) or "unknown"
        return (
            f"Measurement summary: {n_signals} decoded signals, "
            f"{duration:.1f} s duration, channel(s): {channel_str}, "
            f"{store.total_frames:,} raw frames "
            f"({store.decoded_frames:,} decoded)."
        )

    # ── internals ────────────────────────────────────────────────────────

    @staticmethod
    def _stats(vals: np.ndarray) -> dict[str, float]:
        finite = vals[np.isfinite(vals)]
        if len(finite) == 0:
            return {}
        return {
            "min":    float(finite.min()),
            "p5":     float(np.percentile(finite, 5)),
            "median": float(np.median(finite)),
            "mean":   float(finite.mean()),
            "p95":    float(np.percentile(finite, 95)),
            "max":    float(finite.max()),
            "std":    float(finite.std()),
            "n":      float(len(finite)),
        }

    def _format_window(
        self,
        per_signal: dict[str, tuple[np.ndarray, np.ndarray]],
    ) -> str:
        if not per_signal:
            return ""

        # Build a unified time grid by sampling at most MAX_POINTS_PER_SIGNAL
        # from each signal independently — keeps per-signal resolution.
        lines: list[str] = []
        for sig_name, (ts, vals) in per_signal.items():
            ts_ds, vals_ds = self._downsample(ts, vals)
            joined = " ".join(
                f"({t:.3f},{v:g})" for t, v in zip(ts_ds, vals_ds)
            )
            lines.append(f"{sig_name}: {joined}")
        return "\n".join(lines)

    def _downsample(
        self,
        ts: np.ndarray,
        vals: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        n = len(ts)
        if n <= self.MAX_POINTS_PER_SIGNAL:
            return ts, vals
        idx = np.linspace(0, n - 1, self.MAX_POINTS_PER_SIGNAL).astype(np.int64)
        return ts[idx], vals[idx]
