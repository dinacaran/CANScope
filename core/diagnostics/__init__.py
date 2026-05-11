"""
CANScope diagnostics — automatic fault detection + AI-powered root-cause
analysis for CAN measurements.

This package is **fully independent** of the core measurement / visualisation
pipeline.  It only consumes a read-only :class:`SignalStore` via
:class:`DiagnosticContext` and produces :class:`Finding` objects.

Fault rules are defined in YAML files under ``config/diagnostics/`` so that
end users can add / tune detection logic without writing Python.

Public surface
--------------
- :class:`DiagnosticEngine`  — orchestrator (loads YAML configs, runs rules)
- :class:`DiagnosticContext` — read-only adapter around SignalStore
- :class:`Finding` / :class:`Severity` / :class:`CorrectiveAction` — result types
- :func:`load_domain_configs` — discover & load YAML configs from disk

The package is hidden from the main UI; activation is via the
``Ctrl+Shift+A`` shortcut installed by ``gui/diagnostics/activation.py``.
"""
from __future__ import annotations

from core.diagnostics.models import (
    Finding, Severity, CorrectiveAction, Evidence, AnalysisResult,
)
from core.diagnostics.context import DiagnosticContext
from core.diagnostics.config_loader import (
    DomainConfig, RuleConfig, load_domain_configs, ConfigError,
)
from core.diagnostics.engine import DiagnosticEngine

__all__ = [
    "Finding", "Severity", "CorrectiveAction", "Evidence", "AnalysisResult",
    "DiagnosticContext",
    "DomainConfig", "RuleConfig", "load_domain_configs", "ConfigError",
    "DiagnosticEngine",
]
