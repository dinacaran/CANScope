"""Rule processors — dispatch table for the diagnostic engine."""
from __future__ import annotations

from collections.abc import Callable

from core.diagnostics.config_loader import RuleConfig
from core.diagnostics.context import DiagnosticContext
from core.diagnostics.models import Finding
from core.diagnostics.rules.expression import run as run_expression

RuleProcessor = Callable[[RuleConfig, DiagnosticContext], list[Finding]]

RULE_PROCESSORS: dict[str, RuleProcessor] = {
    "expression": run_expression,
}

__all__ = ["RULE_PROCESSORS", "RuleProcessor"]
