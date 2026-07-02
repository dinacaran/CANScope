"""Tests for core/readers/asc_can_reader.py — ASC frame iteration and decode."""
from __future__ import annotations

import pytest

from core.dbc_decoder import DBCDecoder
from core.readers.asc_can_reader import ASCCANReader


@pytest.fixture()
def asc_reader(asc_path, sample_dbc_path):
    return ASCCANReader(str(asc_path), DBCDecoder(str(sample_dbc_path)))


# ── Interface properties ──────────────────────────────────────────────────

def test_has_raw_frames(asc_reader):
    assert asc_reader.has_raw_frames is True


def test_source_description_contains_filename(asc_reader):
    assert "sample.asc" in asc_reader.source_description


# ── iter_frames_only ──────────────────────────────────────────────────────

def test_iter_frames_only_yields_frames(asc_reader):
    frames = list(asc_reader.iter_frames_only())
    assert len(frames) > 0


def test_iter_frames_only_count(asc_reader):
    frames = list(asc_reader.iter_frames_only())
    assert len(frames) == 9


def test_iter_frames_only_arb_ids(asc_reader):
    frames = list(asc_reader.iter_frames_only())
    ids = {f.arbitration_id for f in frames}
    assert 0x100 in ids
    assert 0x200 in ids
    assert 0x300 in ids


# ── Decoded signal iteration ──────────────────────────────────────────────

def test_yields_decoded_samples(asc_reader):
    samples = list(asc_reader)
    assert len(samples) > 0


def test_decoded_sample_signal_names(asc_reader):
    samples = list(asc_reader)
    names = {s.signal_name for s in samples}
    assert "EngSpeed" in names
    assert "Throttle" in names


def test_gear_label_is_drive(asc_reader):
    samples = list(asc_reader)
    gear_samples = [s for s in samples if s.signal_name == "Gear"]
    assert len(gear_samples) > 0
    assert all(s.value == "Drive" for s in gear_samples)


# ── Missing-file error ────────────────────────────────────────────────────

def test_missing_asc_raises(tmp_path, sample_dbc_path):
    from core.readers.asc_can_reader import ASCReadError

    reader = ASCCANReader(
        str(tmp_path / "nonexistent.asc"),
        DBCDecoder(str(sample_dbc_path)),
    )
    with pytest.raises(ASCReadError):
        list(reader.iter_frames_only())
