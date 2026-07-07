"""
Pure-Python schema validation for agent-generated rules.

The LLM emits a candidate rule as a plain mapping.  Before it is ever written
to disk or executed, we validate it here so a malformed rule can be rejected
(and fed back to the model) instead of raising deep inside the engine.

The checks mirror :func:`core.diagnostics.config_loader._parse_rule` but operate
on a raw ``dict`` and return a list of human-readable error strings (empty list
means the rule is valid).  No third-party schema library is used.
"""
from __future__ import annotations

from typing import Any

from core.diagnostics.config_loader import (
    _SEV_MAP, _KNOWN_TYPES, _FAULT_OPS,
)


def validate_generated_rule(rule: Any) -> list[str]:
    """Return a list of validation errors for one candidate rule dict."""
    errs: list[str] = []
    if not isinstance(rule, dict):
        return ["rule must be a mapping"]

    rule_type = str(rule.get("type", "") or "").strip().lower()
    cond = rule.get("condition")
    has_condition = isinstance(cond, str) and bool(cond.strip())
    if not rule_type:
        rule_type = "expression" if has_condition else ""
    if not rule_type:
        errs.append("rule needs a 'condition' or an explicit 'type'")
        return errs
    if rule_type not in _KNOWN_TYPES:
        errs.append(f"unknown rule type {rule_type!r}; expected one of {sorted(_KNOWN_TYPES)}")
        return errs

    sev = str(rule.get("severity", "medium")).strip().lower()
    if sev not in _SEV_MAP:
        errs.append(f"severity {sev!r} invalid; expected one of {sorted(_SEV_MAP)}")

    if rule_type == "expression":
        if not has_condition:
            errs.append("expression rule needs a non-empty 'condition' string")
    elif rule_type == "range_check":
        errs += _check_signal(rule)
        mn, mx = rule.get("min"), rule.get("max")
        if mn is None and mx is None:
            errs.append("range_check needs at least one of 'min' / 'max'")
        for label, v in (("min", mn), ("max", mx)):
            if v is not None and not _is_number(v):
                errs.append(f"'{label}' must be a number")
        if _is_number(mn) and _is_number(mx) and float(mn) > float(mx):
            errs.append("'min' must not exceed 'max'")
    elif rule_type == "message_loss":
        errs += _check_signal(rule)
        gap = rule.get("max_gap_s")
        if not _is_number(gap) or float(gap) <= 0:
            errs.append("message_loss needs a positive 'max_gap_s'")
    elif rule_type == "fault_signal":
        errs += _check_signal(rule)
        fw = rule.get("fault_when")
        if not isinstance(fw, dict) or len(fw) != 1:
            errs.append("fault_signal needs a 'fault_when' mapping with exactly one operator")
        else:
            op = next(iter(fw))
            if op not in _FAULT_OPS:
                errs.append(f"unknown fault_when operator {op!r}; expected one of {sorted(_FAULT_OPS)}")

    return errs


def validate_generated_domain(doc: Any) -> list[str]:
    """Validate a full generated domain document (``domain`` + ``rules``)."""
    errs: list[str] = []
    if not isinstance(doc, dict):
        return ["document must be a mapping"]
    name = doc.get("domain")
    if not isinstance(name, str) or not name.strip():
        errs.append("missing or invalid 'domain' field")
    rules = doc.get("rules", [])
    if not isinstance(rules, list) or not rules:
        errs.append("'rules' must be a non-empty list")
        return errs
    for i, rule in enumerate(rules):
        for e in validate_generated_rule(rule):
            errs.append(f"rules[{i}]: {e}")
    return errs


def referenced_signals(rule: dict) -> list[str]:
    """Return signal names a rule references, for pre-execution resolution.

    Typed rules carry a single ``signal``; expression rules embed signal names
    in the condition string (extracted by the caller against actual store keys).
    """
    names: list[str] = []
    sig = str(rule.get("signal", "") or "").strip()
    if sig:
        names.append(sig)
    return names


# ── helpers ───────────────────────────────────────────────────────────────

def _check_signal(rule: dict) -> list[str]:
    sig = str(rule.get("signal", "") or "").strip()
    return [] if sig else [f"{rule.get('type')} rule needs a 'signal' name"]


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)
