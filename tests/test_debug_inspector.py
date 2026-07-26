from __future__ import annotations

import struct
import time

import can

from core.debug_inspector import (
    _block_header,
    _inspect_mdf4_hd_dg_cg,
    format_runtime_failure,
    inspect_databases,
    inspect_measurement,
)


def test_direct_block_probe_reports_header_and_links(tmp_path):
    path = tmp_path / "block.mf4"
    payload = bytearray(160)
    payload[64:96] = struct.pack("<4s4sQQQ", b"##SD", b"\0" * 4, 32, 1, 0)
    path.write_bytes(payload)

    header = _block_header(path, 64)

    assert header["block_id"] == "##SD"
    assert header["block_len"] == 32
    assert header["links_nr"] == 1
    assert header["complete"] is True
    assert header["aligned"] is True
    assert header["links"] == [0]


def test_raw_mdf4_probe_identifies_exact_out_of_file_hd_pointer(tmp_path):
    path = tmp_path / "bad-first-dg.mf4"
    writer = can.MF4Writer(str(path))
    try:
        writer.on_message_received(
            can.Message(
                timestamp=time.time(),
                arbitration_id=0x123,
                data=bytes(range(8)),
                channel=1,
            )
        )
    finally:
        writer.stop()

    data = bytearray(path.read_bytes())
    bad_address = len(data) + 0x800
    data[88:96] = struct.pack("<Q", bad_address)
    path.write_bytes(data)

    report = inspect_measurement(path, app_version="test")

    assert "CLASSIFICATION: MDF-RAW-BLOCK-LINK" in report
    assert "DBC INVOLVED: NO - file structure failed before DBC decoding" in report
    assert "RAW MDF4 ##HD -> ##DG -> ##CG LINK VALIDATION" in report
    assert (
        f"##HD.first_dg: source=0x40 link[0] target=0x{bad_address:X}"
        in report
    )
    assert "EXACT CORRUPT POINTER" in report
    assert "header outside file" in report


def test_raw_mdf4_probe_prints_valid_hd_dg_cg_chain(tmp_path):
    path = tmp_path / "valid-chain.mf4"
    writer = can.MF4Writer(str(path))
    try:
        writer.on_message_received(
            can.Message(
                timestamp=time.time(),
                arbitration_id=0x456,
                data=b"\x01\x02",
                channel=2,
            )
        )
    finally:
        writer.stop()

    report = inspect_measurement(path, app_version="test")

    assert "PASS Raw MDF4 HD-DG-CG links" in report
    assert "##HD.first_dg: source=0x40 link[0]" in report
    assert "##DG.first_cg:" in report
    assert "##CG.next_cg:" in report
    assert "Raw chain totals: data_groups=" in report


def test_raw_mdf4_probe_identifies_exact_bad_dg_first_cg_pointer(tmp_path):
    path = tmp_path / "bad-first-cg.mf4"
    writer = can.MF4Writer(str(path))
    try:
        writer.on_message_received(
            can.Message(
                timestamp=time.time(),
                arbitration_id=0x321,
                data=b"\x01",
                channel=1,
            )
        )
    finally:
        writer.stop()

    data = bytearray(path.read_bytes())
    first_dg = int(_block_header(path, 64)["links"][0])
    bad_address = len(data) + 0x1000
    data[first_dg + 32:first_dg + 40] = struct.pack("<Q", bad_address)
    path.write_bytes(data)

    lines, status, detail = _inspect_mdf4_hd_dg_cg(path)
    report = "\n".join(lines)

    assert status == "FAIL"
    assert (
        f"##DG.first_cg: source=0x{first_dg:X} "
        f"link[1] target=0x{bad_address:X}"
    ) in detail
    assert "EXACT CORRUPT POINTER" in report
    assert "header outside file" in detail


def test_raw_mdf4_probe_identifies_exact_bad_cg_next_pointer(tmp_path):
    path = tmp_path / "bad-next-cg.mf4"
    writer = can.MF4Writer(str(path))
    try:
        writer.on_message_received(
            can.Message(
                timestamp=time.time(),
                arbitration_id=0x654,
                data=b"\x02",
                channel=1,
            )
        )
    finally:
        writer.stop()

    data = bytearray(path.read_bytes())
    first_dg = int(_block_header(path, 64)["links"][0])
    first_cg = int(_block_header(path, first_dg)["links"][1])
    bad_address = len(data) + 0x1800
    data[first_cg + 24:first_cg + 32] = struct.pack("<Q", bad_address)
    path.write_bytes(data)

    lines, status, detail = _inspect_mdf4_hd_dg_cg(path)
    report = "\n".join(lines)

    assert status == "FAIL"
    assert (
        f"##CG.next_cg: source=0x{first_cg:X} "
        f"link[0] target=0x{bad_address:X}"
    ) in detail
    assert "EXACT CORRUPT POINTER" in report
    assert "header outside file" in detail


def test_known_good_fixed_width_mf4_has_no_false_vlsd_failure(tmp_path):
    path = tmp_path / "known_good.mf4"
    writer = can.MF4Writer(str(path))
    try:
        writer.on_message_received(
            can.Message(
                timestamp=time.time(),
                arbitration_id=0x123,
                data=bytes(range(8)),
                channel=1,
                is_rx=True,
            )
        )
    finally:
        writer.stop()

    report = inspect_measurement(path, app_version="test")

    assert "STATUS: PASS" in report
    assert "STORAGE fixed-width in the channel-group record" in report
    assert "vlsd_offsets=N/A" in report
    assert "PASS python-can MF4Reader" in report


def test_non_mdf_debug_report_is_in_memory_plain_text(tmp_path):
    path = tmp_path / "signals.csv"
    path.write_text("time,value\n0,1\n", encoding="utf-8")

    before = set(tmp_path.iterdir())
    report = inspect_measurement(path, app_version="test")
    after = set(tmp_path.iterdir())

    assert before == after
    assert "STATUS: WARN" in report
    assert "Deep binary inspection currently targets MDF/MF4" in report
    assert str(tmp_path) not in report


def test_database_report_matches_observed_ids_and_redacts_path(
    sample_dbc_path,
):
    report = inspect_databases(
        {0: str(sample_dbc_path)},
        {1: {0x100, 0x999}},
    )

    assert "DATABASE: sample.dbc" in report
    assert "Observed IDs: 2 | Matched: 1 | Unmatched: 1" in report
    assert "Unmatched IDs: 0x999" in report
    assert str(sample_dbc_path.parent) not in report


def test_runtime_failure_classifies_vlsd_and_redacts_full_path(tmp_path):
    path = tmp_path / "private" / "issue.mf4"
    error = (
        f"Error reading MDF bus log '{path}': "
        'Wrong signal data block refence (0x3317FB0) '
        'for VLSD channel "CAN_DataFrame.DataBytes"'
    )

    report = format_runtime_failure(error, path)

    assert "CLASSIFICATION: MDF-VLSD-PAYLOAD" in report
    assert "DBC INVOLVED: NO" in report
    assert "0x3317FB0" in report
    assert "CAN_DataFrame.DataBytes" in report
    assert str(path.parent) not in report
