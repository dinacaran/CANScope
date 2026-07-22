"""Tests for core/readers/blf_can_reader.py — BLF frame iteration and decode."""
from __future__ import annotations

import pytest

from core.dbc_decoder import DBCDecoder
from core.readers.blf_can_reader import BLFCANReader


@pytest.fixture()
def blf_reader(blf_path, sample_dbc_path):
    return BLFCANReader(str(blf_path), DBCDecoder(str(sample_dbc_path)))


# ── Interface properties ──────────────────────────────────────────────────

def test_has_raw_frames(blf_reader):
    assert blf_reader.has_raw_frames is True


def test_source_description_contains_filename(blf_reader):
    assert "sample.blf" in blf_reader.source_description


# ── iter_frames_only ──────────────────────────────────────────────────────

def test_iter_frames_only_yields_frames(blf_reader):
    frames = list(blf_reader.iter_frames_only())
    assert len(frames) > 0


def test_iter_frames_only_count(blf_reader):
    # _generate.py writes 3 bursts × 3 messages = 9 frames
    frames = list(blf_reader.iter_frames_only())
    assert len(frames) == 9


def test_iter_frames_only_arb_ids(blf_reader):
    frames = list(blf_reader.iter_frames_only())
    ids = {f.arbitration_id for f in frames}
    assert 0x100 in ids
    assert 0x200 in ids
    assert 0x300 in ids


def test_iter_frames_only_channel(blf_reader):
    frames = list(blf_reader.iter_frames_only())
    assert all(f.channel is not None for f in frames)


def test_iter_raw_batches_preserves_count_and_ids(blf_reader):
    batches = list(blf_reader.iter_raw_batches(batch_size=4))
    assert sum(len(batch[1]) for batch in batches) == 9
    assert {arb_id for batch in batches for arb_id in batch[3]} == {
        0x100, 0x200, 0x300,
    }


def test_iter_raw_batches_bypasses_can_message_construction(blf_reader, monkeypatch):
    import can.io.blf

    def fail_message_construction(*_args, **_kwargs):
        raise AssertionError("bulk BLF path must not allocate can.Message objects")

    monkeypatch.setattr(can.io.blf, "Message", fail_message_construction)
    batches = list(blf_reader.iter_raw_batches(batch_size=4))
    assert sum(len(batch[1]) for batch in batches) == 9


# ── Decoded signal iteration ──────────────────────────────────────────────

def test_yields_decoded_samples(blf_reader):
    samples = list(blf_reader)
    assert len(samples) > 0


def test_decoded_sample_signal_names(blf_reader):
    samples = list(blf_reader)
    names = {s.signal_name for s in samples}
    assert "EngSpeed" in names
    assert "Throttle" in names


def test_decoded_sample_values(blf_reader):
    samples = list(blf_reader)
    eng_samples = [s for s in samples if s.signal_name == "EngSpeed"]
    assert all(s.numeric_value == pytest.approx(1200.0) for s in eng_samples)


def test_iter_with_frames_yields_pairs(blf_reader):
    pairs = list(blf_reader.iter_with_frames())
    assert all(len(pair) == 2 for pair in pairs)


# ── Missing-file error ────────────────────────────────────────────────────

def test_missing_blf_raises(tmp_path, sample_dbc_path):
    from core.blf_reader import BLFReadError

    reader = BLFCANReader(
        str(tmp_path / "nonexistent.blf"),
        DBCDecoder(str(sample_dbc_path)),
    )
    with pytest.raises(BLFReadError):
        list(reader.iter_frames_only())
