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
"""
from __future__ import annotations

import re

import numpy as np

from core.diagnostics.config_loader import RuleConfig
from core.diagnostics.context import DiagnosticContext
from core.diagnostics.models import Finding


# ── Parser ───────────────────────────────────────────────────────────────

_BOOL_SPLIT = re.compile(r'\b(and|or)\b', re.IGNORECASE)

_TERM_RE = re.compile(
    r'([A-Za-z_]\w*)'            # signal name
    r'\s*(>=|<=|!=|>|<|=)\s*'   # operator
    r'(-?\d+(?:\.\d*)?)',        # numeric value
)

_KEYWORDS = {"and", "or"}


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

    if len(unique_sigs) == 1:
        # ── Single signal: per-sample vectorised evaluation ──────────────
        sig = unique_sigs[0]
        ts, vals = signal_data[sig]

        mask = _apply_op(terms[0][1], vals, terms[0][2])
        for i, conn in enumerate(connectors):
            nxt = _apply_op(terms[i + 1][1], vals, terms[i + 1][2])
            mask = (mask & nxt) if conn == "and" else (mask | nxt)

        if not mask.any():
            return []

        fault_ts   = ts[mask]
        fault_vals = vals[mask]
        t_first    = float(fault_ts[0])
        t_last     = float(fault_ts[-1])
        n_fault    = int(mask.sum())
        distinct   = sorted({float(v) for v in fault_vals[:50]})[:5]

        desc = rule.description or (
            f"Condition '{rule.condition}' triggered on {n_fault} sample(s). "
            f"First at t={t_first:.2f}s, last at t={t_last:.2f}s. "
            f"Values: {', '.join(f'{v:g}' for v in distinct)}."
        )

        return [Finding(
            detector_name=f"yaml::{rule.id}",
            title=rule.title,
            description=desc,
            severity=rule.severity,
            time_window=(t_first, t_first),   # trigger point only, not full fault duration
            signals=unique_keys,
            metrics={
                "condition":      rule.condition,
                "fault_samples":  n_fault,
                "total_samples":  int(len(vals)),
                "fault_fraction": n_fault / len(vals),
            },
        )]

    else:
        # ── Multi-signal: evaluate each term independently, combine ───────
        # "AAA = 3 and BB = 6" → did AAA ever = 3 AND did BB ever = 6?
        term_results = []
        for sig, op, val in terms:
            ts, vals = signal_data[sig]
            mask = _apply_op(op, vals, val)
            if mask.any():
                fault_ts = ts[mask]
                term_results.append((True, float(fault_ts[0]), float(fault_ts[-1]), int(mask.sum())))
            else:
                term_results.append((False, 0.0, 0.0, 0))

        # Apply boolean logic left-to-right
        result = term_results[0][0]
        for i, conn in enumerate(connectors):
            nxt = term_results[i + 1][0]
            result = (result and nxt) if conn == "and" else (result or nxt)

        if not result:
            return []

        triggered = [r for r in term_results if r[0]]
        t_first   = min(r[1] for r in triggered)
        t_last    = max(r[2] for r in triggered)
        n_total   = sum(r[3] for r in triggered)

        desc = rule.description or (
            f"Condition '{rule.condition}' triggered. "
            f"First occurrence at t={t_first:.2f}s, last at t={t_last:.2f}s."
        )

        return [Finding(
            detector_name=f"yaml::{rule.id}",
            title=rule.title,
            description=desc,
            severity=rule.severity,
            time_window=(t_first, t_first),   # trigger point only, not full fault duration
            signals=unique_keys,
            metrics={
                "condition":     rule.condition,
                "fault_samples": n_total,
            },
        )]
