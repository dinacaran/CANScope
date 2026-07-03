"""Tests for core/channel_config.py — save/load round-trip and query helpers."""
from __future__ import annotations

import json

import pytest

from core.channel_config import ChannelConfig, ALL_CHANNELS_KEY


# ── Loading ────────────────────────────────────────────────────────────────

def test_load_legacy_v1(legacy_v1_path):
    cfg = ChannelConfig.load(legacy_v1_path)
    assert cfg.name == "Legacy Config"
    assert 1 in cfg.channels


def test_load_legacy_v1_channel_path(legacy_v1_path):
    cfg = ChannelConfig.load(legacy_v1_path)
    assert cfg.channels[1] == "C:/fake/engine.dbc"


def test_load_wrong_type_raises(tmp_path):
    bad = tmp_path / "bad.canscope_ch"
    bad.write_text(json.dumps({"type": "something_else", "channels": {}}))
    with pytest.raises(ValueError, match="Not a channel config file"):
        ChannelConfig.load(bad)


# ── Save / round-trip ──────────────────────────────────────────────────────

def test_save_round_trip(tmp_path, sample_dbc_path):
    out = tmp_path / "test.canscope_ch"
    cfg = ChannelConfig(name="Test", channels={1: str(sample_dbc_path)})
    cfg.save(out)

    loaded = ChannelConfig.load(out)
    assert loaded.name == "Test"
    assert loaded.channels[1] == str(sample_dbc_path)


def test_save_writes_version_2(tmp_path):
    out = tmp_path / "test.canscope_ch"
    ChannelConfig(name="X", channels={}).save(out)
    data = json.loads(out.read_text())
    assert data["version"] == 2


def test_save_writes_type_field(tmp_path):
    out = tmp_path / "test.canscope_ch"
    ChannelConfig(name="X", channels={}).save(out)
    data = json.loads(out.read_text())
    assert data["type"] == "canscope_channel_config"


# ── Factory ───────────────────────────────────────────────────────────────

def test_from_single_dbc_uses_all_channels_key(sample_dbc_path):
    cfg = ChannelConfig.from_single_dbc(str(sample_dbc_path))
    assert ALL_CHANNELS_KEY in cfg.channels


def test_from_single_dbc_name_is_stem(sample_dbc_path):
    cfg = ChannelConfig.from_single_dbc(str(sample_dbc_path))
    assert cfg.name == sample_dbc_path.stem


# ── Query helpers ──────────────────────────────────────────────────────────

def test_dbc_path_for_specific_channel(sample_dbc_path):
    cfg = ChannelConfig(channels={1: str(sample_dbc_path)})
    assert cfg.dbc_path_for(1) == str(sample_dbc_path)


def test_dbc_path_for_falls_back_to_all_channels(sample_dbc_path):
    cfg = ChannelConfig(channels={ALL_CHANNELS_KEY: str(sample_dbc_path)})
    assert cfg.dbc_path_for(99) == str(sample_dbc_path)


def test_dbc_path_for_returns_none_when_unassigned():
    cfg = ChannelConfig(channels={})
    assert cfg.dbc_path_for(1) is None


def test_is_empty_on_fresh():
    assert ChannelConfig().is_empty()


def test_is_empty_false_when_assigned(sample_dbc_path):
    cfg = ChannelConfig(channels={1: str(sample_dbc_path)})
    assert not cfg.is_empty()


def test_all_dbc_paths_deduplicates(sample_dbc_path):
    p = str(sample_dbc_path)
    cfg = ChannelConfig(channels={1: p, 2: p})
    assert cfg.all_dbc_paths() == [p]


def test_assigned_channels_excludes_all_channels_key(sample_dbc_path):
    p = str(sample_dbc_path)
    cfg = ChannelConfig(channels={ALL_CHANNELS_KEY: p, 1: p})
    assert ALL_CHANNELS_KEY not in cfg.assigned_channels()
    assert 1 in cfg.assigned_channels()


def test_summary_contains_name(sample_dbc_path):
    cfg = ChannelConfig(name="My Config", channels={1: str(sample_dbc_path)})
    assert "My Config" in cfg.summary()
