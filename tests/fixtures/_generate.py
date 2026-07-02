"""
Generate sample.blf and sample.asc for the CANScope test suite.

Run once after cloning, or whenever sample.dbc changes:

    python tests/fixtures/_generate.py

Output files are intentionally excluded from git (.gitignore: *.blf, *.asc).
Each file contains three repeated bursts of CAN frames that match sample.dbc:

  0x100  EngineControl  EngSpeed=1200.0 rpm, Throttle=50.0 %
  0x200  GearStatus     Gear=4 (Drive)
  0x300  DiagRequest    8 bytes, no signals (exercises decoded_no_signals path)
"""
from __future__ import annotations

import struct
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent


def _eng_control_payload() -> bytes:
    # EngSpeed raw = 1200.0 / 0.5 = 2400 = 0x0960, little-endian at bits 0-15
    # Throttle raw = 50.0 / 0.5 = 100 = 0x64, at bits 16-23
    return bytes([0x60, 0x09, 0x64, 0x00, 0x00, 0x00, 0x00, 0x00])


def _gear_status_payload() -> bytes:
    # Gear raw = 4 (Drive), little-endian at bits 0-7
    return bytes([0x04, 0x00, 0x00, 0x00])


def _diag_request_payload() -> bytes:
    return bytes(8)


def _frames():
    """Yield (timestamp_s, arb_id, data) tuples for one burst."""
    burst = [
        (0.001, 0x100, _eng_control_payload()),
        (0.002, 0x200, _gear_status_payload()),
        (0.003, 0x300, _diag_request_payload()),
    ]
    for offset in (0.0, 0.010, 0.020):
        for ts, arb_id, data in burst:
            yield ts + offset, arb_id, data


def generate_blf(out_path: Path) -> None:
    import can

    start_time = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
    messages = [
        can.Message(
            timestamp=ts,
            arbitration_id=arb_id,
            data=data,
            is_extended_id=False,
            channel=1,
        )
        for ts, arb_id, data in _frames()
    ]

    with can.BLFWriter(str(out_path), channel=1) as writer:
        writer.start_timestamp = start_time.timestamp()
        for msg in messages:
            writer(msg)

    print(f"Written: {out_path} ({len(messages)} frames)")


def generate_asc(out_path: Path) -> None:
    import can

    start_time = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
    messages = [
        can.Message(
            timestamp=ts,
            arbitration_id=arb_id,
            data=data,
            is_extended_id=False,
            channel=1,
        )
        for ts, arb_id, data in _frames()
    ]

    with can.ASCWriter(str(out_path)) as writer:
        writer.start_timestamp = start_time.timestamp()
        for msg in messages:
            writer(msg)

    print(f"Written: {out_path} ({len(messages)} frames)")


if __name__ == "__main__":
    generate_blf(HERE / "sample.blf")
    generate_asc(HERE / "sample.asc")
