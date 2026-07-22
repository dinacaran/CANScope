"""Regression tests for fast Database Manager reopening after large loads."""
from __future__ import annotations

import array
import inspect
from types import SimpleNamespace
from unittest.mock import Mock


def _fake_main_window(*, measurement_path, prescan_cache, store):
    return SimpleNamespace(
        measurement_path=measurement_path,
        _prescan_cache=prescan_cache,
        _channel_data_cache=None,
        store=store,
    )


def test_normal_open_uses_prescan_without_touching_large_raw_store():
    from gui.main_window import MainWindow

    class LargeRawStore:
        def __len__(self):
            return 10_000_000

        @property
        def channels(self):
            raise AssertionError("normal Database Manager open scanned raw frames")

        @property
        def arb_ids(self):
            raise AssertionError("normal Database Manager open scanned raw frames")

    target = _fake_main_window(
        measurement_path="large.blf",
        prescan_cache=("large.blf", [1, 2], {1: {0x100}, 2: {0x200}}),
        store=SimpleNamespace(raw_frame_store=LargeRawStore(), channels={1, 2}),
    )

    channels, ids = MainWindow._collect_channel_data(target)

    assert channels == [1, 2]
    assert ids == {1: {0x100}, 2: {0x200}}


def test_config_loaded_open_without_prescan_does_not_scan_raw_store():
    from gui.main_window import MainWindow

    class LargeRawStore:
        def __len__(self):
            return 10_000_000

        @property
        def channels(self):
            raise AssertionError("normal Database Manager open scanned raw frames")

        @property
        def arb_ids(self):
            raise AssertionError("normal Database Manager open scanned raw frames")

    target = _fake_main_window(
        measurement_path="config-loaded.blf",
        prescan_cache=None,
        store=SimpleNamespace(raw_frame_store=LargeRawStore(), channels={1, 2}),
    )

    channels, ids = MainWindow._collect_channel_data(target)

    assert channels == [1, 2]
    assert ids == {}


def test_explicit_full_refresh_reduces_and_caches_raw_ids():
    from gui.main_window import MainWindow

    class RawStore:
        def __init__(self):
            self.channels = array.array("B", [1, 1, 2, 2, 255])
            self.arb_ids = array.array("I", [0x100, 0x100, 0x200, 0x201, 0x999])

        def __len__(self):
            return 5

    raw_store = RawStore()
    target = _fake_main_window(
        measurement_path="large.blf",
        prescan_cache=("large.blf", [1], {1: {0x100}}),
        store=SimpleNamespace(raw_frame_store=raw_store, channels={1, 2}),
    )

    channels, ids = MainWindow._collect_channel_data(target, full_scan=True)
    assert channels == [1, 2]
    assert ids == {1: {0x100}, 2: {0x200, 0x201}}

    # A second refresh must return the cached unique summary without reading
    # either raw metadata buffer again.
    raw_store.channels = object()
    raw_store.arb_ids = object()
    cached_channels, cached_ids = MainWindow._collect_channel_data(
        target, full_scan=True
    )
    assert cached_channels == channels
    assert cached_ids == ids


def test_native_mf4_keeps_prescan_ids_without_raw_store():
    from gui.main_window import MainWindow

    target = _fake_main_window(
        measurement_path="large.mf4",
        prescan_cache=("large.mf4", [1], {1: {0x123, 0x456}}),
        store=SimpleNamespace(raw_frame_store=None, channels={1, 9}),
    )

    channels, ids = MainWindow._collect_channel_data(target, full_scan=True)

    assert channels == [1, 9]
    assert ids == {1: {0x123, 0x456}}


def test_dialog_construction_does_not_auto_refresh_provider():
    from gui.dbc_manager import DBCManagerDialog

    init_source = inspect.getsource(DBCManagerDialog.__init__)
    assert "self._refresh_all_matches()" not in init_source


def test_database_match_metadata_is_cached_by_file_signature(
    monkeypatch, sample_dbc_path
):
    import cantools
    from gui.dbc_manager import _load_database_match_data

    _load_database_match_data.cache_clear()
    real_load = cantools.database.load_file
    load_spy = Mock(wraps=real_load)
    monkeypatch.setattr(cantools.database, "load_file", load_spy)

    path = sample_dbc_path.resolve()
    stat = path.stat()
    args = (str(path), stat.st_mtime_ns, stat.st_size)
    first = _load_database_match_data(*args)
    second = _load_database_match_data(*args)

    assert first == second
    assert load_spy.call_count == 1
