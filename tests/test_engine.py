"""Tests for core/diagnostics/engine.py — DiagnosticEngine orchestrator."""
from __future__ import annotations

import shutil

import pytest

from core.diagnostics.engine import DiagnosticEngine
from core.diagnostics.config_loader import ConfigError

from tests.conftest import make_store_with_signals


# ── Setup: engine pointing at a temp dir containing motor_control_test.yaml ─

@pytest.fixture()
def diag_dir(tmp_path, motor_control_yaml_path):
    shutil.copy(motor_control_yaml_path, tmp_path / "motor_control_test.yaml")
    return tmp_path


@pytest.fixture()
def engine(diag_dir):
    e = DiagnosticEngine(config_dir=diag_dir)
    e.load_configs()
    return e


# ── load_configs ──────────────────────────────────────────────────────────

def test_load_configs_finds_domain(engine):
    domains = engine.get_domains()
    names = [d.name for d in domains]
    assert "MotorControl" in names


def test_load_configs_returns_domain_list(engine):
    assert len(engine.get_domains()) == 1


def test_find_domain_returns_domain(engine):
    d = engine.find_domain("MotorControl")
    assert d is not None
    assert d.name == "MotorControl"


def test_find_domain_returns_none_for_unknown(engine):
    assert engine.find_domain("NoSuchDomain") is None


# ── run — fault detected ──────────────────────────────────────────────────

def test_run_finds_speed_fault(engine):
    # EngSpeed > 5000 is triggered in motor_control_test.yaml
    store = make_store_with_signals([1200.0, 5100.0, 5200.0])
    result = engine.run(store, "MotorControl")
    assert result.has_findings()


def test_run_finding_title(engine):
    store = make_store_with_signals([5100.0])
    result = engine.run(store, "MotorControl")
    titles = [f.title for f in result.findings]
    assert any("speed" in t.lower() for t in titles)


def test_run_finding_has_evidence(engine):
    store = make_store_with_signals([5100.0])
    result = engine.run(store, "MotorControl")
    assert all(f.evidence is not None for f in result.findings)


def test_run_signal_count_in_result(engine):
    store = make_store_with_signals([1200.0, 5100.0], throttle_vals=[50.0])
    result = engine.run(store, "MotorControl")
    assert result.signal_count == 2


def test_run_duration_is_positive(engine):
    store = make_store_with_signals([5100.0])
    result = engine.run(store, "MotorControl")
    assert result.duration_s > 0.0


# ── run — no fault ────────────────────────────────────────────────────────

def test_run_no_fault_when_all_below_threshold(engine):
    store = make_store_with_signals([1200.0, 2000.0, 3000.0])
    result = engine.run(store, "MotorControl")
    # Only speed_high and compound_fault could fire; gear_neutral needs Gear signal
    # No signal exceeds 5000 → speed_high does not fire
    # compound_fault: EngSpeed > 4000 and Throttle > 80 — Throttle absent → no fire
    speed_findings = [f for f in result.findings if "speed" in f.title.lower()]
    assert speed_findings == []


def test_run_empty_store_produces_no_findings(engine):
    from core.signal_store import SignalStore
    store = SignalStore()
    result = engine.run(store, "MotorControl")
    assert not result.has_findings()


# ── run — unknown domain ──────────────────────────────────────────────────

def test_run_unknown_domain_raises(engine):
    store = make_store_with_signals([1200.0])
    with pytest.raises(ConfigError):
        engine.run(store, "NonExistentDomain")


# ── result helpers ────────────────────────────────────────────────────────

def test_result_critical_count(engine):
    # compound_fault is CRITICAL; fires when EngSpeed > 4000 AND Throttle > 80
    store = make_store_with_signals([4500.0], throttle_vals=[85.0])
    result = engine.run(store, "MotorControl")
    assert result.critical_count() >= 1


def test_result_by_severity_ordering(engine):
    store = make_store_with_signals([5100.0], throttle_vals=[85.0])
    result = engine.run(store, "MotorControl")
    if len(result.findings) > 1:
        severities = [int(f.severity) for f in result.by_severity()]
        assert severities == sorted(severities, reverse=True)
