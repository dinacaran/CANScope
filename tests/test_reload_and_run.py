"""
Tests for DiagnosticEngine.reload_and_run — re-parse YAML and rerun against the
already-loaded SignalStore (no measurement reload; SignalStore is reused).
"""
from __future__ import annotations

import pytest

from core.diagnostics.engine import DiagnosticEngine
from tests.conftest import make_store_with_signals


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def config_dir(tmp_path):
    _write(tmp_path / "base_rules" / "motor.yaml",
           "domain: Motor\nrules:\n  - id: speed\n    condition: EngSpeed > 5000\n")
    return tmp_path


@pytest.fixture()
def engine(config_dir):
    return DiagnosticEngine(config_dir=config_dir)


def test_reload_and_run_reuses_same_store(engine):
    store = make_store_with_signals([5100.0])
    result = engine.reload_and_run(store, "Motor")
    # The engine must not have swapped or rebuilt the store instance.
    assert result.signal_count == len(store._series_by_key)
    assert result.has_findings()


def test_reload_picks_up_edited_yaml(engine, config_dir):
    store = make_store_with_signals([4500.0], throttle_vals=[85.0])

    # First run: only the >5000 rule exists → EngSpeed 4500 does not fire it.
    r1 = engine.reload_and_run(store, "Motor")
    assert not r1.has_findings()

    # Append a new rule that WILL fire, then reload_and_run the SAME store.
    _write(config_dir / "base_rules" / "motor.yaml",
           "domain: Motor\nrules:\n"
           "  - id: speed\n    condition: EngSpeed > 5000\n"
           "  - id: throttle\n    condition: Throttle > 80\n    severity: high\n")
    r2 = engine.reload_and_run(store, "Motor")
    assert r2.has_findings()
    assert any("throttle" in f.detector_name.lower() for f in r2.findings)


def test_reload_and_run_no_measurement_reload(engine, monkeypatch):
    """reload_and_run must not touch the SignalStore's data (read-only)."""
    store = make_store_with_signals([5100.0])
    keys_before = set(store._series_by_key.keys())
    engine.reload_and_run(store, "Motor")
    engine.reload_and_run(store, "Motor")
    assert set(store._series_by_key.keys()) == keys_before


# ── include_generated passthrough (the agent loop's hot path) ──────────────

def test_reload_and_run_excludes_generated_by_default(engine, config_dir):
    _write(config_dir / "generated" / "gen.yaml",
           "domain: Motor\nrules:\n  - id: gen1\n    condition: EngSpeed > 100\n")
    store = make_store_with_signals([5100.0])
    result = engine.reload_and_run(store, "Motor")
    assert not any(f.detector_name == "yaml::gen1" for f in result.findings)


def test_reload_and_run_includes_generated_when_requested(engine, config_dir):
    _write(config_dir / "generated" / "gen.yaml",
           "domain: Motor\nrules:\n  - id: gen1\n    condition: EngSpeed > 100\n")
    store = make_store_with_signals([5100.0])
    result = engine.reload_and_run(store, "Motor", include_generated=True)
    assert any(f.detector_name == "yaml::gen1" for f in result.findings)
