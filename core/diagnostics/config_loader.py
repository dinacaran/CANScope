"""
YAML configuration loader for diagnostic domains.

Each YAML file under ``config/diagnostics/`` defines one domain with
a list of rules.  Each rule needs only a ``condition`` expression string.

Rule format
-----------
.. code-block:: yaml

    - condition: EDS_Err > 0
    - condition: MotorTemp > 130
    - condition: MotorTemp > 120 and MotorTemp < 130
    - condition: Status = 3 or Status = 5
    - condition: EDS_Err = 1 and EDS_opsts = 2

Supported operators: ``>``  ``<``  ``>=``  ``<=``  ``=``  ``!=``
Boolean: ``and`` / ``or``

Optional per-rule overrides
---------------------------
``severity``  info / low / medium / high / critical  (default: medium)
``enabled``   true / false  (default: true)
``title``     display name  (default: the condition string)
``description``  free-form text appended to the finding
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.diagnostics.models import Severity


_SEV_MAP = {
    "info":     Severity.INFO,
    "low":      Severity.LOW,
    "medium":   Severity.MEDIUM,
    "warn":     Severity.MEDIUM,
    "warning":  Severity.MEDIUM,
    "high":     Severity.HIGH,
    "error":    Severity.HIGH,
    "critical": Severity.CRITICAL,
    "fatal":    Severity.CRITICAL,
}


class ConfigError(ValueError):
    """Raised when a YAML config fails schema validation."""


@dataclass(slots=True)
class RuleConfig:
    """One rule loaded from YAML — passed to the expression rule processor."""
    id: str
    type: str            # always "expression"
    title: str
    severity: Severity
    condition: str       # e.g. "EDS_Err > 0 and MotorTemp < 130"
    description: str = ""
    suggested_action: str = ""
    enabled: bool = True
    raw: dict[str, Any] = field(default_factory=dict)
    # Signal aliases to auto-plot when this rule fires. Empty = plot fault signal only.
    plot_signals: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DomainConfig:
    """One domain (one YAML file) — name + ordered rules."""
    name: str
    description: str
    signal_map: dict[str, list[str]]   # unused; kept for DiagnosticContext compatibility
    rules: list[RuleConfig]
    source_path: Path
    context_window_s: float = 2.0      # seconds before/after each fault captured for diagnosis

    def enabled_rules(self) -> list[RuleConfig]:
        return [r for r in self.rules if r.enabled]


def default_config_dir() -> Path:
    """Return ``<project_root>/config/diagnostics``."""
    return Path(__file__).resolve().parents[2] / "config" / "diagnostics"


def load_domain_configs(config_dir: Path | None = None) -> list[DomainConfig]:
    """Discover and load every ``*.yaml`` / ``*.yml`` file in *config_dir*."""
    config_dir = config_dir or default_config_dir()
    if not config_dir.exists():
        return []
    domains: list[DomainConfig] = []
    for path in sorted(config_dir.glob("*.y*ml")):
        try:
            domains.append(load_one_config(path))
        except ConfigError:
            raise
    return sorted(domains, key=lambda d: d.name.lower())


def load_one_config(path: Path) -> DomainConfig:
    """Load and validate a single YAML domain file."""
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError("PyYAML is not installed. Run: pip install pyyaml") from exc

    try:
        text = path.read_text(encoding="utf-8")
        doc = yaml.safe_load(text)
    except Exception as exc:
        raise ConfigError(f"Failed to parse {path.name}: {exc}") from exc

    if not isinstance(doc, dict):
        raise ConfigError(f"{path.name}: top-level must be a mapping.")

    name = doc.get("domain")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{path.name}: missing or invalid 'domain' field.")

    description = str(doc.get("description", "")).strip()

    raw_cw = doc.get("context_window_s", 2.0)
    if not isinstance(raw_cw, (int, float)) or raw_cw < 0:
        raise ConfigError(f"{path.name}: 'context_window_s' must be a non-negative number.")
    context_window_s = float(raw_cw)

    raw_rules = doc.get("rules", [])
    if not isinstance(raw_rules, list):
        raise ConfigError(f"{path.name}: 'rules' must be a list.")

    rules: list[RuleConfig] = []
    seen_ids: set[str] = set()
    for i, raw_rule in enumerate(raw_rules):
        rule = _parse_rule(raw_rule, path.name, index=i)
        if rule.id in seen_ids:
            raise ConfigError(f"{path.name}: duplicate rule id {rule.id!r}.")
        seen_ids.add(rule.id)
        rules.append(rule)

    return DomainConfig(
        name=name.strip(),
        description=description,
        signal_map={},
        rules=rules,
        source_path=path,
        context_window_s=context_window_s,
    )


def _parse_rule(raw: Any, fname: str, index: int) -> RuleConfig:
    where = f"{fname}: rules[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: each rule must be a mapping.")

    cond = raw.get("condition")
    if not isinstance(cond, str) or not cond.strip():
        raise ConfigError(
            f"{where}: each rule needs a 'condition' expression.\n"
            f"  Example:  condition: EDS_Err > 0"
        )

    condition = cond.strip()
    rule_id   = str(raw.get("id",    "") or "").strip() or f"rule_{index + 1}"
    title     = str(raw.get("title", "") or "").strip() or condition

    sev_raw = str(raw.get("severity", "medium")).strip().lower()
    if sev_raw not in _SEV_MAP:
        raise ConfigError(
            f"{where}: severity must be one of {sorted(_SEV_MAP.keys())}, "
            f"got {sev_raw!r}."
        )

    ps_raw = raw.get("plot_signals") or []
    plot_signals = [str(s).strip() for s in ps_raw if str(s).strip()]

    return RuleConfig(
        id=rule_id,
        type="expression",
        title=title,
        severity=_SEV_MAP[sev_raw],
        condition=condition,
        description=str(raw.get("description",      "") or "").strip(),
        suggested_action=str(raw.get("suggested_action", "") or "").strip(),
        enabled=bool(raw.get("enabled", True)),
        raw=dict(raw),
        plot_signals=plot_signals,
    )
