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


def run(rule: RuleConfig, ctx: DiagnosticContext) -> list[Finding]:
    series = ctx.get_canonical(rule.signal)
    if series is None or not series.values:
        return []

    ts, vals = ctx.numpy(series)
    if len(vals) == 0:
        return []

    op, target = next(iter(rule.condition.items()))
    mask = _apply_operator(op, vals, target)
    if mask is None or not mask.any():
        return []

    n_fault = int(mask.sum())
    fault_ts = ts[mask]
    t_first = float(fault_ts[0])
    t_last  = float(fault_ts[-1])
    fault_values = vals[mask]
    sample_value = float(fault_values[0])

    distinct_values = sorted({float(v) for v in fault_values[:50]})
    distinct_str = ", ".join(f"{v:g}" for v in distinct_values[:5])
    if len(distinct_values) > 5:
        distinct_str += f", … (+{len(distinct_values) - 5} more)"

    description = rule.description or (
        f"{series.signal_name} asserted a fault condition "
        f"({op} {target!r}) on {n_fault} sample(s)."
    )
    description += (
        f" First occurrence at t={t_first:.2f}s, last at t={t_last:.2f}s. "
        f"Observed fault value(s): {distinct_str}."
    )

    series_key = next(
        (k for k, v in ctx.store._series_by_key.items() if v is series),
        series.signal_name,
    )

    finding = Finding(
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
            "fault_samples":   n_fault,
            "total_samples":   int(len(vals)),
            "fault_fraction":  n_fault / len(vals),
            "first_value":     sample_value,
            "distinct_values": distinct_values[:10],
        },
    )
    return [finding]


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
