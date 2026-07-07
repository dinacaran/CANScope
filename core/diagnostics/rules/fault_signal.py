"""
fault_signal rule processor.

Checks whether a signal asserts a fault state for any sample.

YAML form
---------
.. code-block:: yaml

    - signal: motor_fault_flag
      fault_when: { not_equals: 0 }

Supported operators (one per rule)
----------------------------------
* ``equals: <value>``        — value == X
* ``not_equals: <value>``    — value != X (catches any non-zero fault flag)
* ``gt: <value>``            — value > X
* ``lt: <value>``            — value < X
* ``ge: <value>``            — value >= X
* ``le: <value>``            — value <= X
* ``in: [v1, v2, ...]``      — value matches any item (enum fault states)
* ``bit_set: <bit_index>``   — given bit is set (0 = LSB)
"""
from __future__ import annotations

import numpy as np

from core.diagnostics.config_loader import RuleConfig
from core.diagnostics.context import DiagnosticContext
from core.diagnostics.models import Finding
from core.diagnostics.rules import episodes


def run(rule: RuleConfig, ctx: DiagnosticContext) -> list[Finding]:
    series = ctx.get_canonical(rule.signal)
    if series is None or not series.values:
        return []

    ts, vals = ctx.numpy(series)
    if len(vals) == 0:
        return []

    if not rule.fault_when:
        return []
    op, target = next(iter(rule.fault_when.items()))
    mask = _apply_operator(op, vals, target)
    if mask is None or not mask.any():
        return []

    total_samples = int(len(vals))
    fault_ts   = ts[mask]
    fault_vals = vals[mask]

    series_key = next(
        (k for k, v in ctx.store._series_by_key.items() if v is series),
        series.signal_name,
    )

    merge_gap = ctx.domain.context_window_s if ctx.domain is not None else 2.0
    ranges = episodes.index_ranges(fault_ts, merge_gap)
    episode_count = len(ranges)

    kept = ranges[: episodes.MAX_FINDINGS_PER_RULE]
    findings: list[Finding] = []
    for ep_i, (a, b) in enumerate(kept, start=1):
        t_first = float(fault_ts[a])
        t_last  = float(fault_ts[b])
        ep_vals = fault_vals[a:b + 1]
        n_fault = b - a + 1
        sample_value = float(ep_vals[0])

        distinct_values = sorted({float(v) for v in ep_vals[:50]})
        distinct_str = ", ".join(f"{v:g}" for v in distinct_values[:5])
        if len(distinct_values) > 5:
            distinct_str += f", … (+{len(distinct_values) - 5} more)"

        description = rule.description or (
            f"{series.signal_name} asserted a fault condition "
            f"({op} {target!r}) in episode {ep_i}/{episode_count} "
            f"on {n_fault} sample(s)."
        )
        description += (
            f" From t={t_first:.2f}s to t={t_last:.2f}s "
            f"(duration {t_last - t_first:.2f}s). "
            f"Observed fault value(s): {distinct_str}."
        )

        findings.append(Finding(
            detector_name=f"yaml::{rule.id}",
            title=rule.title,
            description=description,
            severity=rule.severity,
            time_window=(t_first, t_last),
            signals=[series_key],
            metrics={
                "rule_id":         rule.id,
                "rule_type":       rule.type,
                "operator":        op,
                "target":          target,
                "fault_samples":   int(n_fault),
                "total_samples":   total_samples,
                "fault_fraction":  n_fault / total_samples,
                "first_value":     sample_value,
                "distinct_values": distinct_values[:10],
                "episode_index":   ep_i,
                "episode_count":   episode_count,
                "duration_s":      t_last - t_first,
            },
        ))

    if episode_count > episodes.MAX_FINDINGS_PER_RULE:
        dropped = ranges[episodes.MAX_FINDINGS_PER_RULE:]
        span_start = float(fault_ts[dropped[0][0]])
        span_end   = float(fault_ts[dropped[-1][1]])
        extra = len(dropped)
        findings.append(Finding(
            detector_name=f"yaml::{rule.id}",
            title=f"{rule.title} (+{extra} more episodes)",
            description=(
                f"{series.signal_name} asserted a fault in {episode_count} "
                f"episodes total; only the first {episodes.MAX_FINDINGS_PER_RULE} "
                f"are listed individually. The remaining {extra} span "
                f"t={span_start:.2f}s–{span_end:.2f}s."
            ),
            severity=rule.severity,
            time_window=(span_start, span_end),
            signals=[series_key],
            metrics={
                "rule_id":            rule.id,
                "rule_type":          rule.type,
                "episode_count":      episode_count,
                "episodes_truncated": True,
                "episodes_omitted":   extra,
            },
        ))

    return findings


# ── operator implementations ─────────────────────────────────────────────

def _apply_operator(op: str, vals: np.ndarray, target) -> np.ndarray | None:
    """Return a boolean mask of vals matching the operator, or None on bad input."""
    try:
        if op == "equals":
            return vals == _num(target)
        if op == "not_equals":
            return vals != _num(target)
        if op == "gt":
            return vals > _num(target)
        if op == "lt":
            return vals < _num(target)
        if op == "ge":
            return vals >= _num(target)
        if op == "le":
            return vals <= _num(target)
        if op == "in":
            if not isinstance(target, (list, tuple)) or not target:
                return None
            targets = np.asarray([_num(t) for t in target], dtype=np.float64)
            return np.isin(vals, targets)
        if op == "bit_set":
            bit = int(target)
            if bit < 0 or bit > 63:
                return None
            ints = vals.astype(np.int64, copy=False)
            return ((ints >> bit) & 1).astype(bool)
    except (TypeError, ValueError):
        return None
    return None


def _num(x) -> float:
    if isinstance(x, bool):
        return 1.0 if x else 0.0
    return float(x)
