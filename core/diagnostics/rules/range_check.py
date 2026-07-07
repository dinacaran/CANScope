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
from core.diagnostics.rules import episodes


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

    total_samples = int(len(vals))
    n_violate = int(mask.sum())
    fraction = n_violate / total_samples

    # Auto-escalate severity if violations dominate the recording (global rate).
    severity = rule.severity
    if fraction > 0.10 and severity < Severity.CRITICAL:
        severity = Severity.CRITICAL
    elif fraction > 0.01 and severity < Severity.HIGH:
        severity = Severity.HIGH

    unit = rule.unit or series.unit or ""
    bound_text = _format_bounds(rule.min_value, rule.max_value, unit)

    violating_ts   = ts[mask]
    violating_vals = vals[mask]

    series_key = next(
        (k for k, v in ctx.store._series_by_key.items() if v is series),
        series.signal_name,
    )

    merge_gap = ctx.domain.context_window_s if ctx.domain is not None else 2.0
    ranges = episodes.index_ranges(violating_ts, merge_gap)
    episode_count = len(ranges)

    kept = ranges[: episodes.MAX_FINDINGS_PER_RULE]
    findings: list[Finding] = []
    for ep_i, (a, b) in enumerate(kept, start=1):
        t_first = float(violating_ts[a])
        t_last  = float(violating_ts[b])
        ep_vals = violating_vals[a:b + 1]
        n_ep    = b - a + 1
        v_min   = float(ep_vals.min())
        v_max   = float(ep_vals.max())

        description = rule.description or (
            f"{series.signal_name} exceeded its allowed range {bound_text}."
        )
        description += (
            f" Episode {ep_i}/{episode_count}: observed extremes "
            f"{v_min:g}…{v_max:g} {unit}, {n_ep} samples outside range, "
            f"t={t_first:.2f}s–{t_last:.2f}s (duration {t_last - t_first:.2f}s)."
        )

        findings.append(Finding(
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
                "violation_count":    int(n_ep),
                "total_samples":      total_samples,
                "violation_fraction": fraction,
                "observed_min":       v_min,
                "observed_max":       v_max,
                "episode_index":      ep_i,
                "episode_count":      episode_count,
                "duration_s":         t_last - t_first,
            },
        ))

    if episode_count > episodes.MAX_FINDINGS_PER_RULE:
        dropped = ranges[episodes.MAX_FINDINGS_PER_RULE:]
        span_start = float(violating_ts[dropped[0][0]])
        span_end   = float(violating_ts[dropped[-1][1]])
        extra = len(dropped)
        findings.append(Finding(
            detector_name=f"yaml::{rule.id}",
            title=f"{rule.title} (+{extra} more episodes)",
            description=(
                f"{series.signal_name} exceeded its allowed range in "
                f"{episode_count} episodes total; only the first "
                f"{episodes.MAX_FINDINGS_PER_RULE} are listed individually. "
                f"The remaining {extra} span t={span_start:.2f}s–{span_end:.2f}s."
            ),
            severity=severity,
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


def _format_bounds(lo: float | None, hi: float | None, unit: str) -> str:
    unit = unit or ""
    if lo is not None and hi is not None:
        return f"[{lo:g}, {hi:g}] {unit}".strip()
    if lo is not None:
        return f"≥ {lo:g} {unit}".strip()
    return f"≤ {hi:g} {unit}".strip()
