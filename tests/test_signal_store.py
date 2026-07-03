"""Tests for core/signal_store.py — sample ingestion and tree building."""
from __future__ import annotations

import array

import numpy as np
import pytest

from core.models import DecodedSignalSample
from core.signal_store import SignalStore, SignalSeries


def _sample(ts: float, sig: str = "EngSpeed", val: float = 1200.0) -> DecodedSignalSample:
    return DecodedSignalSample(
        timestamp=ts,
        channel=1,
        message_id=0x100,
        message_name="EngineControl",
        signal_name=sig,
        value=val,
        unit="rpm",
        is_extended_id=False,
        direction="Rx",
        numeric_value=val,
    )


# ── add_samples ────────────────────────────────────────────────────────────

def test_add_samples_increments_total(signal_store):
    signal_store.add_samples([_sample(0.001), _sample(0.002)])
    assert signal_store.total_samples == 2


def test_add_samples_creates_series(signal_store):
    signal_store.add_samples([_sample(0.001)])
    assert signal_store.get_series("CH1::EngineControl::EngSpeed") is not None


def test_add_samples_key_format(signal_store):
    signal_store.add_samples([_sample(0.001)])
    assert "CH1::EngineControl::EngSpeed" in signal_store.all_keys()


def test_add_samples_two_signals(signal_store):
    signal_store.add_samples([_sample(0.001, "EngSpeed"), _sample(0.001, "Throttle")])
    assert len(signal_store._series_by_key) == 2


def test_add_samples_channel_tracked(signal_store):
    signal_store.add_samples([_sample(0.001)])
    assert 1 in signal_store.channels


# ── add_samples_direct (hot path) ─────────────────────────────────────────

def test_add_samples_direct_creates_series(signal_store):
    signal_store.add_samples_direct([_sample(0.001)])
    assert signal_store.get_series("CH1::EngineControl::EngSpeed") is not None


def test_add_samples_direct_increments_total(signal_store):
    signal_store.add_samples_direct([_sample(0.001), _sample(0.002)])
    assert signal_store.total_samples == 2


# ── add_series_bulk ────────────────────────────────────────────────────────

def test_add_series_bulk_creates_series(signal_store):
    ts = np.array([0.001, 0.002, 0.003])
    vals = np.array([1200.0, 1300.0, 1400.0])
    signal_store.add_series_bulk(
        channel=1, message_name="EngineControl", message_id=0x100,
        signal_name="EngSpeed", unit="rpm",
        timestamps=ts, values=vals, raw_values=[], has_labels=False,
    )
    series = signal_store.get_series("CH1::EngineControl::EngSpeed")
    assert series is not None
    assert len(series.timestamps) == 3


def test_add_series_bulk_values_correct(signal_store):
    ts = np.array([0.001, 0.002])
    vals = np.array([600.0, 650.0])
    signal_store.add_series_bulk(
        channel=1, message_name="EngineControl", message_id=0x100,
        signal_name="EngSpeed", unit="rpm",
        timestamps=ts, values=vals, raw_values=[], has_labels=False,
    )
    series = signal_store.get_series("CH1::EngineControl::EngSpeed")
    assert list(series.values) == pytest.approx([600.0, 650.0])


def test_add_series_bulk_empty_is_noop(signal_store):
    ts = np.array([], dtype=np.float64)
    vals = np.array([], dtype=np.float64)
    signal_store.add_series_bulk(
        channel=1, message_name="EngineControl", message_id=0x100,
        signal_name="EngSpeed", unit="rpm",
        timestamps=ts, values=vals, raw_values=[], has_labels=False,
    )
    assert signal_store.get_series("CH1::EngineControl::EngSpeed") is None


# ── numpy helpers on SignalSeries ──────────────────────────────────────────

def test_numpy_timestamps_dtype(signal_store):
    signal_store.add_samples([_sample(0.001), _sample(0.002)])
    series = signal_store.get_series("CH1::EngineControl::EngSpeed")
    ts = series.numpy_timestamps()
    assert ts.dtype == np.float64


def test_numpy_values_correct(signal_store):
    signal_store.add_samples([_sample(0.001, val=1200.0), _sample(0.002, val=1300.0)])
    series = signal_store.get_series("CH1::EngineControl::EngSpeed")
    vals = series.numpy_values()
    assert vals == pytest.approx([1200.0, 1300.0])


# ── build_tree_payload ─────────────────────────────────────────────────────

def test_build_tree_payload_structure(signal_store):
    signal_store.add_samples([_sample(0.001)])
    tree = signal_store.build_tree_payload()
    assert 1 in tree
    assert "EngineControl" in tree[1]
    assert "EngSpeed" in tree[1]["EngineControl"]


def test_build_tree_payload_clears_dirty_flag(signal_store):
    signal_store.add_samples([_sample(0.001)])
    assert signal_store.is_tree_dirty()
    signal_store.build_tree_payload()
    assert not signal_store.is_tree_dirty()


# ── normalize_timestamps ──────────────────────────────────────────────────

def test_normalize_timestamps_shifts_to_zero(signal_store):
    signal_store.add_samples([_sample(10.0), _sample(11.0)])
    signal_store.normalize_timestamps(already_normalized=False)
    series = signal_store.get_series("CH1::EngineControl::EngSpeed")
    assert list(series.timestamps)[0] == pytest.approx(0.0)


# ── get_series ────────────────────────────────────────────────────────────

def test_get_series_returns_none_for_unknown(signal_store):
    assert signal_store.get_series("CH99::Foo::Bar") is None


# ── SignalSeries key property ──────────────────────────────────────────────

def test_series_key_no_channel():
    s = SignalSeries(
        channel=None, message_name="Msg", message_id=1,
        signal_name="Sig", unit="",
    )
    assert s.key == "CH?::Msg::Sig"


def test_series_latest_value_empty():
    s = SignalSeries(
        channel=1, message_name="Msg", message_id=1,
        signal_name="Sig", unit="",
    )
    assert s.latest_value == ""
