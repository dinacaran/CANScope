# PR Policy
 This document This document meant to communicate The files and structures that are protected, Basic functionality and feature of this tool is not altered.

## What NOT to Modify Without permission:
Protected loading/decoding files and areas:

- `core/load_worker.py`
- `core/channel_config.py`
- `core/dbc_decoder.py`
- `core/vectorized_decoder.py`
- `core/raw_frame_store.py`
- `core/blf_reader.py`
- `core/readers/__init__.py`
- `core/readers/base.py`
- `core/readers/db_format.py`
- `core/readers/blf_can_reader.py`
- `core/readers/asc_can_reader.py`
- `core/readers/mdf_reader.py`
- `core/readers/mdf_can_reader.py`
- The **Load + Decode**, pre-scan, reader selection, worker wiring, progress,
  partial-update, completion, signal-tree handoff, and CAN Trace behavior in
  `gui/main_window.py`
- Loading/decoding dependency versions in `requirements.txt`

Protected behavior includes format detection, channel numbering, timestamp
normalization, DBC/ARXML selection, native asammdf extraction, ASC direct-array
parsing, BLF batched extraction, vectorized decoding, multiplexed-signal
filtering, bulk SignalStore insertion, RawFrameStore/CAN Trace construction,
fallback paths, decoded signal/sample counts, and loading progress messages.

Read `docs/Project_structure.md` for the accepted loading/decoding architecture.

## What NOT to Modify Without Owner's permission:
- `CANScope.spec` — PyInstaller build spec; modifying breaks the portable build.
- The `APP_NAME` constant in `app.py` — affects window title and branding.