"""
Prompt builders for the closed-loop agent.

Three interactions with the LLM:

1. **generate** — given the triggering finding + retrieved doc context, propose
   ONE candidate YAML rule (returned inside a fenced ```yaml block).
2. **decide**   — given the new findings, choose ``drill`` / ``pivot`` / ``stop``
   (returned as a small JSON object).
3. **report**   — write the final root-cause narrative (streamed to the chat panel).

Keeping the model's machine-readable outputs tiny and fenced makes parsing
robust without any function-calling API.
"""
from __future__ import annotations

from typing import Any


AGENT_SYSTEM = (
    "You are an automotive CAN-bus diagnostic agent. You iteratively investigate "
    "a fault by proposing detection rules that run against an already-loaded "
    "measurement. You are precise, conservative, and never invent signal names — "
    "you may only use signal names from the provided list of resolvable signals."
)

# Rule types the engine can execute, described for the model.
_RULE_TYPE_HELP = """\
Allowed rule shapes (YAML):

# 1. expression — a condition over signal values
- type: expression
  condition: <Signal> <op> <value> [and|or ...]   # ops: > < >= <= = !=
  severity: info|low|medium|high|critical
  title: <short label>

# 2. range_check — flag samples outside a band
- type: range_check
  signal: <Signal>
  min: <number>        # at least one of min/max
  max: <number>
  unit: <optional>

# 3. message_loss — flag gaps between samples
- type: message_loss
  signal: <Signal>
  max_gap_s: <seconds>

# 4. fault_signal — flag a fault flag/state
- type: fault_signal
  signal: <Signal>
  fault_when: { not_equals: 0 }   # or equals/gt/lt/ge/le/in/bit_set
"""


def _finding_brief(finding: Any) -> str:
    tw = getattr(finding, "time_window", (0.0, 0.0)) or (0.0, 0.0)
    sev = getattr(getattr(finding, "severity", None), "label", lambda: "?")()
    sigs = ", ".join(getattr(finding, "signals", []) or []) or "—"
    desc = getattr(finding, "description", "") or getattr(finding, "title", "")
    return (
        f"- {getattr(finding, 'title', '')} [{sev}] "
        f"(t={tw[0]:.2f}–{tw[1]:.2f}s; signals: {sigs})\n  {desc}"
    )


def build_generate_rule_messages(
    domain_name: str,
    trigger_finding: Any,
    doc_snippets: list,
    candidate_signals: list[str],
    resolvable_signals: list[str],
    prior_feedback: str = "",
) -> list[dict]:
    """Ask the model for ONE candidate rule to probe the fault further."""
    docs_text = "\n\n".join(
        f"### {s.name} (score {s.score})\n{s.text}" for s in doc_snippets
    ) or "(no matching diagnostic docs)"

    cand = ", ".join(candidate_signals) or "(none suggested)"
    # Keep the resolvable list bounded so the prompt stays small.
    resolvable = ", ".join(resolvable_signals[:120]) or "(none)"

    feedback_block = (
        f"\nYour previous attempt was rejected: {prior_feedback}\n"
        "Fix it and try a different, valid rule.\n" if prior_feedback else ""
    )

    user = (
        f"Domain under investigation: {domain_name}\n\n"
        f"Triggering finding:\n{_finding_brief(trigger_finding)}\n\n"
        f"Relevant diagnostic docs:\n{docs_text}\n\n"
        f"Candidate signals from the docs: {cand}\n"
        f"Signals that actually exist in this measurement (use ONLY these): {resolvable}\n"
        f"{feedback_block}\n"
        f"{_RULE_TYPE_HELP}\n"
        "Propose exactly ONE new rule that would confirm or narrow the root cause. "
        "Respond with ONLY a fenced ```yaml block containing a single rule object "
        "(a mapping, not a list). No prose."
    )
    return [
        {"role": "system", "content": AGENT_SYSTEM},
        {"role": "user", "content": user},
    ]


def build_decision_messages(
    domain_name: str,
    iteration: int,
    max_iterations: int,
    new_findings: list,
    executed_rule_summary: str,
) -> list[dict]:
    """Ask the model whether to drill / pivot / stop after running a rule."""
    if new_findings:
        findings_text = "\n".join(_finding_brief(f) for f in new_findings)
    else:
        findings_text = "(the proposed rule produced NO findings)"
    user = (
        f"Iteration {iteration}/{max_iterations} for domain {domain_name}.\n"
        f"Rule just executed: {executed_rule_summary}\n\n"
        f"Resulting findings:\n{findings_text}\n\n"
        "Decide the next action. Respond with ONLY a fenced ```json block:\n"
        '{"action": "drill|pivot|stop", "reason": "<one sentence>", '
        '"hypothesis": "<current best root-cause guess>"}\n'
        "- drill: the finding is promising; probe deeper.\n"
        "- pivot: this line is a dead end; try a different signal/hypothesis.\n"
        "- stop: you have enough evidence for a root cause."
    )
    return [
        {"role": "system", "content": AGENT_SYSTEM},
        {"role": "user", "content": user},
    ]


def build_report_messages(
    domain_name: str,
    trigger_finding: Any,
    trace: list[str],
    all_findings: list,
    hypothesis: str,
) -> list[dict]:
    """Ask the model to write the final root-cause report (streamed to chat)."""
    findings_text = "\n".join(_finding_brief(f) for f in all_findings) or "(none)"
    trace_text = "\n".join(f"{i+1}. {t}" for i, t in enumerate(trace)) or "(no steps)"
    user = (
        f"Investigation of domain '{domain_name}' is complete.\n\n"
        f"Original trigger:\n{_finding_brief(trigger_finding)}\n\n"
        f"Steps taken:\n{trace_text}\n\n"
        f"All findings gathered:\n{findings_text}\n\n"
        f"Working hypothesis: {hypothesis or '(none stated)'}\n\n"
        "Write a concise root-cause report in Markdown with sections: "
        "**Root cause**, **Evidence**, **Recommended action**. Max 400 words. "
        "Base every claim strictly on the findings above."
    )
    return [
        {"role": "system", "content": AGENT_SYSTEM},
        {"role": "user", "content": user},
    ]
