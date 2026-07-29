"""Tests for core/diagnostics/config_loader.py — YAML domain loading."""
from __future__ import annotations

import textwrap

import pytest

from core.diagnostics.config_loader import (
    load_one_config, load_domain_configs, ConfigError, DomainConfig,
)
from core.diagnostics.models import Severity


# ── Load motor_control_test.yaml ──────────────────────────────────────────

def test_domain_name(motor_control_yaml_path):
    cfg = load_one_config(motor_control_yaml_path)
    assert cfg.name == "MotorControl"


def test_rule_count(motor_control_yaml_path):
    cfg = load_one_config(motor_control_yaml_path)
    assert len(cfg.rules) == 3


def test_rule_ids(motor_control_yaml_path):
    cfg = load_one_config(motor_control_yaml_path)
    ids = [r.id for r in cfg.rules]
    assert "speed_high" in ids
    assert "gear_neutral" in ids
    assert "compound_fault" in ids


def test_severity_high(motor_control_yaml_path):
    cfg = load_one_config(motor_control_yaml_path)
    r = next(r for r in cfg.rules if r.id == "speed_high")
    assert r.severity == Severity.HIGH


def test_severity_medium(motor_control_yaml_path):
    cfg = load_one_config(motor_control_yaml_path)
    r = next(r for r in cfg.rules if r.id == "gear_neutral")
    assert r.severity == Severity.MEDIUM


def test_severity_critical(motor_control_yaml_path):
    cfg = load_one_config(motor_control_yaml_path)
    r = next(r for r in cfg.rules if r.id == "compound_fault")
    assert r.severity == Severity.CRITICAL


def test_context_window(motor_control_yaml_path):
    cfg = load_one_config(motor_control_yaml_path)
    assert cfg.context_window_s == pytest.approx(1.0)


def test_description_set(motor_control_yaml_path):
    cfg = load_one_config(motor_control_yaml_path)
    assert cfg.description == "Motor control fault detection rules"


def test_all_rules_enabled(motor_control_yaml_path):
    cfg = load_one_config(motor_control_yaml_path)
    assert all(r.enabled for r in cfg.rules)
    assert len(cfg.enabled_rules()) == 3


def test_condition_strings(motor_control_yaml_path):
    cfg = load_one_config(motor_control_yaml_path)
    conds = {r.condition for r in cfg.rules}
    assert "EngSpeed > 5000" in conds
    assert "Gear = 3" in conds
    assert "EngSpeed > 4000 and Throttle > 80" in conds


# ── Validation errors ─────────────────────────────────────────────────────

def test_missing_condition_raises(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(textwrap.dedent("""\
        domain: Test
        rules:
          - title: No condition here
    """))
    with pytest.raises(ConfigError, match="condition"):
        load_one_config(bad)


def test_invalid_severity_raises(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(textwrap.dedent("""\
        domain: Test
        rules:
          - condition: EngSpeed > 0
            severity: supersonic
    """))
    with pytest.raises(ConfigError, match="severity"):
        load_one_config(bad)


def test_missing_domain_field_raises(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("rules:\n  - condition: X > 0\n")
    with pytest.raises(ConfigError, match="domain"):
        load_one_config(bad)


def test_duplicate_rule_id_raises(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(textwrap.dedent("""\
        domain: Test
        rules:
          - id: dup
            condition: EngSpeed > 0
          - id: dup
            condition: EngSpeed > 1
    """))
    with pytest.raises(ConfigError, match="duplicate"):
        load_one_config(bad)


def test_non_list_rules_raises(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("domain: Test\nrules: not_a_list\n")
    with pytest.raises(ConfigError, match="list"):
        load_one_config(bad)


# ── bad expression condition is recorded, not fatal ───────────────────────
# A single mistyped condition must not skip the whole file (that would be a
# worse silence). It is recorded on the rule so the engine surfaces it.

def test_bad_condition_records_error_not_skips_file(tmp_path):
    p = tmp_path / "d.yaml"
    p.write_text(textwrap.dedent("""\
        domain: D
        rules:
          - id: broken
            condition: (A - B > 5
          - id: ok
            condition: A > 5
    """))
    cfg = load_one_config(p)
    assert [r.id for r in cfg.rules] == ["broken", "ok"]      # both loaded
    broken = next(r for r in cfg.rules if r.id == "broken")
    assert broken.condition_error and "position" in broken.condition_error
    assert broken.source_file == "d.yaml"
    ok = next(r for r in cfg.rules if r.id == "ok")
    assert ok.condition_error is None
    assert ok.source_file == "d.yaml"


def test_bad_condition_file_not_dropped(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "domain: D\nrules:\n  - id: broken\n    condition: A >\n", encoding="utf-8")
    domains = load_domain_configs(tmp_path)
    assert len(domains) == 1
    assert domains[0].rules[0].condition_error is not None


def test_valid_condition_has_no_error(motor_control_yaml_path):
    cfg = load_one_config(motor_control_yaml_path)
    assert all(r.condition_error is None for r in cfg.rules)


# ── load_domain_configs ───────────────────────────────────────────────────

def test_load_nonexistent_dir_returns_empty(tmp_path):
    result = load_domain_configs(tmp_path / "no_such_dir")
    assert result == []


def test_load_domain_configs_from_tmp_dir(tmp_path, motor_control_yaml_path):
    import shutil
    shutil.copy(motor_control_yaml_path, tmp_path / "mc.yaml")
    domains = load_domain_configs(tmp_path)
    assert len(domains) == 1
    assert domains[0].name == "MotorControl"
