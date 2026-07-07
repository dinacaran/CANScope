"""
AgentLoop — the Proposal B closed loop (pure Python, no Qt).

The GUI runs :meth:`AgentLoop.run` on a background thread and wires:

* ``emit(kind, payload)``  — pushes UI events onto a queue drained by a QTimer.
* ``gate_callback(yaml)``  — blocks until the engineer picks Approve/Edit/Skip
  (auto-approves under autopilot).
* ``should_stop()``        — polled between steps for the Stop button.

Hard budget enforced here: ``max_iterations`` (≤5), a wall-clock ``timeout_s``
(≤5 min), and a signature stack that halts on any repeated (rules + findings)
signature.  Any budget hit produces an *"Inconclusive — needs human review"*
report instead of a root cause.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.diagnostics import telemetry
from core.diagnostics.context import DiagnosticContext
from core.diagnostics.agent.config import AgentConfig
from core.diagnostics.agent.knowledge import KnowledgeIndex
from core.diagnostics.agent.prompts import (
    build_generate_rule_messages, build_decision_messages, build_report_messages,
)
from core.diagnostics.agent.schema import validate_generated_rule
from core.diagnostics.agent.signatures import SignatureStack, signature

# Attempts to coax a valid rule out of the LLM within a single iteration.
_MAX_GEN_ATTEMPTS = 3


class StopReason:
    ROOT_CAUSE = "root_cause"
    MAX_ITERATIONS = "max_iterations"
    TIMEOUT = "timeout"
    OSCILLATION = "oscillation"
    CANCELLED = "cancelled"
    NO_PROGRESS = "no_progress"
    NO_TRIGGER = "no_trigger"


# Reasons that still count as a successful, conclusive investigation.
_CONCLUSIVE = {StopReason.ROOT_CAUSE}


@dataclass(slots=True)
class GateDecision:
    """Engineer's verdict on a candidate rule."""
    action: str            # "approve" | "edit" | "skip"
    yaml_text: str = ""    # the (possibly edited) YAML to execute


@dataclass(slots=True)
class AgentReport:
    outcome: str                       # "root_cause" | "inconclusive"
    stop_reason: str
    iterations: int
    hypothesis: str
    text: str
    findings: list = field(default_factory=list)
    approx_llm_tokens: int = 0
    trace: list[str] = field(default_factory=list)


def _autopilot_gate(yaml_text: str) -> GateDecision:
    return GateDecision(action="approve", yaml_text=yaml_text)


class AgentLoop:
    def __init__(
        self,
        engine,
        store,
        llm_client,
        knowledge: KnowledgeIndex,
        config: AgentConfig,
        domain_name: str,
        *,
        generated_dir: Path,
        model: str | None = None,
        emit: Callable[[str, Any], None] | None = None,
        gate_callback: Callable[[str], GateDecision] | None = None,
        should_stop: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.engine = engine
        self.store = store
        self.llm = llm_client
        self.knowledge = knowledge
        self.config = config
        self.domain_name = domain_name
        self.generated_dir = Path(generated_dir)
        self.model = model
        self._emit = emit or (lambda kind, payload=None: None)
        self._gate = gate_callback or _autopilot_gate
        self._should_stop = should_stop or (lambda: False)
        self._clock = clock
        self._tokens = 0

    # ── public entry point ───────────────────────────────────────────────
    def run(self) -> AgentReport:
        try:
            return self._run()
        except Exception as exc:  # never let the loop crash the GUI thread
            self._emit("error", str(exc))
            report = AgentReport(
                outcome="inconclusive",
                stop_reason="error",
                iterations=0,
                hypothesis="",
                text=f"Agent aborted due to an internal error: {exc}",
            )
            self._emit("done", report)
            return report

    # ── the loop ─────────────────────────────────────────────────────────
    def _run(self) -> AgentReport:
        self._wall_start = time.monotonic()
        self._clear_generated()
        ctx = DiagnosticContext(self.store)
        resolvable = self._resolvable_signal_names()
        # Episodes already investigated, so the loop advances chronologically
        # instead of re-picking the same highest-severity aggregate each time.
        self._examined: set = set()

        self._emit("step", f"Running base rules for '{self.domain_name}'…")
        result = self.engine.reload_and_run(
            self.store, self.domain_name, knowledge=self.knowledge,
        )
        if not result.findings:
            report = self._inconclusive(
                StopReason.NO_TRIGGER, iterations=0, hypothesis="", trace=[],
                findings=[],
                note="No base-rule fault fired, so there is nothing to investigate.",
            )
            return report

        trigger = self._pick_trigger(result.findings)
        trace: list[str] = [f"Base rules fired: {trigger.title}"]
        hypothesis = ""

        sig_stack = SignatureStack()
        sig_stack.push(signature(self._active_rules(), result.findings))

        start = self._clock()
        iteration = 0
        stop_reason = StopReason.MAX_ITERATIONS

        while True:
            if self._should_stop():
                stop_reason = StopReason.CANCELLED
                break
            if iteration >= self.config.max_iterations:
                stop_reason = StopReason.MAX_ITERATIONS
                break
            if self._clock() - start >= self.config.timeout_s:
                stop_reason = StopReason.TIMEOUT
                break

            iteration += 1
            self._emit("iter", (iteration, self.config.max_iterations))

            t0, t1 = trigger.time_window
            query = (
                f"{trigger.title}. {getattr(trigger, 'description', '')} "
                f"[episode window {t0:.2f}-{t1:.2f}s]"
            ).strip()
            self._emit("step", f"Iteration {iteration}: retrieving diagnostic docs…")
            snippets = self.knowledge.retrieve(query, k=3)
            cand_signals = self.knowledge.candidate_signals(query, k=3)

            candidate = self._generate_valid_rule(
                trigger, snippets, cand_signals, resolvable, ctx, iteration
            )
            if candidate is None:
                stop_reason = StopReason.NO_PROGRESS
                trace.append(f"Iter {iteration}: no valid rule could be generated.")
                break

            candidate_yaml = self._to_domain_yaml(candidate, iteration)

            # ── engineer gate (skipped under autopilot) ──
            if self.config.autopilot:
                decision = GateDecision("approve", candidate_yaml)
            else:
                self._emit("step", f"Iteration {iteration}: awaiting engineer approval…")
                decision = self._gate(candidate_yaml)

            if decision.action == "skip":
                trace.append(f"Iter {iteration}: candidate rule skipped by engineer.")
                self._emit("step", f"Iteration {iteration}: rule skipped.")
                continue

            yaml_to_run = decision.yaml_text or candidate_yaml
            self._write_generated(yaml_to_run, iteration)

            self._emit("step", f"Iteration {iteration}: running the new rule…")
            result = self.engine.reload_and_run(
                self.store, self.domain_name, knowledge=self.knowledge,
            )
            findings = result.findings

            sig = signature(self._active_rules(), findings)
            if not sig_stack.push(sig):
                stop_reason = StopReason.OSCILLATION
                trace.append(f"Iter {iteration}: repeated signature — stopping (oscillation).")
                break

            self._emit("step", f"Iteration {iteration}: analysing findings…")
            action, reason, hyp = self._decide(iteration, findings, candidate)
            if hyp:
                hypothesis = hyp
            summary = self._rule_summary(candidate)
            trace.append(f"Iter {iteration}: {summary} → {action} ({reason})")

            if action == "stop":
                stop_reason = StopReason.ROOT_CAUSE
                break

            if result.findings:
                trigger = self._pick_trigger(result.findings)

        # ── produce the report ──
        if stop_reason in _CONCLUSIVE:
            return self._root_cause_report(trigger, trace, result.findings, hypothesis, iteration)
        return self._inconclusive(
            stop_reason, iterations=iteration, hypothesis=hypothesis, trace=trace,
            findings=result.findings, note=None,
        )

    # ── rule generation with validation + signal resolution ──────────────
    def _generate_valid_rule(
        self, trigger, snippets, cand_signals, resolvable, ctx, iteration,
    ) -> dict | None:
        feedback = ""
        for attempt in range(_MAX_GEN_ATTEMPTS):
            self._emit(
                "step",
                f"Iteration {iteration}: asking the model for a candidate rule"
                + (f" (retry {attempt})" if attempt else "") + "…",
            )
            messages = build_generate_rule_messages(
                self.domain_name, trigger, snippets, cand_signals, resolvable, feedback,
            )
            text = self._llm_chat(messages)
            rule, parse_err = _extract_rule(text)
            if parse_err:
                feedback = parse_err
                continue
            errs = validate_generated_rule(rule)
            if errs:
                feedback = "; ".join(errs)
                continue
            unresolved = self._unresolved_signals(rule, ctx)
            if unresolved:
                feedback = f"these signals do not exist in the measurement: {', '.join(unresolved)}"
                continue
            return rule
        return None

    def _unresolved_signals(self, rule: dict, ctx: DiagnosticContext) -> list[str]:
        """Signal names in the rule that cannot be resolved against the store."""
        names: list[str] = []
        sig = str(rule.get("signal", "") or "").strip()
        if sig:
            names.append(sig)
        # expression rules embed signal tokens in the condition
        cond = str(rule.get("condition", "") or "")
        for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", cond):
            if tok.lower() in ("and", "or"):
                continue
            names.append(tok)
        unresolved = []
        for n in names:
            if ctx.resolve_signal_key(n) is None:
                unresolved.append(n)
        return list(dict.fromkeys(unresolved))

    # ── LLM helpers ──────────────────────────────────────────────────────
    def _llm_chat(self, messages: list[dict]) -> str:
        prompt_chars = sum(len(m.get("content", "")) for m in messages)
        text = self.llm.chat(messages, model=self.model)
        self._tokens += _chars_to_tokens(prompt_chars) + telemetry.approx_tokens(text)
        return text

    def _decide(self, iteration: int, findings: list, candidate: dict) -> tuple[str, str, str]:
        messages = build_decision_messages(
            self.domain_name, iteration, self.config.max_iterations,
            findings, self._rule_summary(candidate),
        )
        text = self._llm_chat(messages)
        obj = _extract_json(text)
        action = str(obj.get("action", "stop")).strip().lower()
        if action not in ("drill", "pivot", "stop"):
            action = "stop"
        return action, str(obj.get("reason", "")).strip(), str(obj.get("hypothesis", "")).strip()

    # ── report builders ──────────────────────────────────────────────────
    def _root_cause_report(self, trigger, trace, findings, hypothesis, iteration) -> AgentReport:
        self._emit("report_begin", None)
        messages = build_report_messages(
            self.domain_name, trigger, trace, findings, hypothesis,
        )
        prompt_chars = sum(len(m.get("content", "")) for m in messages)
        self._tokens += _chars_to_tokens(prompt_chars)
        full = ""
        try:
            for piece in self.llm.chat_stream(messages, model=self.model):
                full += piece
                self._emit("report_chunk", piece)
        except Exception as exc:
            full = f"(Report generation failed: {exc})"
            self._emit("report_chunk", full)
        self._tokens += telemetry.approx_tokens(full)
        self._emit("report_end", None)

        report = AgentReport(
            outcome="root_cause", stop_reason=StopReason.ROOT_CAUSE,
            iterations=iteration, hypothesis=hypothesis, text=full,
            findings=findings, approx_llm_tokens=self._tokens, trace=trace,
        )
        self._write_telemetry(report)
        self._emit("done", report)
        return report

    def _inconclusive(self, stop_reason, iterations, hypothesis, trace, findings, note) -> AgentReport:
        reason_text = {
            StopReason.MAX_ITERATIONS: "the iteration budget was exhausted",
            StopReason.TIMEOUT: "the time budget (wall-clock timeout) was reached",
            StopReason.OSCILLATION: "the investigation started repeating itself",
            StopReason.CANCELLED: "the investigation was stopped by the user",
            StopReason.NO_PROGRESS: "no valid detection rule could be generated",
            StopReason.NO_TRIGGER: "no base-rule fault was present to investigate",
        }.get(stop_reason, stop_reason)

        lines = [
            "## Inconclusive — needs human review",
            "",
            f"The agent could not reach a confident root cause because {reason_text}.",
        ]
        if note:
            lines += ["", note]
        if hypothesis:
            lines += ["", f"**Best current hypothesis:** {hypothesis}"]
        if trace:
            lines += ["", "**Steps taken:**", ""]
            lines += [f"{i+1}. {t}" for i, t in enumerate(trace)]
        lines += ["", "Please review the findings manually and refine the base rules."]
        text = "\n".join(lines)

        self._emit("report_begin", None)
        self._emit("report_chunk", text)
        self._emit("report_end", None)

        report = AgentReport(
            outcome="inconclusive", stop_reason=stop_reason,
            iterations=iterations, hypothesis=hypothesis, text=text,
            findings=findings, approx_llm_tokens=self._tokens, trace=trace,
        )
        self._write_telemetry(report)
        self._emit("done", report)
        return report

    def _write_telemetry(self, report: AgentReport) -> None:
        duration = time.monotonic() - getattr(self, "_wall_start", time.monotonic())
        telemetry.write_run_log({
            "mode": "agent",
            "domain": self.domain_name,
            "rules_run": len(self._active_rules()),
            "findings_count": len(report.findings),
            "duration_s": duration,
            "iterations": report.iterations,
            "approx_llm_tokens": report.approx_llm_tokens,
            "outcome": report.outcome,
            "stop_reason": report.stop_reason,
        })

    # ── generated-file helpers ───────────────────────────────────────────
    def _to_domain_yaml(self, rule: dict, iteration: int) -> str:
        import yaml
        rule = dict(rule)
        rule.setdefault("id", f"agent_iter{iteration}")
        doc = {"domain": self.domain_name, "rules": [rule]}
        return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)

    def _write_generated(self, yaml_text: str, iteration: int) -> Path:
        self.generated_dir.mkdir(parents=True, exist_ok=True)
        path = self.generated_dir / f"agent_{self._slug()}_iter{iteration}.yaml"
        path.write_text(yaml_text, encoding="utf-8")
        return path

    def _clear_generated(self) -> None:
        """Remove this domain's prior agent scratch files (agent-owned only)."""
        if not self.generated_dir.is_dir():
            return
        for p in self.generated_dir.glob(f"agent_{self._slug()}_iter*.yaml"):
            try:
                p.unlink()
            except OSError:
                pass

    def _slug(self) -> str:
        return re.sub(r"[^A-Za-z0-9]+", "_", self.domain_name).strip("_").lower() or "domain"

    # ── episode-by-episode trigger selection ─────────────────────────────
    def _pick_trigger(self, findings: list):
        """Pick the next episode to investigate, chronologically.

        Findings are ordered by episode start time (then severity), and each is
        examined once.  This walks the measurement front-to-back instead of
        repeatedly re-selecting the single highest-severity aggregate.  When
        every episode has been examined, falls back to the earliest.
        """
        ordered = sorted(
            findings,
            key=lambda f: (float(f.time_window[0]), -int(f.severity)),
        )
        for f in ordered:
            key = (
                f.detector_name,
                round(float(f.time_window[0]), 2),
                round(float(f.time_window[1]), 2),
            )
            if key not in self._examined:
                self._examined.add(key)
                return f
        return ordered[0] if ordered else findings[0]

    # ── misc ─────────────────────────────────────────────────────────────
    def _active_rules(self) -> list:
        domain = self.engine.find_domain(self.domain_name)
        return domain.enabled_rules() if domain else []

    def _resolvable_signal_names(self) -> list[str]:
        """Human-facing signal names the LLM may reference (from store keys)."""
        names: list[str] = []
        for key, series in self.store._series_by_key.items():
            sn = getattr(series, "signal_name", None)
            if sn and sn not in names:
                names.append(sn)
        return names

    @staticmethod
    def _rule_summary(rule: dict) -> str:
        rtype = rule.get("type", "expression")
        if rtype == "expression":
            return f"expression: {rule.get('condition', '')}"
        return f"{rtype} on {rule.get('signal', '?')}"


# ── parsing helpers (module-level, testable) ─────────────────────────────

_FENCE_RE = re.compile(r"```(?:yaml|yml|json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract_rule(text: str) -> tuple[dict | None, str | None]:
    """Parse a candidate rule from an LLM reply. Returns (rule, error)."""
    import yaml
    block = _first_fenced_block(text) or text
    try:
        data = yaml.safe_load(block)
    except Exception as exc:
        return None, f"could not parse YAML/JSON: {exc}"
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None, "expected a single rule mapping"
    return data, None


def _extract_json(text: str) -> dict:
    """Best-effort JSON/YAML object extraction for the decision step."""
    import json
    block = _first_fenced_block(text)
    if block:
        try:
            return json.loads(block)
        except Exception:
            pass
    # fall back to the first {...} span
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        for parser in (json.loads, _yaml_load):
            try:
                obj = parser(m.group(0))
                if isinstance(obj, dict):
                    return obj
            except Exception:
                continue
    return {}


def _first_fenced_block(text: str) -> str | None:
    m = _FENCE_RE.search(text or "")
    return m.group(1).strip() if m else None


def _yaml_load(s: str):
    import yaml
    return yaml.safe_load(s)


def _chars_to_tokens(n: int) -> int:
    """Approx tokens from a character count (~4 chars/token)."""
    return -(-n // 4) if n > 0 else 0
