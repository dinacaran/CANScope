"""
range_check rule processor.

Flags samples outside the user-defined ``min`` / ``max`` range.

YAML form
---------
.. code-block:: yaml

    - id: dc_bus_overvoltage
      type: range_check
      title: DC bus over-voltage
      severity: critical
      signal: dc_bus_voltage
      max: 450                  # at least one of min/max required
      unit: V                   # optional, used in the message
      description: ...
"""
from __future__ import annotations

import numpy as np

from core.diagnostics.config_loader import RuleConfig
from core.diagnostics.context import DiagnosticContext
from core.diagnostics.models import Finding, Severity


def run(rule: RuleConfig, ctx: DiagnosticContext) -> list[Finding]:
    series = ctx.get_canonical(rule.signal)
    if series is None or not series.values:
        return []

    ts, vals = ctx.numpy(series)
    if len(vals) == 0:
        return []

    mask = np.zeros_like(vals, dtype=bool)
    if rule.min_value is not None:
        mask |= vals < rule.min_value
    if rule.max_value is not None:
        mask |= vals > rule.max_value
    if not mask.any():
        return []

    n_violate = int(mask.sum())
    violating_ts   = ts[mask]
    violating_vals = vals[mask]
    t_first = float(violating_ts[0])
    t_last  = float(violating_ts[-1])
    v_min   = float(violating_vals.min())
    v_max   = float(violating_vals.max())
    fraction = n_violate / len(vals)

    # Auto-escalate severity if violations dominate the recording
    severity = rule.severity
    if fraction > 0.10 and severity < Severity.CRITICAL:
        severity = Severity.CRITICAL
    elif fraction > 0.01 and severity < Severity.HIGH:
        severity = Severity.HIGH

    unit = rule.unit or series.unit or ""
    bound_text = _format_bounds(rule.min_value, rule.max_value, unit)
    description = rule.description or (
        f"{series.signal_name} exceeded its allowed range {bound_text}."
    )
    description += (
        f" Observed extremes: {v_min:g}…{v_max:g} {unit}. "
        f"{n_violate} samples ({fraction*100:.2f}%) outside range, "
        f"first at t={t_first:.2f}s, last at t={t_last:.2f}s."
    )

    series_key = next(
        (k for k, v in ctx.store._series_by_key.items() if v is series),
        series.signal_name,
    )

    finding = Finding(
        detector_name=f"yaml::{rule.id}",
        title=rule.title,
        description=description,
        severity=severity,
        time_window=(t_first, t_last),
        signals=[series_key],
        metrics={
            "rule_id":            rule.id,
            "rule_type":          rule.type,
            "min_allowed":        rule.min_value if rule.min_value is not None else float("nan"),
            "max_allowed":        rule.max_value if rule.max_value is not None else float("nan"),
            "violation_count":    n_violate,
            "total_samples":      int(len(vals)),
            "violation_fraction": fraction,
            "observed_min":       v_min,
            "observed_max":       v_max,
        },
    )
    return [finding]


def _format_bounds(lo: float | None, hi: float | None, unit: str) -> str:
    unit = unit or ""
    if lo is not None and hi is not None:
        return f"[{lo:g}, {hi:g}] {unit}".strip()
    if lo is not None:
        return f"≥ {lo:g} {unit}".strip()
    return f"≤ {hi:g} {unit}".strip()
