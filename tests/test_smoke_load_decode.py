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


def test_can_raw_worker_publishes_one_bulk_signal_handoff(sample_dbc_path):
    """Large BLF/ASC group sets must not rebuild the Qt tree incrementally."""
    from core.channel_config import ChannelConfig
    from core.load_worker import LoadWorker
    from core.signal_store import SignalStore

    class PackedReader:
        def iter_raw_batches(self, _batch_size):
            timestamps = []
            channels = []
            arb_ids = []
            dlcs = []
            directions = []
            flags = []
            data_block = bytearray(51 * 64)
            payloads = (
                (0x100, bytes([0x60, 0x09, 0x64, 0, 0, 0, 0, 0])),
                (0x200, bytes([0x04, 0, 0, 0])),
                (0x300, bytes(8)),
            )
            for channel in range(1, 18):
                for arb_id, payload in payloads:
                    slot = len(timestamps)
                    timestamps.append(slot * 0.001)
                    channels.append(channel)
                    arb_ids.append(arb_id)
                    dlcs.append(len(payload))
                    directions.append(0)
                    flags.append(0)
                    offset = slot * 64
                    data_block[offset:offset + len(payload)] = payload
            yield (
                1000.0, timestamps, channels, arb_ids, dlcs,
                directions, flags, data_block,
            )

    worker = LoadWorker(
        "unused.asc", ChannelConfig.from_single_dbc(str(sample_dbc_path))
    )
    store = SignalStore()
    progress = []
    trees = []
    partials = []
    worker.progress.connect(progress.append)
    worker.tree_update.connect(trees.append)
    worker.partial_ready.connect(lambda: partials.append(True))

    worker._run_can_raw_vectorized(PackedReader(), store)
    try:
        assert len(trees) == 1
        assert len(partials) == 1
        assert len(store._series_by_key) == 51
        assert store.total_samples == 51
        assert len(store.raw_frame_store) == 51
        assert not any(message.startswith("Decoded ") for message in progress)
        assert not any("pass 1/2" in message or "pass 2/2" in message
                       for message in progress)
        assert any(message.startswith("Bulk vectorized decode/import complete:")
                   for message in progress)
    finally:
        store.raw_frame_store.close()


def test_raw_can_csv_bulk_decodes_and_populates_trace(
    raw_can_csv_path, sample_dbc_path,
):
    from core.channel_config import ChannelConfig
    from core.load_worker import LoadWorker
    from core.readers import reader_factory
    from core.signal_store import SignalStore

    config = ChannelConfig.from_single_dbc(str(sample_dbc_path))
    reader = reader_factory(str(raw_can_csv_path), str(sample_dbc_path))
    worker = LoadWorker(str(raw_can_csv_path), config)
    store = SignalStore()

    worker._run_can_raw_vectorized(reader, store)
    try:
        assert len(store._series_by_key) == 3
        assert store.total_samples == 3
        assert store.total_frames == 3
        assert store.decoded_frames == 2
        assert store.unmatched_frames == 1
        assert len(store.raw_frame_store) == 3
        records = store.raw_frame_store.get_window([0, 1, 2])
        assert [record.arbitration_id for record in records] == [
            0x100, 0x200, 0x18FF50E5,
        ]
        assert records[0].decoded is True
        assert records[2].is_fd is True
    finally:
        store.raw_frame_store.close()
