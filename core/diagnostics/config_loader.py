"""
YAML configuration loader for diagnostic domains.

Each YAML file under ``config/diagnostics/`` defines one domain with a list of
rules.  The simplest rule needs only a ``condition`` expression string.

Rule format (expression — the default)
--------------------------------------
.. code-block:: yaml

    - condition: EDS_Err > 0
    - condition: MotorTemp > 130
    - condition: MotorTemp > 120 and MotorTemp < 130
    - condition: Status = 3 or Status = 5
    - condition: EDS_Err = 1 and EDS_opsts = 2

Supported operators: ``>``  ``<``  ``>=``  ``<=``  ``=``  ``!=``
Boolean: ``and`` / ``or``

Typed rules
-----------
A rule may instead declare an explicit ``type`` and use the matching processor:

.. code-block:: yaml

    - type: range_check
      signal: dc_bus_voltage
      max: 450                 # at least one of min/max
      unit: V

    - type: message_loss
      signal: rpm_actual
      max_gap_s: 0.5

    - type: fault_signal
      signal: motor_fault_flag
      fault_when: { not_equals: 0 }

Optional per-rule overrides (all rule types)
--------------------------------------------
``severity``  info / low / medium / high / critical  (default: medium)
``enabled``   true / false  (default: true)
``title``     display name  (default: the condition string / "<type> on <signal>")
``description``  free-form text appended to the finding
``suggested_action``  hint fed to the LLM
``plot_signals``  signals to auto-plot when the rule fires

Directory layout / origin
-------------------------
Rules are loaded from two sub-folders and tagged with an ``origin``:

* ``config/diagnostics/base_rules/``  → ``origin="base"`` (engineer-authored)
* ``config/diagnostics/generated/``   → ``origin="generated"`` (AI-agent workspace)

If neither sub-folder exists, ``*.y*ml`` files directly under the config dir are
loaded as ``origin="base"`` (backward-compat).  Files that share the same
``domain:`` name are merged into a single :class:`DomainConfig`, so the agent can
append candidate rules to an existing base domain.
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

# Rule types with a registered processor (see core.diagnostics.rules).
_KNOWN_TYPES = {"expression", "range_check", "message_loss", "fault_signal"}

# Operators accepted inside a ``fault_when`` mapping.
_FAULT_OPS = {"equals", "not_equals", "gt", "lt", "ge", "le", "in", "bit_set"}


class ConfigError(ValueError):
    """Raised when a YAML config fails schema validation."""


@dataclass(slots=True)
class RuleConfig:
    """One rule loaded from YAML — passed to a rule processor.

    ``condition`` is used by the ``expression`` processor.  The remaining
    optional fields carry the parameters for the typed processors
    (``range_check`` / ``message_loss`` / ``fault_signal``).
    """
    id: str
    type: str            # "expression" | "range_check" | "message_loss" | "fault_signal"
    title: str
    severity: Severity
    condition: str = ""              # expression rules
    description: str = ""
    suggested_action: str = ""
    enabled: bool = True
    raw: dict[str, Any] = field(default_factory=dict)
    # Signal aliases to auto-plot when this rule fires. Empty = plot fault signal only.
    plot_signals: list[str] = field(default_factory=list)
    # Where this rule came from: "base" (engineer) or "generated" (AI agent).
    origin: str = "base"
    # ── typed-rule parameters ────────────────────────────────────────────
    signal: str = ""                       # range_check / message_loss / fault_signal
    min_value: float | None = None         # range_check
    max_value: float | None = None         # range_check
    unit: str = ""                         # range_check
    max_gap_s: float | None = None         # message_loss
    fault_when: dict[str, Any] | None = None   # fault_signal


@dataclass(slots=True)
class DomainConfig:
    """One domain — name + ordered rules (possibly merged across files)."""
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
    """Discover and load rule files, tagging origin and merging by domain name.

    Scans ``<config_dir>/base_rules`` (origin ``base``) then
    ``<config_dir>/generated`` (origin ``generated``).  If neither sub-folder
    exists, falls back to ``*.y*ml`` directly under *config_dir* (origin
    ``base``) for backward compatibility.
    """
    config_dir = config_dir or default_config_dir()
    if not config_dir.exists():
        return []

    base_dir = config_dir / "base_rules"
    gen_dir = config_dir / "generated"

    sources: list[tuple[Path, str]] = []
    if base_dir.is_dir() or gen_dir.is_dir():
        if base_dir.is_dir():
            sources += [(p, "base") for p in sorted(base_dir.glob("*.y*ml"))]
        if gen_dir.is_dir():
            sources += [(p, "generated") for p in sorted(gen_dir.glob("*.y*ml"))]
    else:
        sources += [(p, "base") for p in sorted(config_dir.glob("*.y*ml"))]

    loaded = [load_one_config(path, origin=origin) for path, origin in sources]
    merged = _merge_by_name(loaded)
    return sorted(merged, key=lambda d: d.name.lower())


def _merge_by_name(domains: list[DomainConfig]) -> list[DomainConfig]:
    """Combine domains that share a name; concatenate rules, keep ids unique."""
    by_name: dict[str, DomainConfig] = {}
    order: list[str] = []
    for d in domains:
        existing = by_name.get(d.name)
        if existing is None:
            by_name[d.name] = d
            order.append(d.name)
            continue
        seen_ids = {r.id for r in existing.rules}
        for rule in d.rules:
            if rule.id in seen_ids:
                # Preserve traceability but avoid a hard collision across files.
                suffix = 2
                new_id = f"{rule.id}__{rule.origin}{suffix}"
                while new_id in seen_ids:
                    suffix += 1
                    new_id = f"{rule.id}__{rule.origin}{suffix}"
                rule.id = new_id
            seen_ids.add(rule.id)
            existing.rules.append(rule)
        # Prefer a non-empty description from whichever file supplies one.
        if not existing.description and d.description:
            existing.description = d.description
    return [by_name[n] for n in order]


def load_one_config(path: Path, origin: str = "base") -> DomainConfig:
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
        rule = _parse_rule(raw_rule, path.name, index=i, origin=origin)
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


def _parse_rule(raw: Any, fname: str, index: int, origin: str = "base") -> RuleConfig:
    where = f"{fname}: rules[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: each rule must be a mapping.")

    # ── shared fields ────────────────────────────────────────────────────
    rule_type = str(raw.get("type", "") or "").strip().lower()
    cond = raw.get("condition")
    has_condition = isinstance(cond, str) and bool(cond.strip())

    if not rule_type:
        rule_type = "expression" if has_condition else ""
    if not rule_type:
        raise ConfigError(
            f"{where}: rule needs a 'condition' expression or an explicit 'type'.\n"
            f"  Example:  condition: EDS_Err > 0"
        )
    if rule_type not in _KNOWN_TYPES:
        raise ConfigError(
            f"{where}: unknown rule type {rule_type!r}. "
            f"Expected one of {sorted(_KNOWN_TYPES)}."
        )

    rule_id = str(raw.get("id", "") or "").strip() or f"rule_{index + 1}"

    sev_raw = str(raw.get("severity", "medium")).strip().lower()
    if sev_raw not in _SEV_MAP:
        raise ConfigError(
            f"{where}: severity must be one of {sorted(_SEV_MAP.keys())}, "
            f"got {sev_raw!r}."
        )

    ps_raw = raw.get("plot_signals") or []
    plot_signals = [str(s).strip() for s in ps_raw if str(s).strip()]

    common = dict(
        id=rule_id,
        type=rule_type,
        severity=_SEV_MAP[sev_raw],
        description=str(raw.get("description", "") or "").strip(),
        suggested_action=str(raw.get("suggested_action", "") or "").strip(),
        enabled=bool(raw.get("enabled", True)),
        raw=dict(raw),
        plot_signals=plot_signals,
        origin=origin,
    )

    if rule_type == "expression":
        return _parse_expression(raw, where, common)
    if rule_type == "range_check":
        return _parse_range_check(raw, where, common)
    if rule_type == "message_loss":
        return _parse_message_loss(raw, where, common)
    if rule_type == "fault_signal":
        return _parse_fault_signal(raw, where, common)
    raise ConfigError(f"{where}: unhandled rule type {rule_type!r}.")  # pragma: no cover


# ── per-type parsers ─────────────────────────────────────────────────────

def _parse_expression(raw: dict, where: str, common: dict) -> RuleConfig:
    cond = raw.get("condition")
    if not isinstance(cond, str) or not cond.strip():
        raise ConfigError(
            f"{where}: expression rules need a 'condition'.\n"
            f"  Example:  condition: EDS_Err > 0"
        )
    condition = cond.strip()
    title = str(raw.get("title", "") or "").strip() or condition
    return RuleConfig(title=title, condition=condition, **common)


def _require_signal(raw: dict, where: str) -> str:
    signal = str(raw.get("signal", "") or "").strip()
    if not signal:
        raise ConfigError(f"{where}: {raw.get('type')!r} rules need a 'signal' name.")
    return signal


def _parse_range_check(raw: dict, where: str, common: dict) -> RuleConfig:
    signal = _require_signal(raw, where)
    min_v = raw.get("min")
    max_v = raw.get("max")
    if min_v is None and max_v is None:
        raise ConfigError(f"{where}: range_check needs at least one of 'min' / 'max'.")
    if min_v is not None and not isinstance(min_v, (int, float)):
        raise ConfigError(f"{where}: 'min' must be a number.")
    if max_v is not None and not isinstance(max_v, (int, float)):
        raise ConfigError(f"{where}: 'max' must be a number.")
    if min_v is not None and max_v is not None and float(min_v) > float(max_v):
        raise ConfigError(f"{where}: 'min' ({min_v}) must not exceed 'max' ({max_v}).")
    unit = str(raw.get("unit", "") or "").strip()
    title = str(raw.get("title", "") or "").strip() or f"Range check on {signal}"
    return RuleConfig(
        title=title,
        signal=signal,
        min_value=float(min_v) if min_v is not None else None,
        max_value=float(max_v) if max_v is not None else None,
        unit=unit,
        **common,
    )


def _parse_message_loss(raw: dict, where: str, common: dict) -> RuleConfig:
    signal = _require_signal(raw, where)
    gap = raw.get("max_gap_s")
    if not isinstance(gap, (int, float)) or gap <= 0:
        raise ConfigError(f"{where}: message_loss needs a positive 'max_gap_s'.")
    title = str(raw.get("title", "") or "").strip() or f"Message loss on {signal}"
    return RuleConfig(
        title=title,
        signal=signal,
        max_gap_s=float(gap),
        **common,
    )


def _parse_fault_signal(raw: dict, where: str, common: dict) -> RuleConfig:
    signal = _require_signal(raw, where)
    fw = raw.get("fault_when")
    if not isinstance(fw, dict) or len(fw) != 1:
        raise ConfigError(
            f"{where}: fault_signal needs a 'fault_when' mapping with exactly one "
            f"operator, e.g.  fault_when: {{ not_equals: 0 }}."
        )
    op = next(iter(fw))
    if op not in _FAULT_OPS:
        raise ConfigError(
            f"{where}: unknown fault_when operator {op!r}. "
            f"Expected one of {sorted(_FAULT_OPS)}."
        )
    title = str(raw.get("title", "") or "").strip() or f"Fault flag on {signal}"
    return RuleConfig(
        title=title,
        signal=signal,
        fault_when=dict(fw),
        **common,
    )
