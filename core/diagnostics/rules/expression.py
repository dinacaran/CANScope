"""
Expression rule processor.

Evaluates a free-form condition string against the loaded measurement.

Supported syntax
----------------
    SIGNAL OP VALUE  [and|or  SIGNAL OP VALUE ...]

Operators: > < >= <= = !=
Boolean:   and  or  (left-to-right, no parentheses needed for simple rules)

Examples
--------
    EDS_Err > 0
    MotorTemp > 130
    MotorTemp > 120 and MotorTemp < 130
    Status = 3 or Status = 5
    EDS_Err = 1 and EDS_opsts = 2

Signal names are matched case-insensitively against the loaded measurement.

Evaluation model
----------------
All signals in the expression are placed on a **common timebase** (the union of
their sample timestamps, or the densest signal's grid when the union is huge)
and forward-filled with their last known value (zero-order hold — CAN signals
hold their value between frames).  The full boolean expression is then evaluated
per grid sample, so ``and`` / ``or`` mean *simultaneous* truth.  Contiguous
fault samples are grouped into **episodes** (see :mod:`.episodes`); one Finding
is emitted per episode with its real ``(start, end)`` extent.
"""
from __future__ import annotations

import re

import numpy as np

from core.diagnostics.config_loader import RuleConfig
from core.diagnostics.context import DiagnosticContext
from core.diagnostics.models import Finding
from core.diagnostics.rules import episodes


# ── Parser ───────────────────────────────────────────────────────────────

_BOOL_SPLIT = re.compile(r'\b(and|or)\b', re.IGNORECASE)

_TERM_RE = re.compile(
    r'([A-Za-z_]\w*)'            # signal name
    r'\s*(>=|<=|!=|>|<|=)\s*'   # operator
    r'(-?\d+(?:\.\d*)?)',        # numeric value
)

_KEYWORDS = {"and", "or"}

# Above this many grid points, fall back to the densest signal's own grid
# instead of the union, to bound memory on multi-million-sample measurements.
_MAX_GRID_POINTS = 5_000_000


def _parse(expr: str):
    """
    Parse 'AAA > 2 and BBB = 3' into:
        terms      = [(signal, op, value), ...]
        connectors = ['and', ...]  (len = len(terms) - 1)
    """
    parts = _BOOL_SPLIT.split(expr)
    terms = []
    connectors = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            m = _TERM_RE.search(part.strip())
            if not m:
                raise ValueError(f"Cannot parse: {part.strip()!r}")
            sig = m.group(1)
            if sig.lower() in _KEYWORDS:
                raise ValueError(f"Keyword used as signal name: {sig!r}")
            terms.append((sig, m.group(2), float(m.group(3))))
        else:
            connectors.append(part.strip().lower())
    if not terms:
        raise ValueError("Empty condition")
    return terms, connectors


def _apply_op(op: str, vals: np.ndarray, target: float) -> np.ndarray:
    if op == ">":  return vals > target
    if op == "<":  return vals < target
    if op == ">=": return vals >= target
    if op == "<=": return vals <= target
    if op == "=":  return vals == target
    if op == "!=": return vals != target
    return np.zeros(len(vals), dtype=bool)


# ── Main entry point ─────────────────────────────────────────────────────

def run(rule: RuleConfig, ctx: DiagnosticContext) -> list[Finding]:
    try:
        terms, connectors = _parse(rule.condition)
    except ValueError:
        return []

    # Fetch data for every unique signal in the expression
    signal_data: dict[str, tuple] = {}
    sig_store_keys: dict[str, str] = {}   # canonical name → _series_by_key key
    for sig, _, _ in terms:
        if sig in signal_data:
            continue
        series = ctx.get_canonical(sig)
        if series is None or not series.values:
            return []   # signal absent in measurement — skip silently
        ts, vals = ctx.numpy(series)
        if len(vals) == 0:
            return []
        signal_data[sig] = (ts, vals)
        sig_store_keys[sig] = series.key   # full "CH1::Msg::Sig" key for evidence

    unique_sigs = list(dict.fromkeys(sig for sig, _, _ in terms))
    # Use store keys so the evidence builder can look signals up in _series_by_key
    unique_keys = [sig_store_keys.get(s, s) for s in unique_sigs]

    # ── Common timebase (union of all involved signals, or densest grid) ──
    grid = _build_grid(unique_sigs, signal_data)

    # Zero-order-hold each unique signal onto the grid once, then evaluate terms.
    filled: dict[str, np.ndarray] = {
        s: _zoh(signal_data[s][0], signal_data[s][1], grid) for s in unique_sigs
    }
    # ``valid`` marks grid points at or after each signal's first sample.
    valid: dict[str, np.ndarray] = {
        s: grid >= signal_data[s][0][0] for s in unique_sigs
    }

    def _term_mask(idx: int) -> np.ndarray:
        sig, op, val = terms[idx]
        # NaN comparisons already yield False; the & valid guard keeps a term
        # false before its signal's first sample.
        return _apply_op(op, filled[sig], val) & valid[sig]

    mask = _term_mask(0)
    for i, conn in enumerate(connectors):
        nxt = _term_mask(i + 1)
        mask = (mask & nxt) if conn == "and" else (mask | nxt)

    if not mask.any():
        return []

    fault_idx = np.where(mask)[0]
    fault_ts = grid[fault_idx]

    # Primary signal drives representative values in the description / metrics.
    primary_sig = unique_sigs[0]
    primary_vals = filled[primary_sig]

    merge_gap = ctx.domain.context_window_s if ctx.domain is not None else 2.0
    ranges = episodes.index_ranges(fault_ts, merge_gap)
    episode_count = len(ranges)
    total_samples = int(grid.size)

    kept = ranges[: episodes.MAX_FINDINGS_PER_RULE]
    findings: list[Finding] = []
    for ep_i, (a, b) in enumerate(kept, start=1):
        g0, g1 = int(fault_idx[a]), int(fault_idx[b])
        t_start = float(grid[g0])
        t_end   = float(grid[g1])
        n_fault = b - a + 1
        ep_vals = primary_vals[fault_idx[a:b + 1]]
        distinct = sorted({float(v) for v in ep_vals[:50] if np.isfinite(v)})[:5]
        distinct_str = ", ".join(f"{v:g}" for v in distinct) or "n/a"

        desc = rule.description or (
            f"Condition '{rule.condition}' triggered in episode "
            f"{ep_i}/{episode_count} on {n_fault} sample(s), "
            f"t={t_start:.2f}s–{t_end:.2f}s "
            f"(duration {t_end - t_start:.2f}s). "
            f"{primary_sig} values: {distinct_str}."
        )

        findings.append(Finding(
            detector_name=f"yaml::{rule.id}",
            title=rule.title,
            description=desc,
            severity=rule.severity,
            time_window=(t_start, t_end),
            signals=unique_keys,
            metrics={
                "condition":            rule.condition,
                "fault_samples":        int(n_fault),
                "total_samples":        total_samples,
                "fault_fraction":       n_fault / total_samples,
                "episode_index":        ep_i,
                "episode_count":        episode_count,
                "duration_s":           t_end - t_start,
                "representative_values": distinct,
            },
        ))

    if episode_count > episodes.MAX_FINDINGS_PER_RULE:
        dropped = ranges[episodes.MAX_FINDINGS_PER_RULE:]
        span_start = float(grid[int(fault_idx[dropped[0][0]])])
        span_end   = float(grid[int(fault_idx[dropped[-1][1]])])
        extra = len(dropped)
        findings.append(Finding(
            detector_name=f"yaml::{rule.id}",
            title=f"{rule.title} (+{extra} more episodes)",
            description=(
                f"Condition '{rule.condition}' triggered in "
                f"{episode_count} episodes total; only the first "
                f"{episodes.MAX_FINDINGS_PER_RULE} are listed individually. "
                f"The remaining {extra} span t={span_start:.2f}s–{span_end:.2f}s."
            ),
            severity=rule.severity,
            time_window=(span_start, span_end),
            signals=unique_keys,
            metrics={
                "condition":          rule.condition,
                "episode_count":      episode_count,
                "episodes_truncated": True,
                "episodes_omitted":   extra,
            },
        ))

    return findings


# ── helpers ──────────────────────────────────────────────────────────────

def _build_grid(unique_sigs: list[str], signal_data: dict[str, tuple]) -> np.ndarray:
    """Common evaluation timebase for the expression's signals."""
    if len(unique_sigs) == 1:
        return signal_data[unique_sigs[0]][0]
    grids = [signal_data[s][0] for s in unique_sigs]
    union = np.unique(np.concatenate(grids))
    if union.size > _MAX_GRID_POINTS:
        # Memory guard: evaluate on the densest signal's own grid instead.
        return max(grids, key=len)
    return union


def _zoh(ts: np.ndarray, vals: np.ndarray, grid: np.ndarray) -> np.ndarray:
    """Zero-order-hold *vals* sampled at *ts* onto *grid*.

    Each grid point takes the last value at or before it; grid points before the
    first sample get NaN (caller guards them with a ``valid`` mask).
    """
    idx = np.searchsorted(ts, grid, side="right") - 1
    out = np.where(idx >= 0, vals[np.clip(idx, 0, len(vals) - 1)], np.nan)
    return out
