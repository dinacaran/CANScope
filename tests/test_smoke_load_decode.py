"""
Smoke tests: end-to-end reader → signal extraction without LoadWorker.

Verifies the full decode pipeline for each supported format in a single
reader-iteration pass.  No GUI, no Qt, no worker thread needed.
"""
from __future__ import annotations

import pytest


# ── BLF smoke ────────────────────────────────────────────────────────────

def test_smoke_blf_yields_signals(blf_path, sample_dbc_path):
    from core.dbc_decoder import DBCDecoder
    from core.readers.blf_can_reader import BLFCANReader

    reader = BLFCANReader(str(blf_path), DBCDecoder(str(sample_dbc_path)))
    samples = list(reader)
    assert len(samples) > 0


def test_smoke_blf_signal_names(blf_path, sample_dbc_path):
    from core.dbc_decoder import DBCDecoder
    from core.readers.blf_can_reader import BLFCANReader

    reader = BLFCANReader(str(blf_path), DBCDecoder(str(sample_dbc_path)))
    names = {s.signal_name for s in reader}
    assert {"EngSpeed", "Throttle", "Gear"} == names


def test_smoke_blf_diag_no_signals_stat(blf_path, sample_dbc_path):
    from core.dbc_decoder import DBCDecoder
    from core.readers.blf_can_reader import BLFCANReader

    dec = DBCDecoder(str(sample_dbc_path))
    reader = BLFCANReader(str(blf_path), dec)
    list(reader)
    # 3 bursts × 1 DiagRequest = 3 signal-less matches
    assert dec.stats["decoded_no_signals"] == 3


# ── ASC smoke ─────────────────────────────────────────────────────────────

def test_smoke_asc_yields_signals(asc_path, sample_dbc_path):
    from core.dbc_decoder import DBCDecoder
    from core.readers.asc_can_reader import ASCCANReader

    reader = ASCCANReader(str(asc_path), DBCDecoder(str(sample_dbc_path)))
    samples = list(reader)
    assert len(samples) > 0


def test_smoke_asc_signal_names(asc_path, sample_dbc_path):
    from core.dbc_decoder import DBCDecoder
    from core.readers.asc_can_reader import ASCCANReader

    reader = ASCCANReader(str(asc_path), DBCDecoder(str(sample_dbc_path)))
    names = {s.signal_name for s in reader}
    assert {"EngSpeed", "Throttle", "Gear"} == names


def test_smoke_asc_diag_no_signals_stat(asc_path, sample_dbc_path):
    from core.dbc_decoder import DBCDecoder
    from core.readers.asc_can_reader import ASCCANReader

    dec = DBCDecoder(str(sample_dbc_path))
    reader = ASCCANReader(str(asc_path), dec)
    list(reader)
    assert dec.stats["decoded_no_signals"] == 3


# ── CSV narrow smoke ──────────────────────────────────────────────────────

def test_smoke_csv_narrow_yields_samples(narrow_csv_path):
    from core.readers.csv_reader import CSVSignalReader

    samples = list(CSVSignalReader(narrow_csv_path))
    assert len(samples) == 7


def test_smoke_csv_narrow_numeric_values(narrow_csv_path):
    from core.readers.csv_reader import CSVSignalReader

    samples = list(CSVSignalReader(narrow_csv_path))
    for s in samples:
        # All rows in the fixture have parseable numeric values
        assert s.numeric_value is not None


# ── DBC decoder smoke — no crash on DiagRequest ───────────────────────────

def test_smoke_diag_request_does_not_crash(decoder, frame_diag):
    # Must return empty list and not raise
    result = decoder.decode_frame(frame_diag)
    assert result == []
    assert decoder.stats["decoded_no_signals"] == 1
    assert decoder.stats["decode_fail"] == 0


# ── End-to-end: reader → SignalStore via add_samples ─────────────────────

def test_smoke_blf_into_signal_store(blf_path, sample_dbc_path):
    from core.dbc_decoder import DBCDecoder
    from core.readers.blf_can_reader import BLFCANReader
    from core.signal_store import SignalStore

    dec = DBCDecoder(str(sample_dbc_path))
    reader = BLFCANReader(str(blf_path), dec)
    store = SignalStore()
    for _frame, samples in reader.iter_with_frames():
        if samples:
            store.add_samples(samples)
    store.normalize_timestamps()

    assert len(store._series_by_key) == 3    # EngSpeed, Throttle, Gear
    assert store.total_samples > 0
