"""Tests for core/dbc_decoder.py — decode pipeline and stats tracking."""
from __future__ import annotations

import pytest

from core.models import RawFrame


# ── Loading ────────────────────────────────────────────────────────────────

def test_load_sample_dbc(decoder):
    assert len(decoder.database.messages) == 3


def test_message_names(decoder):
    names = {m.name for m in decoder.database.messages}
    assert names == {"EngineControl", "GearStatus", "DiagRequest"}


def test_missing_dbc_raises(tmp_path):
    from core.dbc_decoder import DBCDecoder, DBCLoadError

    with pytest.raises(DBCLoadError):
        DBCDecoder(str(tmp_path / "nonexistent.dbc"))


# ── Decode: EngineControl ──────────────────────────────────────────────────

def test_decode_engine_control_returns_two_signals(decoder, frame_engine):
    samples = decoder.decode_frame(frame_engine)
    assert len(samples) == 2


def test_decode_engine_speed_value(decoder, frame_engine):
    samples = decoder.decode_frame(frame_engine)
    eng_speed = next(s for s in samples if s.signal_name == "EngSpeed")
    assert eng_speed.numeric_value == pytest.approx(1200.0)


def test_decode_throttle_value(decoder, frame_engine):
    samples = decoder.decode_frame(frame_engine)
    throttle = next(s for s in samples if s.signal_name == "Throttle")
    assert throttle.numeric_value == pytest.approx(50.0)


def test_decode_sample_metadata(decoder, frame_engine):
    samples = decoder.decode_frame(frame_engine)
    s = samples[0]
    assert s.channel == 1
    assert s.message_name == "EngineControl"
    assert s.message_id == 0x100
    assert s.timestamp == pytest.approx(0.001)


# ── Decode: GearStatus (enum signal, decode_choices=False) ────────────────

def test_decode_gear_raw_value(decoder, frame_gear):
    samples = decoder.decode_frame(frame_gear)
    assert len(samples) == 1
    gear = samples[0]
    assert gear.signal_name == "Gear"
    assert gear.numeric_value == pytest.approx(4.0)


def test_decode_gear_display_label(decoder, frame_gear):
    samples = decoder.decode_frame(frame_gear)
    gear = samples[0]
    # choices cache should produce the string label
    assert gear.value == "Drive"


# ── Decode: DiagRequest (message with no signals) ─────────────────────────

def test_decode_diag_returns_empty(decoder, frame_diag):
    samples = decoder.decode_frame(frame_diag)
    assert samples == []


def test_decode_diag_increments_no_signals_stat(decoder, frame_diag):
    decoder.decode_frame(frame_diag)
    assert decoder.stats["decoded_no_signals"] == 1


def test_decode_diag_does_not_increment_fail_stat(decoder, frame_diag):
    decoder.decode_frame(frame_diag)
    assert decoder.stats["decode_fail"] == 0


# ── Decode: unknown arbitration ID ────────────────────────────────────────

def test_decode_unknown_id_returns_empty(decoder):
    frame = RawFrame(
        timestamp=0.0, channel=1, arbitration_id=0xDEAD,
        is_extended_id=False, is_fd=False, dlc=8,
        data=bytes(8), direction="Rx",
    )
    assert decoder.decode_frame(frame) == []


def test_decode_unknown_id_no_fail_stat(decoder):
    frame = RawFrame(
        timestamp=0.0, channel=1, arbitration_id=0xDEAD,
        is_extended_id=False, is_fd=False, dlc=8,
        data=bytes(8), direction="Rx",
    )
    decoder.decode_frame(frame)
    assert decoder.stats["decode_fail"] == 0


# ── Stats accumulation ─────────────────────────────────────────────────────

def test_stats_decode_success_count(decoder, frame_engine, frame_gear):
    decoder.decode_frame(frame_engine)
    decoder.decode_frame(frame_gear)
    assert decoder.stats["decode_success"] == 2


def test_stats_no_signals_accumulates(decoder, frame_diag):
    decoder.decode_frame(frame_diag)
    decoder.decode_frame(frame_diag)
    assert decoder.stats["decoded_no_signals"] == 2


# ── diagnostics_text ──────────────────────────────────────────────────────

def test_diagnostics_text_contains_dbc_label(decoder):
    text = decoder.diagnostics_text()
    assert "DBC file:" in text


def test_diagnostics_text_shows_message_count(decoder):
    text = decoder.diagnostics_text()
    assert "3" in text


def test_diagnostics_text_contains_no_signals_counter(decoder, frame_diag):
    decoder.decode_frame(frame_diag)
    text = decoder.diagnostics_text()
    assert "Matched, no signals:" in text
    assert "1" in text
