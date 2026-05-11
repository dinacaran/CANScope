"""
DiagnosticEngine — orchestrates the full diagnostic pipeline.

Flow per :meth:`run`:

1. Load YAML domain configs (cached after first call; reload on user request).
2. For the chosen domain, build a :class:`DiagnosticContext` over the
   :class:`SignalStore`.
3. For every enabled rule, dispatch to the matching processor in
   :mod:`core.diagnostics.rules`.
4. Build evidence packets via :class:`EvidenceBuilder`.
5. (Optional) Call the LLM for root-cause hypotheses and corrective actions.

The engine is pure Python (no Qt). The GUI runs it in a worker thread.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from core.diagnostics.config_loader import (
    DomainConfig, load_domain_configs, default_config_dir, ConfigError,
)
from core.diagnostics.context import DiagnosticContext
from core.diagnostics.evidence import EvidenceBuilder
from core.diagnostics.models import AnalysisResult, Finding
from core.diagnostics.rules import RULE_PROCESSORS

ProgressCb = Callable[[str], None]


class DiagnosticEngine:
    """
    Orchestrator. Stateful: caches loaded YAML configs across runs so the
    GUI's "Reload Rules" button is the only thing that re-reads disk.
    """

    def __init__(self, config_dir: Path | None = None) -> None:
        self.config_dir = config_dir or default_config_dir()
        self._domains: list[DomainConfig] = []
        self._loaded: bool = False
        self._evidence_builder = EvidenceBuilder()

    # ── config management ────────────────────────────────────────────────

    def load_configs(self) -> list[DomainConfig]:
        """(Re)load all YAML domain files from ``config_dir``."""
        self._domains = load_domain_configs(self.config_dir)
        self._loaded = True
        return self._domains

    def get_domains(self) -> list[DomainConfig]:
        if not self._loaded:
            self.load_configs()
        return self._domains

    def find_domain(self, name: str) -> DomainConfig | None:
        for d in self.get_domains():
            if d.name == name:
                return d
        return None

    # ── analysis pipeline ────────────────────────────────────────────────

    def run(
        self,
        store,
        domain_name: str,
        progress: ProgressCb | None = None,
    ) -> AnalysisResult:
        """
        Run rule-based detection only — does NOT call the LLM.

        The GUI/chat panel calls :meth:`run_with_llm` separately so that the
        rule findings appear immediately while the LLM is still thinking.
        """
        if progress:
            progress(f"Loading rules for '{domain_name}'…")
        domain = self.find_domain(domain_name)
        if domain is None:
            raise ConfigError(
                f"Domain {domain_name!r} not found in {self.config_dir}. "
                f"Available: {[d.name for d in self.get_domains()]}"
            )

        ctx = DiagnosticContext(store, domain, progress=progress)
        t0 = time.perf_counter()
        findings: list[Finding] = []

        rules = domain.enabled_rules()
        for i, rule in enumerate(rules, 1):
            if progress:
                progress(f"Rule {i}/{len(rules)}: {rule.condition}")
            processor = RULE_PROCESSORS.get(rule.type)
            if processor is None:
                continue
            try:
                rule_findings = processor(rule, ctx)
            except Exception as exc:                                # pragma: no cover
                # Never let one bad rule kill the whole run.
                rule_findings = []
                if progress:
                    progress(f"  ✗ rule {rule.id} raised an error: {exc}")
            if progress:
                if rule_findings:
                    f0 = rule_findings[0]
                    t1, t2 = f0.time_window
                    progress(
                        f"  ● fault: {len(rule_findings)} finding(s), "
                        f"t={t1:.2f}s – {t2:.2f}s"
                    )
                else:
                    progress("  ○ no fault detected")
            # Resolve plot_signals names → store keys (silent, uses ctx cache)
            if rule.plot_signals:
                resolved_plot = list(dict.fromkeys(
                    key
                    for name in rule.plot_signals
                    if (key := ctx.resolve_signal_key(name)) is not None
                ))
            else:
                resolved_plot = None   # sentinel → fall back to finding.signals

            # Inherit per-rule metadata into each finding
            for f in rule_findings:
                if rule.suggested_action and "suggested_action" not in f.metrics:
                    f.metrics["suggested_action"] = rule.suggested_action
                # plot_signals: use explicit list from YAML, else the fault signal
                f.plot_signals = resolved_plot if resolved_plot is not None else list(f.signals)
            findings.extend(rule_findings)

        # Build evidence packets (kept inside engine so the GUI can show
        # them even if the LLM step is skipped).
        if progress:
            progress("Building evidence packets…")
        for f in findings:
            f.evidence = self._evidence_builder.build_for_finding(f, ctx)

        duration = time.perf_counter() - t0
        return AnalysisResult(
            domain_name=domain.name,
            findings=findings,
            duration_s=duration,
            signal_count=len(store._series_by_key),
        )

    def build_manifest(self, store, domain_name: str) -> str:
        """
        Public helper for the LLM stage — short summary of what's loaded.
        """
        domain = self.find_domain(domain_name)
        ctx = DiagnosticContext(store, domain)
        return self._evidence_builder.build_manifest(ctx)
