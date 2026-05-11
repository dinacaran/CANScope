"""
Result types for the diagnostics pipeline.

All of these are pure dataclasses — no Qt, no SignalStore reference, safely
serialisable to JSON for reports and chat persistence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class Severity(IntEnum):
    """Finding severity. Higher = more urgent. Used for sorting + UI colour."""
    INFO     = 0   # informational only ("idle for long periods")
    LOW      = 1   # advisory ("temperature trending up")
    MEDIUM   = 2   # warning ("phase imbalance 8 %")
    HIGH     = 3   # error ("phase imbalance 22 %")
    CRITICAL = 4   # immediate action ("over-voltage 1.4× rated")

    def label(self) -> str:
        return self.name.title()

    def colour(self) -> str:
        """Hex colour used by the GUI panel."""
        return {
            Severity.INFO:     "#6090c0",
            Severity.LOW:      "#80a050",
            Severity.MEDIUM:   "#c0a040",
            Severity.HIGH:     "#d07040",
            Severity.CRITICAL: "#c04040",
        }[self]


@dataclass(slots=True)
class CorrectiveAction:
    """A single suggested action — checked off by the user during repair."""
    description: str
    rationale: str = ""
    priority: int = 0  # 0 = first, higher = later

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "rationale":   self.rationale,
            "priority":    self.priority,
        }


@dataclass(slots=True)
class Evidence:
    """
    Compact snippet describing what the detector saw.

    Built by :class:`EvidenceBuilder`.  Sent to the LLM as plain text — must
    stay small (target < 5 KB per finding).

    Fields
    ------
    summary : str
        One-paragraph natural-language description of the anomaly.
    signals_summary : dict[str, dict]
        Per-signal stats (min/max/mean/std/p5/p50/p95).
    sample_window : str
        CSV-formatted text snippet around the event (max ~100 rows).
    time_window : tuple[float, float]
        (start_s, end_s) of the snippet, normalised file timestamps.
    """
    summary: str
    signals_summary: dict[str, dict[str, float]] = field(default_factory=dict)
    sample_window: str = ""
    time_window: tuple[float, float] = (0.0, 0.0)

    def to_text(self) -> str:
        """Format as compact text for LLM consumption."""
        lines = [f"SUMMARY: {self.summary}"]
        if self.time_window != (0.0, 0.0):
            lines.append(
                f"TIME WINDOW: {self.time_window[0]:.3f}s – {self.time_window[1]:.3f}s"
            )
        if self.signals_summary:
            lines.append("SIGNAL STATISTICS:")
            for sig, stats in self.signals_summary.items():
                stats_str = ", ".join(f"{k}={v:g}" for k, v in stats.items())
                lines.append(f"  {sig}: {stats_str}")
        if self.sample_window:
            lines.append("SAMPLE WINDOW (downsampled):")
            lines.append(self.sample_window)
        return "\n".join(lines)


@dataclass(slots=True)
class Finding:
    """
    A single detected anomaly.

    Created by a :class:`Detector`, optionally enriched with corrective
    actions / explanation by the LLM.

    Fields
    ------
    detector_name : str
        Identifier of the detector that produced this finding (for traceability).
    title : str
        Short label shown in the UI list (e.g. "Phase U–V current imbalance").
    description : str
        Detailed sentence used in the report.
    severity : Severity
    time_window : tuple[float, float]
        (start_s, end_s) of the anomaly event in normalised file time.
    signals : list[str]
        SignalStore keys involved — used by the GUI to highlight on the plot
        and by the EvidenceBuilder to extract snippets.
    metrics : dict[str, Any]
        Detector-specific numeric metrics (e.g. {"imbalance_pct": 22.4}).
    evidence : Evidence | None
        Filled in by EvidenceBuilder before LLM stage.
    corrective_actions : list[CorrectiveAction]
        Filled in by the LLM stage.
    llm_explanation : str
        Filled in by the LLM stage — natural-language root cause hypothesis.
    """
    detector_name: str
    title: str
    description: str
    severity: Severity
    time_window: tuple[float, float] = (0.0, 0.0)
    signals: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    evidence: Evidence | None = None
    corrective_actions: list[CorrectiveAction] = field(default_factory=list)
    llm_explanation: str = ""
    # Resolved store keys to auto-plot when this finding is selected.
    # Populated by the engine from the rule's plot_signals config.
    plot_signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "detector_name":      self.detector_name,
            "title":              self.title,
            "description":        self.description,
            "severity":           int(self.severity),
            "severity_label":     self.severity.label(),
            "time_window":        list(self.time_window),
            "signals":            list(self.signals),
            "metrics":            dict(self.metrics),
            "corrective_actions": [a.to_dict() for a in self.corrective_actions],
            "llm_explanation":    self.llm_explanation,
        }


@dataclass(slots=True)
class AnalysisResult:
    """
    The complete output of one diagnostic run for one domain.

    Returned by :meth:`DiagnosticEngine.run`.  The GUI keeps the latest
    result around for the chat panel so follow-up questions can reference it.
    """
    domain_name: str
    findings: list[Finding] = field(default_factory=list)
    overall_summary: str = ""    # filled by LLM
    duration_s: float = 0.0
    signal_count: int = 0

    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity >= Severity.CRITICAL)

    def has_findings(self) -> bool:
        return len(self.findings) > 0

    def by_severity(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: -int(f.severity))

    def to_dict(self) -> dict:
        return {
            "domain":          self.domain_name,
            "findings":        [f.to_dict() for f in self.findings],
            "overall_summary": self.overall_summary,
            "duration_s":      self.duration_s,
            "signal_count":    self.signal_count,
        }
