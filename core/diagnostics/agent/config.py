"""
Agent configuration — a single, simple ``config/diagnostics/agent.yaml``.

Only five keys, all with sensible defaults so a non-programmer can tune the
loop.  Hard safety bounds (max 5 iterations, 5-minute timeout) are enforced
here regardless of what the YAML asks for.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.diagnostics.config_loader import default_config_dir

# Hard ceilings from the Proposal B spec — the YAML can only go lower.
_MAX_ITER_CEILING = 5
_TIMEOUT_CEILING_S = 300.0


@dataclass(slots=True)
class AgentConfig:
    enabled: bool = False
    platform: str = "generic"
    max_iterations: int = 5
    timeout_s: float = 300.0
    autopilot: bool = False


def default_agent_config_path() -> Path:
    """Return ``<project_root>/config/diagnostics/agent.yaml``."""
    return default_config_dir() / "agent.yaml"


def load_agent_config(path: Path | None = None) -> AgentConfig:
    """Load agent settings, clamped to the hard safety bounds.

    A missing or unreadable file yields a disabled default config, so the app
    behaves exactly as today when the agent has never been configured.
    """
    path = path or default_agent_config_path()
    if not path.exists():
        return AgentConfig()

    try:
        import yaml
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(doc, dict):
            return AgentConfig()
    except Exception:
        return AgentConfig()

    return AgentConfig(
        enabled=bool(doc.get("enabled", False)),
        platform=str(doc.get("platform", "generic") or "generic").strip(),
        max_iterations=_clamp_int(doc.get("max_iterations", 5), 1, _MAX_ITER_CEILING),
        timeout_s=_clamp_float(doc.get("timeout_s", 300.0), 1.0, _TIMEOUT_CEILING_S),
        autopilot=bool(doc.get("autopilot", False)),
    )


def _clamp_int(value, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return hi


def _clamp_float(value, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return hi
