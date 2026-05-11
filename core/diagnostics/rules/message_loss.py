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

    big_idx = np.where(mask)[0]
    biggest = int(big_idx[np.argmax(diffs[big_idx])])
    biggest_gap = float(diffs[biggest])
    t_start = float(ts[biggest])
    t_end   = float(ts[biggest + 1])
    median_dt = float(np.median(diffs))

    description = rule.description or (
        f"{series.signal_name} stopped transmitting for "
        f"{biggest_gap:.2f}s — exceeds the {rule.max_gap_s:g}s "
        f"threshold defined for this signal."
    )
    description += (
        f" Gap from t={t_start:.2f}s to t={t_end:.2f}s. "
        f"Median sample period is {median_dt*1000:.1f}ms — "
        f"the gap is {biggest_gap/max(median_dt, 1e-9):.0f}× longer than expected. "
        f"Total dropouts: {len(big_idx)}."
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
        time_window=(t_start, t_end),
        signals=[series_key],
        metrics={
            "rule_id":         rule.id,
            "rule_type":       rule.type,
            "max_gap_s":       rule.max_gap_s,
            "biggest_gap_s":   biggest_gap,
            "median_period_s": median_dt,
            "dropout_count":   int(len(big_idx)),
        },
    )
    return [finding]
