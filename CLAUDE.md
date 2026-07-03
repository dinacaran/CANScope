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

---
## What NOT to Modify Without Instruction

- `core/signal_store.py` — the data source for the entire app; read-only from diagnostics code.
- `CANScope.spec` — PyInstaller build spec; modifying breaks the portable build.
- The `APP_NAME` constant in `app.py` — affects window title and branding.
- The LLM model list in `core/diagnostics/llm/client.py` — tied to GitHub Models availability.
