"""
LLM prompt templates for fault diagnosis.

Two tiers:

* :data:`SYSTEM_PROMPT_BASE` — sets role, response style.
* :func:`build_analysis_prompt` — initial analysis: rule-based findings +
  evidence packets → root-cause hypotheses + corrective actions.
* :func:`build_chat_followup_prompt` — for follow-up Q&A in the chat panel.
"""
from __future__ import annotations

from core.diagnostics.models import AnalysisResult, Finding


SYSTEM_PROMPT_BASE = """\
You are an expert automotive ECU / inverter diagnostic engineer.

You will receive:
1. A short summary of the loaded CAN measurement.
2. A list of fault findings produced by deterministic rules running on the
   raw data.
3. For each finding: a small evidence packet (statistical summary +
   downsampled values around the event).

Your job:
- For every finding, give a **plausible root cause** (1–3 hypotheses,
  ranked) and a list of **concrete corrective actions** the engineer can
  perform on the vehicle / bench.
- Be specific. Reference the observed numeric values, time stamps, and
  signal names from the evidence. Do NOT invent values that are not in the
  evidence.
- If two findings are likely caused by the same root failure, say so
  explicitly and group them.
- Use cautious language for hypotheses ("most likely…", "consistent with…")
  and decisive language for the recommended actions.

Format your reply as Markdown. Use headings (`##`) per finding and bullet
lists for the corrective actions. Keep it under 600 words unless the
problem is genuinely complex.

Never speculate about issues the rules did not detect. Stick to the
provided findings.
"""


def build_analysis_prompt(
    domain_name: str,
    manifest: str,
    findings: list[Finding],
) -> list[dict]:
    """Build the chat messages for the initial analysis call."""
    if not findings:
        user_text = (
            f"Domain: {domain_name}\n\n"
            f"{manifest}\n\n"
            "No rule-based fault findings were produced for this measurement. "
            "Please provide a one-sentence acknowledgement and remind the "
            "engineer to confirm rule coverage if they expected faults."
        )
        return [
            {"role": "system", "content": SYSTEM_PROMPT_BASE},
            {"role": "user",   "content": user_text},
        ]

    parts: list[str] = [f"Domain: {domain_name}", "", manifest, ""]
    parts.append(f"FINDINGS ({len(findings)} total):")
    for i, f in enumerate(findings, 1):
        parts.append("")
        parts.append(f"### Finding {i}: {f.title}")
        parts.append(f"- Severity: **{f.severity.label()}**")
        parts.append(f"- Detector: `{f.detector_name}`")
        parts.append(
            f"- Time window: t={f.time_window[0]:.2f}s – {f.time_window[1]:.2f}s"
        )
        if f.signals:
            parts.append(f"- Affected signals: {', '.join(f.signals)}")
        parts.append(f"- Description: {f.description}")
        # Suggested action from YAML (if any) — gives the engineer/LLM context
        ya_action = f.metrics.get("suggested_action") if isinstance(f.metrics, dict) else None
        if ya_action:
            parts.append(f"- Configured hint: {ya_action}")
        if f.evidence:
            parts.append("- Evidence:")
            parts.append("  ```")
            parts.extend("  " + line for line in f.evidence.to_text().splitlines())
            parts.append("  ```")

    parts.append("")
    parts.append(
        "Please analyse each finding. Group related findings if root causes "
        "are shared. End with a short overall summary."
    )
    user_text = "\n".join(parts)

    return [
        {"role": "system", "content": SYSTEM_PROMPT_BASE},
        {"role": "user",   "content": user_text},
    ]


def build_chat_followup_prompt(
    history: list[dict],
    user_question: str,
    result: AnalysisResult | None,
) -> list[dict]:
    """
    Build messages for a chat follow-up.

    *history* is the prior conversation (excluding the system prompt, which
    is re-added).  *result* is the latest analysis result so the model can
    reference findings without us re-sending the full evidence.
    """
    msgs: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT_BASE}]

    if result is not None and result.findings:
        # Compact recap so the model doesn't lose context after several turns
        recap_lines = [
            f"Latest analysis context (domain: {result.domain_name}):",
            f"- {len(result.findings)} findings, "
            f"{result.critical_count()} critical."
        ]
        for f in result.by_severity()[:10]:
            recap_lines.append(
                f"- [{f.severity.label()}] {f.title} "
                f"(t={f.time_window[0]:.2f}s)"
            )
        msgs.append({"role": "system", "content": "\n".join(recap_lines)})

    msgs.extend(history)
    msgs.append({"role": "user", "content": user_question})
    return msgs
