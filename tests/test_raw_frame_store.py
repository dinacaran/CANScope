"""Tests for core/raw_frame_store.py — append, seal, get_window, match_mask."""
from __future__ import annotations

import numpy as np
import pytest

from core.raw_frame_store import RawFrameStore


def _populated_store(n: int = 3, sealed: bool = False) -> RawFrameStore:
    store = RawFrameStore()
    for i in range(n):
        store.append(
            timestamp=float(i) * 0.001,
            channel=1,
            arb_id=0x100 + i,
            dlc=8,
            direction="Rx",
            is_extended=False,
            is_fd=False,
            data=bytes([i] * 8),
            frame_name=f"Msg{i}",
            decoded=(i == 0),
        )
    if sealed:
        store.seal()
    return store


# ── append / __len__ ─────────────────────────────────────────────────────

def test_append_increments_len():
    store = _populated_store(3)
    assert len(store) == 3


def test_append_stores_timestamp():
    store = _populated_store(1)
    assert store.timestamps[0] == pytest.approx(0.0)


def test_append_stores_arb_id():
    store = _populated_store(1)
    assert store.arb_ids[0] == 0x100


def test_append_stores_channel():
    store = _populated_store(1)
    assert store.channels[0] == 1


def test_append_none_channel_stored_as_255():
    store = RawFrameStore()
    store.append(
        timestamp=0.0, channel=None, arb_id=0x100, dlc=8,
        direction="Rx", is_extended=False, is_fd=False,
        data=bytes(8), frame_name="", decoded=False,
    )
    assert store.channels[0] == 255


# ── seal / get_window ─────────────────────────────────────────────────────

def test_get_window_returns_records():
    store = _populated_store(3, sealed=True)
    records = store.get_window([0, 1, 2])
    assert len(records) == 3


def test_get_window_record_timestamp():
    store = _populated_store(3, sealed=True)
    rec = store.get_window([0])[0]
    assert rec.time_s == pytest.approx(0.0)


def test_get_window_record_arb_id():
    store = _populated_store(3, sealed=True)
    rec = store.get_window([0])[0]
    assert rec.arbitration_id == 0x100


def test_get_window_record_channel():
    store = _populated_store(3, sealed=True)
    rec = store.get_window([0])[0]
    assert rec.channel == 1


def test_get_window_decoded_flag():
    store = _populated_store(3, sealed=True)
    # Frame 0 was appended with decoded=True
    rec0 = store.get_window([0])[0]
    rec1 = store.get_window([1])[0]
    assert rec0.decoded is True
    assert rec1.decoded is False


def test_get_window_out_of_range_skipped():
    store = _populated_store(3, sealed=True)
    records = store.get_window([99])
    assert records == []


def test_get_window_data_bytes():
    store = _populated_store(1, sealed=True)
    rec = store.get_window([0])[0]
    # Frame 0 data = bytes([0]*8); get up to dlc=8 bytes
    assert rec.data[:1] == bytes([0])


# ── append_raw ────────────────────────────────────────────────────────────

def test_append_raw_increments_len():
    store = RawFrameStore()
    store.append_raw(0.0, 1, 0x100, 8, 0, False, False, bytes(8))
    assert len(store) == 1


def test_append_raw_name_always_empty():
    store = RawFrameStore()
    store.append_raw(0.0, 1, 0x100, 8, 0, False, False, bytes(8))
    store.seal()
    rec = store.get_window([0])[0]
    assert rec.frame_name == ""


def test_append_raw_batch_preserves_metadata_and_payload():
    store = RawFrameStore()
    data = bytearray(2 * 64)
    data[0:3] = b"\x01\x02\x03"
    data[64:66] = b"\xAA\xBB"
    store.append_raw_batch(
        timestamps=[0.0, 0.1],
        channels=[1, 2],
        arb_ids=[0x123, 0x18FEF100],
        dlcs=[3, 2],
        directions=[0, 1],
        flags=[0, 3],
        data_block=data,
    )
    store.seal()

    first, second = store.get_window([0, 1])
    assert first.data == b"\x01\x02\x03"
    assert first.direction == "Rx"
    assert second.data == b"\xAA\xBB"
    assert second.channel == 2
    assert second.direction == "Tx"
    assert second.is_extended is True
    assert second.is_fd is True


def test_append_numpy_batch_preserves_columns_and_pads_payload():
    store = RawFrameStore()
    try:
        store.append_numpy_batch(
            timestamps=np.array([2.0, 2.1], dtype=np.float64),
            channels=np.array([1, 2], dtype=np.uint8),
            arb_ids=np.array([0x123, 0x18FEF100], dtype=np.uint32),
            dlcs=np.array([3, 2], dtype=np.uint8),
            directions=np.array([0, 1], dtype=np.uint8),
            flags=np.array([0, 3], dtype=np.uint8),
            data_rows=np.array([
                [1, 2, 3, 0, 0, 0, 0, 0],
                [0xAA, 0xBB, 0, 0, 0, 0, 0, 0],
            ], dtype=np.uint8),
        )
        store.seal()

        first, second = store.get_window([0, 1])
        assert first.time_s == pytest.approx(2.0)
        assert first.data == b"\x01\x02\x03"
        assert second.channel == 2
        assert second.data == b"\xAA\xBB"
        assert second.direction == "Tx"
        assert second.is_extended is True
        assert second.is_fd is True
    finally:
        store.close()


# ── build_match_mask ──────────────────────────────────────────────────────

def test_match_mask_no_filter_returns_none():
    store = _populated_store(3, sealed=True)
    mask = store.build_match_mask("", None)
    assert mask is None


def test_match_mask_channel_filter():
    store = RawFrameStore()
    store.append(0.0, 1, 0x100, 8, "Rx", False, False, bytes(8), "", False)
    store.append(0.001, 2, 0x200, 8, "Rx", False, False, bytes(8), "", False)
    store.seal()
    mask = store.build_match_mask("", channel_filter=1)
    assert mask is not None
    assert mask[0] is np.bool_(True)
    assert mask[1] is np.bool_(False)


def test_match_mask_empty_store():
    store = RawFrameStore()
    store.seal()
    mask = store.build_match_mask("rx", None)
    assert len(mask) == 0


# ── close / cleanup ───────────────────────────────────────────────────────

def test_close_removes_temp_file():
    store = _populated_store(1, sealed=True)
    path = store._data_path
    store.close()
    import os
    assert not os.path.exists(path)


def test_close_idempotent():
    store = _populated_store(1, sealed=True)
    store.close()
    store.close()   # second close must not raise
