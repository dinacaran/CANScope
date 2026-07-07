"""Rule processors — dispatch table for the diagnostic engine."""
from __future__ import annotations

from collections.abc import Callable

from core.diagnostics.config_loader import RuleConfig
from core.diagnostics.context import DiagnosticContext
from core.diagnostics.models import Finding
from core.diagnostics.rules.expression import run as run_expression
from core.diagnostics.rules.range_check import run as run_range_check
from core.diagnostics.rules.message_loss import run as run_message_loss
from core.diagnostics.rules.fault_signal import run as run_fault_signal

RuleProcessor = Callable[[RuleConfig, DiagnosticContext], list[Finding]]

RULE_PROCESSORS: dict[str, RuleProcessor] = {
    "expression": run_expression,
    "range_check": run_range_check,
    "message_loss": run_message_loss,
    "fault_signal": run_fault_signal,
}

__all__ = ["RULE_PROCESSORS", "RuleProcessor"]
