from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np

from core.readers.mdf_can_reader import MDFCANReader
from core.readers.mdf_reader import LazyTextValues, MDFReader
from core.load_worker import LoadWorker
from core.signal_store import SignalStore


class _FakeMF4Reader:
    def __init__(self, messages):
        self._messages = messages

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        return iter(self._messages)


class _Decoder:
    dbc_path = Path("test.dbc")
    load_messages = []

    def __init__(self):
        self.calls = 0

    def decode_frame(self, _frame):
        self.calls += 1
        return []


def test_mf4_raw_tuple_path_skips_per_frame_dbc_decode(tmp_path, monkeypatch):
    mf4 = tmp_path / "bus.mf4"
    mf4.write_bytes(b"test")
    msg = SimpleNamespace(
        timestamp=12.5,
        channel=2,
        arbitration_id=0x18FEF100,
        dlc=8,
        data=bytearray(range(8)),
        is_rx=True,
        is_extended_id=True,
        is_fd=False,
    )
    decoder = _Decoder()
    monkeypatch.setattr(
        "core.readers.mdf_can_reader.can.MF4Reader",
        lambda _path: _FakeMF4Reader([msg]),
    )

    reader = MDFCANReader(mf4, decoder)
    rows = list(reader.iter_raw_tuples())

    assert rows == [(12.5, 2, 0x18FEF100, 8, 0, True, False, bytes(range(8)))]
    assert decoder.calls == 0
    assert hasattr(reader, "iter_frames_only")


def test_mf4_legacy_iterator_still_decodes(tmp_path, monkeypatch):
    mf4 = tmp_path / "bus.mf4"
    mf4.write_bytes(b"test")
    msg = SimpleNamespace(
        timestamp=1.0,
        channel=None,
        arbitration_id=0x123,
        data=b"\x01",
        is_rx=False,
        is_extended_id=False,
        is_fd=False,
    )
    decoder = _Decoder()
    monkeypatch.setattr(
        "core.readers.mdf_can_reader.can.MF4Reader",
        lambda _path: _FakeMF4Reader([msg]),
    )

    frame, samples = next(iter(MDFCANReader(mf4, decoder).iter_with_frames()))

    assert frame.channel is None
    assert frame.direction == "Tx"
    assert frame.dlc == 1
    assert samples == []
    assert decoder.calls == 1


def test_asammdf_metadata_preserves_channel_message_and_id():
    channel = SimpleNamespace(
        display_names={"CAN2.VehicleStatus.VehicleSpeed": "bus"}
    )
    group = SimpleNamespace(
        channel_group=SimpleNamespace(
            acq_source=SimpleNamespace(
                path="CAN2.CAN_DataFrame.ID=0x18FEF100 EXT=True"
            ),
            acq_name="CAN2 message ID=0x18FEF100 EXT=True",
        ),
        channels=[SimpleNamespace(), channel],
    )
    extracted = SimpleNamespace(
        groups=[group],
        channels_db={"VehicleSpeed": [(0, 1)]},
    )

    result = MDFCANReader._decoded_group_metadata(
        extracted, 0, "VehicleSpeed", channel_config=None
    )

    assert result == (2, "VehicleStatus", 0x18FEF100)


def test_lazy_text_values_preserve_display_labels_without_eager_list():
    labels = LazyTextValues(np.array([b"Off ", b"On"], dtype="S4"))

    assert bool(labels)
    assert labels[0] == "Off"
    assert labels[-1] == "On"
    assert list(labels) == ["Off", "On"]
    assert np.array(labels, dtype=object).tolist() == ["Off", "On"]


def test_extracted_bus_arrays_use_two_global_selects():
    numeric = SimpleNamespace(
        timestamps=np.array([10.0, 10.1]),
        samples=np.array([1.5, 2.5]),
        unit="V",
    )
    enum = SimpleNamespace(
        timestamps=np.array([10.0, 10.1]),
        samples=np.array([b"Off", b"On"], dtype="S3"),
        unit="",
    )
    enum_raw = SimpleNamespace(samples=np.array([0, 1], dtype=np.uint8))
    channels = [
        SimpleNamespace(name="Voltage", channel_type=0),
        SimpleNamespace(name="State", channel_type=0),
    ]
    select_calls = []

    class _Extracted:
        groups = [SimpleNamespace(
            channels=channels,
            channel_group=SimpleNamespace(acq_name="Status"),
        )]

        def select(self, specs, raw=False):
            select_calls.append((list(specs), raw))
            if raw:
                return [enum_raw]
            return [numeric, enum]

    rows = list(MDFReader._iter_arrays(
        _Extracted(), include_group_index=True, batch_all_groups=True
    ))

    assert len(select_calls) == 2
    assert select_calls[0][1] is False
    assert select_calls[1][1] is True
    assert len(rows) == 2
    assert rows[0][1] == ("Status", "Voltage", "V")
    assert rows[0][3].tolist() == [1.5, 2.5]
    assert rows[1][3].tolist() == [0.0, 1.0]
    assert list(rows[1][4]) == ["Off", "On"]


def test_signal_metadata_callback_precedes_sample_array_read(tmp_path, monkeypatch):
    events = []
    signal = SimpleNamespace(
        timestamps=np.array([4.0, 4.1]),
        samples=np.array([42.0, 43.0]),
        unit="km/h",
    )
    decoded_channel = SimpleNamespace(
        name="VehicleSpeed",
        channel_type=0,
        unit="km/h",
        display_names={"CAN1.VehicleStatus.VehicleSpeed": "bus"},
    )
    group = SimpleNamespace(
        channels=[decoded_channel],
        channel_group=SimpleNamespace(
            acq_source=SimpleNamespace(
                path="CAN1.CAN_DataFrame.ID=0x123 EXT=False"
            ),
            acq_name="CAN1 message ID=0x123 EXT=False",
        ),
    )

    class _Extracted:
        groups = [group]
        channels_db = {"VehicleSpeed": [(0, 0)]}

        def select(self, _specs, raw=False):
            assert raw is False
            events.append("select")
            return [signal]

        def close(self):
            pass

    class _Source:
        _mdf = SimpleNamespace(bus_logging_map={"CAN": {1: object()}})

        def extract_bus_logging(self, **_kwargs):
            events.append("extract")
            return _Extracted()

        def close(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "asammdf",
        SimpleNamespace(MDF=lambda *_args, **_kwargs: _Source()),
    )
    mf4 = tmp_path / "bus.mf4"
    mf4.write_bytes(b"test")
    reader = MDFCANReader(mf4, tmp_path / "test.dbc")

    rows = list(reader.iter_decoded_channel_arrays(
        None,
        metadata_ready=lambda metadata: events.append(("metadata", metadata)),
    ))

    assert events[0] == "extract"
    assert events[1][0] == "metadata"
    assert events[1][1] == [
        (1, "VehicleStatus", 0x123, "VehicleSpeed", "km/h")
    ]
    assert events[2] == "select"
    assert rows[0][0] == (1, "VehicleStatus", 0x123, "VehicleSpeed", "km/h")


def test_mdf_composite_channel_is_emitted_as_one_raw_array_batch():
    samples = np.array(
        [
            (1, 0x123, 0, 3, [1, 2, 3, 0, 0, 0, 0, 0], 0, 0),
            (2, 0x18FEF100, 1, 8, [8, 7, 6, 5, 4, 3, 2, 1], 1, 1),
        ],
        dtype=[
            ("CAN_DataFrame.BusChannel", "u1"),
            ("CAN_DataFrame.ID", "<u4"),
            ("CAN_DataFrame.IDE", "u1"),
            ("CAN_DataFrame.DataLength", "u1"),
            ("CAN_DataFrame.DataBytes", "u1", (8,)),
            ("CAN_DataFrame.Dir", "u1"),
            ("CAN_DataFrame.EDL", "u1"),
        ],
    )
    group = SimpleNamespace(
        channels=[SimpleNamespace(name="Timestamp"), SimpleNamespace(name="CAN_DataFrame")]
    )
    source = SimpleNamespace(
        groups=[group],
        get=lambda **_kwargs: SimpleNamespace(
            samples=samples,
            timestamps=np.array([5.0, 5.1]),
        ),
    )
    batches = []

    count = MDFCANReader._emit_raw_frame_arrays(
        source,
        lambda *columns: batches.append(columns),
    )

    assert count == 2
    assert len(batches) == 1
    timestamps, channels, ids, dlcs, directions, flags, data = batches[0]
    assert timestamps.tolist() == [5.0, 5.1]
    assert channels.tolist() == [1, 2]
    assert ids.tolist() == [0x123, 0x18FEF100]
    assert dlcs.tolist() == [3, 8]
    assert directions.tolist() == [0, 1]
    assert flags.tolist() == [0, 3]
    assert data.shape == (2, 8)


def test_bus_mf4_native_arrays_populate_can_trace_in_bulk(tmp_path):
    metadata = [(1, "VehicleStatus", 0x123, "VehicleSpeed", "km/h")]

    class _Reader:
        source_description = "MF4 bus log + DBC"
        supports_raw_frame_arrays = True
        raw_trace_error = ""

        def iter_decoded_channel_arrays(
            self,
            _config,
            progress=None,
            metadata_ready=None,
            raw_frame_batch=None,
        ):
            metadata_ready(metadata)
            raw_frame_batch(
                np.array([9.5, 10.0]),
                np.array([1, 1], dtype=np.uint8),
                np.array([0x123, 0x456], dtype=np.uint32),
                np.array([2, 2], dtype=np.uint8),
                np.array([0, 1], dtype=np.uint8),
                np.array([0, 0], dtype=np.uint8),
                np.array([[0xAA, 0xBB], [0xCC, 0xDD]], dtype=np.uint8),
            )
            yield (
                metadata[0],
                np.array([10.0]),
                np.array([42.0]),
                [],
            )

    worker = LoadWorker(tmp_path / "bus.mf4")
    store = SignalStore()
    progress_messages = []
    worker.progress.connect(progress_messages.append)

    worker._run_mdf_bus_arrays(_Reader(), store)
    try:
        assert store.raw_frame_store is not None
        assert len(store.raw_frame_store) == 2
        first, second = store.raw_frame_store.get_window([0, 1])
        assert first.time_s == 0.0
        assert second.time_s == 0.5
        assert first.frame_name == "VehicleStatus"
        assert first.decoded is True
        assert second.decoded is False
        assert first.data == b"\xAA\xBB"
        assert store.total_frames == 2
        assert store.decoded_frames == 1
        assert store.unmatched_frames == 1
        assert any(message.startswith("CAN Trace ready: 2 raw frames")
                   for message in progress_messages)
    finally:
        store.raw_frame_store.close()


def test_tree_update_preserves_nested_integer_key_payload(tmp_path):
    worker = LoadWorker(tmp_path / "bus.mf4")
    payload = {1: {"VehicleStatus": ["VehicleSpeed"]}}
    received = []

    worker.tree_update.connect(received.append)
    worker.tree_update.emit(payload)

    assert received == [payload]


def test_bus_mf4_bulk_handoff_avoids_incremental_progress_and_duplicate_tree(
    tmp_path,
):
    metadata = [
        (1, "VehicleStatus", 0x123, "VehicleSpeed", "km/h"),
        (1, "VehicleStatus", 0x123, "DriveState", ""),
    ]

    class _Reader:
        source_description = "MF4 bus log + DBC"

        def iter_decoded_channel_arrays(
            self, _config, progress=None, metadata_ready=None
        ):
            metadata_ready(metadata)
            for meta in metadata:
                yield (
                    meta,
                    np.array([10.0, 10.1]),
                    np.array([1.0, 2.0]),
                    [],
                )

    worker = LoadWorker(tmp_path / "bus.mf4")
    store = SignalStore()
    progress_messages = []
    tree_payloads = []
    partial_updates = []
    worker.progress.connect(progress_messages.append)
    worker.tree_update.connect(tree_payloads.append)
    worker.partial_ready.connect(lambda: partial_updates.append(True))

    worker._run_mdf_bus_arrays(_Reader(), store)

    assert len(tree_payloads) == 1
    assert tree_payloads[0] == {
        1: {"VehicleStatus": ["VehicleSpeed", "DriveState"]}
    }
    assert len(partial_updates) == 1
    assert not any(message.startswith("Imported ") for message in progress_messages)
    assert any(message.startswith("Bulk import complete: 2 signals")
               for message in progress_messages)
    assert store.total_samples == 4


def test_bus_mf4_replaces_metadata_tree_when_an_array_is_empty(tmp_path):
    metadata = [
        (1, "VehicleStatus", 0x123, "VehicleSpeed", "km/h"),
        (1, "VehicleStatus", 0x123, "EmptySignal", ""),
    ]

    class _Reader:
        source_description = "MF4 bus log + DBC"

        def iter_decoded_channel_arrays(
            self, _config, progress=None, metadata_ready=None
        ):
            metadata_ready(metadata)
            yield (
                metadata[0],
                np.array([10.0]),
                np.array([1.0]),
                [],
            )
            yield metadata[1], np.array([]), np.array([]), []

    worker = LoadWorker(tmp_path / "bus.mf4")
    store = SignalStore()
    tree_payloads = []
    worker.tree_update.connect(tree_payloads.append)

    worker._run_mdf_bus_arrays(_Reader(), store)

    assert len(tree_payloads) == 2
    assert tree_payloads[-1] == {1: {"VehicleStatus": ["VehicleSpeed"]}}


def test_predecoded_mdf_publishes_metadata_before_one_global_array_batch(
    tmp_path, monkeypatch
):
    events = []
    numeric = SimpleNamespace(
        timestamps=np.array([0.0, 0.1]),
        samples=np.array([12.0, 13.0]),
        unit="V",
    )
    enum = SimpleNamespace(
        timestamps=np.array([0.0, 0.1]),
        samples=np.array([b"Off", b"On"], dtype="S3"),
        unit="",
    )
    enum_raw = SimpleNamespace(samples=np.array([0, 1], dtype=np.uint8))
    group = SimpleNamespace(
        channels=[
            SimpleNamespace(name="Voltage", channel_type=0, unit="V"),
            SimpleNamespace(name="State", channel_type=0, unit=""),
        ],
        channel_group=SimpleNamespace(acq_name="Status"),
    )

    class _MDF:
        groups = [group]

        def select(self, _specs, raw=False):
            events.append("raw-select" if raw else "engineering-select")
            return [enum_raw] if raw else [numeric, enum]

        def close(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "asammdf",
        SimpleNamespace(MDF=lambda *_args, **_kwargs: _MDF()),
    )
    path = tmp_path / "decoded.mf4"
    path.write_bytes(b"test")
    reader = MDFReader(path)

    rows = list(reader.iter_channel_arrays(
        metadata_ready=lambda metadata: events.append(("metadata", metadata)),
        batch_all_groups=True,
    ))

    assert events[0] == (
        "metadata",
        [("Status", "Voltage", "V"), ("Status", "State", "")],
    )
    assert events[1:] == ["engineering-select", "raw-select"]
    assert len(rows) == 2
    assert list(rows[1][3]) == ["Off", "On"]


def test_predecoded_mdf_avoids_incremental_tree_rebuild_queue(tmp_path):
    class _Reader:
        metadata_first_arrays = True
        source_description = "decoded MF4"

        def iter_channel_arrays(self, metadata_ready=None, batch_all_groups=False):
            assert batch_all_groups is True
            metadata = [("Status", f"Signal{i}", "") for i in range(25)]
            metadata_ready(metadata)
            for _group, name, unit in metadata:
                yield (
                    ("Status", name, unit),
                    np.array([0.0]),
                    np.array([1.0]),
                    [],
                )

    worker = LoadWorker(tmp_path / "decoded.mf4")
    store = SignalStore()
    tree_payloads = []
    partial_updates = []
    worker.tree_update.connect(tree_payloads.append)
    worker.partial_ready.connect(lambda: partial_updates.append(True))

    worker._run_bulk_array(_Reader(), store)

    assert len(tree_payloads) == 2  # metadata-first and final verified payload
    assert len(tree_payloads[0][None]["Status"]) == 25
    assert len(partial_updates) == 1
    assert len(store.all_keys()) == 25
