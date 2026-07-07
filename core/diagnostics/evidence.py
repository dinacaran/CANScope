"""
EvidenceBuilder — reduces SignalStore data to small text payloads for LLM.

Critical for keeping LLM costs and latency low: the full measurement is
millions of samples (tens of MB) but each Evidence packet is < 5 KB.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

import numpy as np

from core.diagnostics.context import DiagnosticContext
from core.diagnostics.models import Evidence, Finding

if TYPE_CHECKING:
    from core.diagnostics.agent.knowledge import KnowledgeIndex

# A signal is treated as the Diagnostic Trouble Code channel if its name looks
# like a DTC id (e.g. "Active_DTC_ID", "DTC", "Full_DTC").
_DTC_SIGNAL_RE = re.compile(r"dtc", re.IGNORECASE)


def related_signal_keys(
    finding: Finding,
    ctx: DiagnosticContext,
    knowledge: "KnowledgeIndex",
    *,
    limit: int,
) -> list[str]:
    """Store keys of signals the knowledge manual associates with an active DTC.

    The DTC (e.g. ``Active_DTC_ID``) frequently flips to its fault code
    *slightly after* the fault trigger, so the code is read over the finding's
    window **expanded by ``context_window_s``** (forward padding catches the
    late transition).  Every distinct non-zero code is looked up in *knowledge*
    and its "CANscope Signals to Plot" names are resolved to store keys.

    Returns keys not already in ``finding.signals`` (rule-referenced signals keep
    priority), at most *limit*.  Returns ``[]`` when knowledge is empty, no DTC
    signal exists, or nothing resolves — so plain-engine mode is unchanged.
    """
    if knowledge is None or not getattr(knowledge, "docs", None) or limit <= 0:
        return []

    win_start, win_end = _padded_window(finding, ctx)
    codes = _active_dtc_codes(ctx, win_start, win_end)
    if not codes:
        return []

    already = set(finding.signals)
    out: list[str] = []
    for code in codes:
        for name in knowledge.candidate_signals(str(code), k=3):
            key = ctx.resolve_signal_key(name)
            if key is None or key in already or key in out:
                continue
            out.append(key)
            if len(out) >= limit:
                return out
    return out


def _padded_window(finding: Finding, ctx: DiagnosticContext) -> tuple[float, float]:
    t_start, t_end = finding.time_window
    padding = (
        ctx.domain.context_window_s
        if ctx.domain is not None
        else EvidenceBuilder.WINDOW_PADDING_S
    )
    return max(0.0, t_start - padding), t_end + padding


def _active_dtc_codes(
    ctx: DiagnosticContext, win_start: float, win_end: float
) -> list[int]:
    """Distinct non-zero DTC codes present on any DTC signal within the window."""
    codes: list[int] = []
    for series in ctx.store._series_by_key.values():
        name = getattr(series, "signal_name", "") or ""
        if not _DTC_SIGNAL_RE.search(name):
            continue
        _ts, vals = ctx.values_in_window(series, win_start, win_end)
        for v in vals:
            if np.isfinite(v) and v != 0:
                code = int(v)
                if code not in codes:
                    codes.append(code)
    return codes


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
    MAX_SIGNALS_PER_FINDING: int = 8
    WINDOW_PADDING_S: float = 2.0

    def build_for_finding(
        self,
        finding: Finding,
        ctx: DiagnosticContext,
        knowledge: "KnowledgeIndex | None" = None,
        extra_signal_keys: list[str] | None = None,
    ) -> Evidence:
        win_start, win_end = _padded_window(finding, ctx)

        # Rule-referenced signals keep priority; knowledge-derived related
        # signals (DTC manual) fill any remaining slots up to the cap.
        if extra_signal_keys is None and knowledge is not None:
            extra_signal_keys = related_signal_keys(
                finding, ctx, knowledge,
                limit=max(0, self.MAX_SIGNALS_PER_FINDING - len(finding.signals)),
            )
        combined = list(finding.signals)
        for key in (extra_signal_keys or []):
            if key not in combined:
                combined.append(key)
        signals_to_use = combined[: self.MAX_SIGNALS_PER_FINDING]

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
