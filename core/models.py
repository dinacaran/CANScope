"""
Lightweight dataclasses shared across the CAN Scope core.

This module has NO heavy dependencies (no cantools, no python-can, no numpy,
no asammdf) so it can be imported at startup without triggering those slow
module-level imports.  All reader/decoder modules import from here instead
of from each other to keep the import graph flat.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RawFrame:
    """One raw CAN frame as read from a BLF or ASC file."""
    timestamp:       float
    channel:         int | None
    arbitration_id:  int
    is_extended_id:  bool
    is_fd:           bool
    dlc:             int
    data:            bytes
    direction:       str   # 'Rx' | 'Tx' | 'Unknown'


@dataclass(slots=True)
class DecodedSignalSample:
    """One physical-value sample for a single signal at a single timestamp."""
    timestamp:      float
    channel:        int | None
    message_id:     int
    message_name:   str
    signal_name:    str
    value:          Any          # display value (str label or float)
    unit:           str
    is_extended_id: bool
    direction:      str
    numeric_value:  float        # always a float — NaN when unavailable
