"""
message_loss rule processor.

Flags long gaps between consecutive samples — typical symptom of an ECU
going off-bus or a heartbeat lost.

YAML form
---------
.. code-block:: yaml

    - id: rpm_signal_lost
      type: message_loss
      title: Speed feedback timeout
      severity: critical
      signal: rpm_actual
      max_gap_s: 0.5            # gap above this is a fault
      description: ...
"""
from __future__ import annotations

import numpy as np

from core.diagnostics.config_loader import RuleConfig
from core.diagnostics.context import DiagnosticContext
from core.diagnostics.models import Finding
from core.diagnostics.rules import episodes


MIN_SAMPLES = 5


def run(rule: RuleConfig, ctx: DiagnosticContext) -> list[Finding]:
    if rule.max_gap_s is None:
        return []
    series = ctx.get_canonical(rule.signal)
    if series is None or len(series.timestamps) < MIN_SAMPLES:
        return []

    ts, _vals = ctx.numpy(series)
    diffs = np.diff(ts)
    if len(diffs) == 0:
        return []

    mask = diffs > rule.max_gap_s
    if not mask.any():
        return []

    big_idx = np.where(mask)[0]          # ascending → chronological dropouts
    dropout_count = int(len(big_idx))
    median_dt = float(np.median(diffs))

    series_key = next(
        (k for k, v in ctx.store._series_by_key.items() if v is series),
        series.signal_name,
    )

    # One finding per dropout gap, in time order (capped).
    kept = big_idx[: episodes.MAX_FINDINGS_PER_RULE]
    findings: list[Finding] = []
    for gap_i, idx in enumerate(kept, start=1):
        idx = int(idx)
        gap = float(diffs[idx])
        t_start = float(ts[idx])
        t_end   = float(ts[idx + 1])

        description = rule.description or (
            f"{series.signal_name} stopped transmitting for "
            f"{gap:.2f}s — exceeds the {rule.max_gap_s:g}s "
            f"threshold defined for this signal."
        )
        description += (
            f" Dropout {gap_i}/{dropout_count}: gap from t={t_start:.2f}s "
            f"to t={t_end:.2f}s. Median sample period is {median_dt*1000:.1f}ms — "
            f"the gap is {gap/max(median_dt, 1e-9):.0f}× longer than expected."
        )

        findings.append(Finding(
            detector_name=f"yaml::{rule.id}",
            title=rule.title,
            description=description,
            severity=rule.severity,
            time_window=(t_start, t_end),
            signals=[series_key],
            metrics={
                "rule_id":         rule.id,
                "rule_type":       rule.type,
                "max_gap_s":       rule.max_gap_s,
                "gap_s":           gap,
                "median_period_s": median_dt,
                "dropout_index":   gap_i,
                "dropout_count":   dropout_count,
            },
        ))

    if dropout_count > episodes.MAX_FINDINGS_PER_RULE:
        dropped = big_idx[episodes.MAX_FINDINGS_PER_RULE:]
        span_start = float(ts[int(dropped[0])])
        span_end   = float(ts[int(dropped[-1]) + 1])
        extra = int(len(dropped))
        findings.append(Finding(
            detector_name=f"yaml::{rule.id}",
            title=f"{rule.title} (+{extra} more dropouts)",
            description=(
                f"{series.signal_name} dropped out {dropout_count} times total; "
                f"only the first {episodes.MAX_FINDINGS_PER_RULE} are listed "
                f"individually. The remaining {extra} span "
                f"t={span_start:.2f}s–{span_end:.2f}s."
            ),
            severity=rule.severity,
            time_window=(span_start, span_end),
            signals=[series_key],
            metrics={
                "rule_id":            rule.id,
                "rule_type":          rule.type,
                "max_gap_s":          rule.max_gap_s,
                "dropout_count":      dropout_count,
                "dropouts_truncated": True,
                "dropouts_omitted":   extra,
            },
        ))

    return findings
