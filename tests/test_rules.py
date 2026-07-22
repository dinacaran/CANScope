"""Tests for core/diagnostics/rules/expression.py — expression rule processor."""
from __future__ import annotations

import numpy as np
import pytest

from core.diagnostics.config_loader import RuleConfig
from core.diagnostics.context import DiagnosticContext
from core.diagnostics.models import Severity
from core.diagnostics.rules.expression import (
    run, parse, condition_signals, ExpressionError,
)

from tests.conftest import make_store_with_signals, make_test_domain


def _rule(condition: str, rule_id: str = "test_rule", severity=Severity.HIGH) -> RuleConfig:
    return RuleConfig(
        id=rule_id,
        type="expression",
        title=condition,
        severity=severity,
        condition=condition,
    )


def _ctx(store, domain=None):
    return DiagnosticContext(store, domain or make_test_domain())


# ── parser unit tests (new recursive-descent API) ─────────────────────────

def test_parse_simple():
    parse("EngSpeed > 5000")                       # does not raise
    assert condition_signals("EngSpeed > 5000") == ["EngSpeed"]


def test_parse_compound_and():
    assert condition_signals("EngSpeed > 4000 and Throttle > 80") == ["EngSpeed", "Throttle"]


def test_parse_compound_or():
    parse("Gear = 3 or Gear = 4")
    assert condition_signals("Gear = 3 or Gear = 4") == ["Gear"]


def test_parse_equality_operator():
    parse("Gear = 3")
    parse("Gear == 3")                             # == is a synonym for =


def test_parse_not_equal():
    parse("Gear != 0")


def test_parse_invalid_raises():
    with pytest.raises(ExpressionError):
        parse("not a valid expression")


def test_parse_empty_raises():
    with pytest.raises(ExpressionError):
        parse("")


def test_expression_error_is_valueerror():
    # Subclassing ValueError keeps triage / config_loader's `except ValueError`
    # working unchanged.
    assert issubclass(ExpressionError, ValueError)


# ── Single-signal threshold ───────────────────────────────────────────────

def test_threshold_fires_when_exceeded():
    store = make_store_with_signals([1200.0, 5100.0, 5200.0])
    findings = run(_rule("EngSpeed > 5000"), _ctx(store))
    assert len(findings) == 1


def test_threshold_no_finding_when_all_below():
    store = make_store_with_signals([1200.0, 2000.0, 3000.0])
    findings = run(_rule("EngSpeed > 5000"), _ctx(store))
    assert findings == []


def test_threshold_finding_detector_name():
    store = make_store_with_signals([1200.0, 5100.0])
    findings = run(_rule("EngSpeed > 5000", rule_id="speed_high"), _ctx(store))
    assert findings[0].detector_name == "yaml::speed_high"


def test_threshold_finding_title():
    store = make_store_with_signals([1200.0, 5100.0])
    rule = _rule("EngSpeed > 5000")
    rule = RuleConfig(
        id="r1", type="expression", title="Engine speed too high",
        severity=Severity.HIGH, condition="EngSpeed > 5000",
    )
    findings = run(rule, _ctx(store))
    assert findings[0].title == "Engine speed too high"


def test_threshold_finding_severity():
    store = make_store_with_signals([1200.0, 5100.0])
    findings = run(_rule("EngSpeed > 5000", severity=Severity.CRITICAL), _ctx(store))
    assert findings[0].severity == Severity.CRITICAL


def test_threshold_time_window_is_first_fault():
    # ts are 0.001, 0.002, 0.003 (linspace in make_store_with_signals)
    store = make_store_with_signals([1200.0, 5100.0, 5200.0])
    findings = run(_rule("EngSpeed > 5000"), _ctx(store))
    t_first = findings[0].time_window[0]
    # second sample (index 1) is first to exceed 5000; ts = 0.002
    assert t_first == pytest.approx(0.002)


def test_threshold_metrics_fault_samples():
    store = make_store_with_signals([1200.0, 5100.0, 5200.0])
    findings = run(_rule("EngSpeed > 5000"), _ctx(store))
    assert findings[0].metrics["fault_samples"] == 2


# ── Enum equality ─────────────────────────────────────────────────────────

def test_enum_equality_fires():
    store = make_store_with_signals([1200.0], gear_vals=[4.0, 3.0, 4.0])
    findings = run(_rule("Gear = 3"), _ctx(store))
    assert len(findings) == 1


def test_enum_equality_no_fault():
    store = make_store_with_signals([1200.0], gear_vals=[4.0, 4.0])
    findings = run(_rule("Gear = 3"), _ctx(store))
    assert findings == []


# ── Compound conditions ───────────────────────────────────────────────────

def test_compound_and_both_true():
    # Both EngSpeed > 4000 AND Throttle > 80 must be present
    store = make_store_with_signals([4500.0], throttle_vals=[85.0])
    findings = run(_rule("EngSpeed > 4000 and Throttle > 80"), _ctx(store))
    assert len(findings) == 1


def test_compound_and_one_false():
    # Throttle never > 80 → no finding
    store = make_store_with_signals([4500.0], throttle_vals=[50.0])
    findings = run(_rule("EngSpeed > 4000 and Throttle > 80"), _ctx(store))
    assert findings == []


def test_compound_or_first_true():
    # Gear 3 triggers (neutral), Gear 1 in data
    store = make_store_with_signals([1200.0], gear_vals=[3.0])
    findings = run(_rule("Gear = 3 or Gear = 5"), _ctx(store))
    assert len(findings) == 1


# ── Missing signal ────────────────────────────────────────────────────────

def test_missing_signal_returns_empty():
    store = make_store_with_signals([1200.0])
    findings = run(_rule("NonExistentSignal > 0"), _ctx(store))
    assert findings == []


def test_missing_one_signal_in_compound_returns_empty():
    store = make_store_with_signals([4500.0])
    # Throttle not in store
    findings = run(_rule("EngSpeed > 4000 and Throttle > 80"), _ctx(store))
    assert findings == []


# ── Signals list in finding ───────────────────────────────────────────────

def test_finding_signals_contains_store_key():
    store = make_store_with_signals([5100.0])
    findings = run(_rule("EngSpeed > 5000"), _ctx(store))
    assert any("EngSpeed" in key for key in findings[0].signals)
