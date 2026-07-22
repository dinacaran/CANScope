# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
---
#what to update in CHANGELOG.md
only update summary of update. no need to add code level details.
---
for Project structure read file: docs/Project_structure.md
---
for Diagnostics Feature Architecture read file: docs/AI_diagnostic.md
---
for next step refer: TODO.md
---
for commands refer to file: docs/commands.md
---
for Pull Request Policy: docs/PR_Policy.md

## Project Overview

**CANScope** (v00.00.45) is a Windows desktop tool built with Python 3.12 + PySide6.  
It loads automotive CAN measurement files (BLF, ASC, MF4, MDF, CSV), decodes signals via DBC or ARXML (AUTOSAR) databases, and plots them interactively.

An **AI-powered diagnostics feature** (`core/diagnostics/`, `gui/diagnostics/`) sits on top:  
- YAML-based fault rules — no Python required for end users  
- Three rule types: value fault (`fault_when`), range violation (`min`/`max`), message loss (`max_gap_s`)  
- LLM root-cause analysis via GitHub Models API (Ctrl+Shift+A hidden shortcut)

---
## What NOT to Modify Without permission
App Version update and CHANGELOG.md

### Validated loading and decoding pipeline

The BLF, ASC, MF4, and MDF loading/decoding behavior has been fully tested and
accepted by the project owner. **Do not modify, refactor, replace, optimize, or
otherwise change this pipeline without explicit permission from the project
owner.** This restriction applies even when a proposed change appears to be a
cleanup, performance improvement, dependency upgrade, or bug fix.

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
---
## What NOT to Modify Without Instruction

- `core/signal_store.py` — the data source for the entire app; read-only from diagnostics code.
- `CANScope.spec` — PyInstaller build spec; modifying breaks the portable build.
- The `APP_NAME` constant in `app.py` — affects window title and branding.
- The LLM model list in `core/diagnostics/llm/client.py` — tied to GitHub Models availability.
