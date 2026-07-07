"""
Signature stack — the loop's oscillation guard.

Each iteration produces a signature: a stable hash of the *active rule set*
plus the *findings* it produced.  If a signature ever repeats, the loop is
going in circles (e.g. the LLM keeps proposing an equivalent rule), so we stop
rather than burn the rest of the budget.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


def _rule_key(rule: Any) -> dict:
    """Canonical, order-independent view of a rule for hashing."""
    return {
        "id": getattr(rule, "id", ""),
        "type": getattr(rule, "type", ""),
        "condition": getattr(rule, "condition", "") or "",
        "signal": getattr(rule, "signal", "") or "",
        "min": getattr(rule, "min_value", None),
        "max": getattr(rule, "max_value", None),
        "max_gap_s": getattr(rule, "max_gap_s", None),
        "fault_when": getattr(rule, "fault_when", None),
        "severity": int(getattr(rule, "severity", 0)),
    }


def _finding_key(finding: Any) -> dict:
    """Canonical view of a finding — stable across tiny float jitter."""
    tw = getattr(finding, "time_window", (0.0, 0.0)) or (0.0, 0.0)
    return {
        "detector": getattr(finding, "detector_name", ""),
        "severity": int(getattr(finding, "severity", 0)),
        "t": [round(float(tw[0]), 2), round(float(tw[1]), 2)],
        "signals": sorted(getattr(finding, "signals", []) or []),
    }


def signature(rules: Iterable[Any], findings: Iterable[Any]) -> str:
    """Return a stable sha256 hex digest for (rule set, findings)."""
    payload = {
        "rules": sorted((_rule_key(r) for r in rules), key=lambda d: d["id"]),
        "findings": sorted(
            (_finding_key(f) for f in findings),
            key=lambda d: (d["detector"], d["t"][0]),
        ),
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class SignatureStack:
    """Records signatures seen this run and detects repeats."""

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self.history: list[str] = []

    def push(self, sig: str) -> bool:
        """Record *sig*.

        Returns ``True`` if it is new (the loop may continue) or ``False`` if
        it has been seen before (oscillation → the loop must stop).
        """
        self.history.append(sig)
        if sig in self._seen:
            return False
        self._seen.add(sig)
        return True

    def __contains__(self, sig: str) -> bool:
        return sig in self._seen

    def __len__(self) -> int:
        return len(self._seen)
