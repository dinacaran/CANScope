"""Tests for core/diagnostics/evidence.py — EvidenceBuilder compact evidence."""
from __future__ import annotations

import numpy as np
import pytest

from core.diagnostics.evidence import EvidenceBuilder
from core.diagnostics.models import Finding, Severity

from tests.conftest import make_store_with_signals, make_test_domain
from core.diagnostics.context import DiagnosticContext


def _ctx(store):
    return DiagnosticContext(store, make_test_domain())


def _finding(t_start: float = 0.001, t_end: float = 0.001, signals=None) -> Finding:
    return Finding(
        detector_name="yaml::test",
        title="Test fault",
        description="Some description",
        severity=Severity.HIGH,
        time_window=(t_start, t_end),
        signals=signals or ["CH1::EngineControl::EngSpeed"],
    )


# ── _stats ────────────────────────────────────────────────────────────────

def test_stats_min_max():
    eb = EvidenceBuilder()
    vals = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    s = eb._stats(vals)
    assert s["min"] == pytest.approx(1.0)
    assert s["max"] == pytest.approx(5.0)


def test_stats_mean():
    eb = EvidenceBuilder()
    vals = np.array([0.0, 10.0])
    s = eb._stats(vals)
    assert s["mean"] == pytest.approx(5.0)


def test_stats_empty_array():
    eb = EvidenceBuilder()
    vals = np.array([], dtype=np.float64)
    s = eb._stats(vals)
    assert s == {}


def test_stats_ignores_nan():
    eb = EvidenceBuilder()
    vals = np.array([1.0, float("nan"), 3.0])
    s = eb._stats(vals)
    assert s["n"] == 2.0


# ── _downsample ────────────────────────────────────────────────────────────

def test_downsample_small_array_unchanged():
    eb = EvidenceBuilder()
    ts = np.linspace(0, 1, 50)
    vals = np.ones(50)
    ts_out, vals_out = eb._downsample(ts, vals)
    assert len(ts_out) == 50


def test_downsample_large_array_to_100():
    eb = EvidenceBuilder()
    ts = np.linspace(0, 1, 200)
    vals = np.ones(200)
    ts_out, vals_out = eb._downsample(ts, vals)
    assert len(ts_out) == 100


def test_downsample_preserves_endpoints():
    eb = EvidenceBuilder()
    ts = np.linspace(0.0, 1.0, 200)
    vals = np.arange(200, dtype=float)
    ts_out, vals_out = eb._downsample(ts, vals)
    assert ts_out[0] == pytest.approx(0.0)
    assert ts_out[-1] == pytest.approx(1.0)


# ── _format_window ────────────────────────────────────────────────────────

def test_format_window_empty():
    eb = EvidenceBuilder()
    assert eb._format_window({}) == ""


def test_format_window_contains_signal_name():
    eb = EvidenceBuilder()
    ts = np.array([0.001, 0.002])
    vals = np.array([1200.0, 1300.0])
    result = eb._format_window({"EngSpeed": (ts, vals)})
    assert "EngSpeed" in result


def test_format_window_contains_values():
    eb = EvidenceBuilder()
    ts = np.array([0.001])
    vals = np.array([1200.0])
    result = eb._format_window({"EngSpeed": (ts, vals)})
    assert "1200" in result


# ── build_for_finding ─────────────────────────────────────────────────────

def test_build_for_finding_summary_non_empty():
    store = make_store_with_signals([1200.0, 5100.0, 5200.0])
    ctx = _ctx(store)
    eb = EvidenceBuilder()
    finding = _finding()
    ev = eb.build_for_finding(finding, ctx)
    assert ev.summary != ""


def test_build_for_finding_time_window_padded():
    store = make_store_with_signals([1200.0, 5100.0])
    ctx = _ctx(store)
    eb = EvidenceBuilder()
    finding = _finding(t_start=0.5, t_end=0.5)
    ev = eb.build_for_finding(finding, ctx)
    # default padding is context_window_s from domain (make_test_domain has default 2.0)
    assert ev.time_window[0] <= 0.5
    assert ev.time_window[1] >= 0.5


def test_build_for_finding_signals_summary_has_stats():
    store = make_store_with_signals([1200.0, 1300.0, 1400.0])
    ctx = _ctx(store)
    eb = EvidenceBuilder()
    finding = _finding()
    ev = eb.build_for_finding(finding, ctx)
    assert "EngSpeed" in ev.signals_summary
    assert "min" in ev.signals_summary["EngSpeed"]


def test_build_for_finding_signal_not_in_store():
    store = make_store_with_signals([1200.0])
    ctx = _ctx(store)
    eb = EvidenceBuilder()
    finding = _finding(signals=["CH1::Missing::Signal"])
    ev = eb.build_for_finding(finding, ctx)
    # Should not crash; signals_summary will be empty
    assert ev.summary != ""


# ── to_text ──────────────────────────────────────────────────────────────

def test_to_text_contains_summary():
    store = make_store_with_signals([1200.0])
    ctx = _ctx(store)
    eb = EvidenceBuilder()
    ev = eb.build_for_finding(_finding(), ctx)
    text = ev.to_text()
    assert "SUMMARY:" in text


def test_to_text_contains_time_window():
    store = make_store_with_signals([1200.0])
    ctx = _ctx(store)
    eb = EvidenceBuilder()
    ev = eb.build_for_finding(_finding(t_start=0.5), ctx)
    text = ev.to_text()
    assert "TIME WINDOW:" in text


# ── build_manifest ────────────────────────────────────────────────────────

def test_build_manifest_contains_signal_count():
    store = make_store_with_signals([1200.0], throttle_vals=[50.0])
    ctx = DiagnosticContext(store, make_test_domain())
    eb = EvidenceBuilder()
    manifest = eb.build_manifest(ctx)
    assert "2" in manifest  # 2 decoded signals
