"""
Per-run diagnostic telemetry.

Each rule run (manual or agent-driven) appends a small JSON record under
``logs/diagnostics/`` so we can later reason about cost/speed of the feature.
Writing telemetry must never break a diagnostic run, so every failure here is
swallowed.

Record fields
-------------
``timestamp``          ISO-8601 UTC
``mode``               "manual" | "agent"
``domain``             domain name that was run
``rules_run``          number of enabled rules executed
``findings_count``     number of findings produced
``duration_s``         wall-clock seconds
``iterations``         1 for a manual run; the agent loop's iteration count
``approx_llm_tokens``  rough token estimate (chars/4); 0 when no LLM was used
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path


def default_log_dir() -> Path:
    """Return ``<project_root>/logs/diagnostics``."""
    return Path(__file__).resolve().parents[2] / "logs" / "diagnostics"


def approx_tokens(text: str) -> int:
    """Rough token estimate: ~4 characters per token (ceil division)."""
    if not text:
        return 0
    return -(-len(text) // 4)


def write_run_log(record: dict, log_dir: Path | None = None) -> Path | None:
    """Write a single telemetry record as a JSON file. Never raises.

    Returns the written path, or ``None`` if anything went wrong or telemetry
    is disabled via ``CANSCOPE_DIAG_TELEMETRY=0`` (used by the test suite).
    """
    if os.environ.get("CANSCOPE_DIAG_TELEMETRY", "1") == "0":
        return None
    try:
        now = datetime.now(timezone.utc)
        payload = {
            "timestamp": now.isoformat(),
            "mode": record.get("mode", "manual"),
            "domain": record.get("domain", ""),
            "rules_run": int(record.get("rules_run", 0)),
            "findings_count": int(record.get("findings_count", 0)),
            "duration_s": round(float(record.get("duration_s", 0.0)), 4),
            "iterations": int(record.get("iterations", 1)),
            "approx_llm_tokens": int(record.get("approx_llm_tokens", 0)),
        }
        # Preserve any extra keys the caller supplied (e.g. agent outcome).
        for k, v in record.items():
            payload.setdefault(k, v)

        target_dir = log_dir or default_log_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = now.strftime("%Y%m%dT%H%M%S")
        path = target_dir / f"diag_{stamp}_{uuid.uuid4().hex[:8]}.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path
    except Exception:
        return None
