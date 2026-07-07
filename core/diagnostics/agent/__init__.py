"""
CANScope diagnostics **agent** — Proposal B closed loop.

A base-rule finding triggers a bounded loop that:

1. retrieves diagnostic-doc context for the fault,
2. asks the LLM to generate a candidate YAML rule,
3. validates + resolves its signal names against the loaded ``SignalStore``,
4. (optionally) asks the engineer to approve the rule,
5. runs it via :meth:`DiagnosticEngine.reload_and_run`,
6. analyses the new findings and lets the LLM decide: drill / pivot / stop.

Hard budget (never exceeded): ≤5 iterations, a wall-clock timeout, and a
signature stack that halts on any repeated (rules + findings) signature.  On a
budget hit the loop emits an explicit *"Inconclusive — needs human review"*
report.

Everything here is pure Python (no Qt).  The GUI drives it from a background
thread; see ``gui/diagnostics/agent_panel.py`` and ``window.py``.  The whole
feature is disabled unless ``config/diagnostics/agent.yaml`` sets
``enabled: true``.
"""
from __future__ import annotations

from core.diagnostics.agent.config import AgentConfig, load_agent_config
from core.diagnostics.agent.knowledge import KnowledgeIndex, DocSnippet
from core.diagnostics.agent.schema import (
    validate_generated_rule, validate_generated_domain,
)
from core.diagnostics.agent.signatures import SignatureStack, signature
from core.diagnostics.agent.loop import (
    AgentLoop, AgentReport, GateDecision, StopReason,
)

__all__ = [
    "AgentConfig", "load_agent_config",
    "KnowledgeIndex", "DocSnippet",
    "validate_generated_rule", "validate_generated_domain",
    "SignatureStack", "signature",
    "AgentLoop", "AgentReport", "GateDecision", "StopReason",
]
