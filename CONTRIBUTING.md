# Contributing to CAN Scope

Thank you for taking the time to contribute.

---

## Before You Open a Pull Request

1. **Check existing issues** — your bug or feature may already be tracked.
2. **Open an issue first** for any non-trivial change so we can discuss the
   approach before you invest time writing code.
3. For small fixes (typos, one-line bugs) you can submit a PR directly.
4. **Check the protected areas below** — if your change touches one, discuss it
   in an issue first. CI will block the PR otherwise.

---

## Protected Areas

The BLF, ASC, MF4, and MDF loading and decoding pipeline has been validated
against real measurement data and accepted by the project owner. Changes there
risk silently corrupting decoded values in ways tests do not catch, so it is
off-limits without prior agreement — **including** cleanups, refactors,
performance work, and dependency bumps.

| Path | Why |
|------|-----|
| `core/load_worker.py`, `core/channel_config.py` | Load orchestration and channel numbering |
| `core/dbc_decoder.py`, `core/vectorized_decoder.py` | Signal decoding |
| `core/blf_reader.py`, `core/raw_frame_store.py` | Raw frame handling |
| `core/readers/` | Format detection and all reader implementations |
| `core/signal_store.py` | Data source for the entire app |
| `requirements.txt` | Loading/decoding dependency versions |
| `CANScope.spec` | PyInstaller build spec — changes break the portable build |

The full policy is in /docs/PR_policy.md. It is enforced automatically by
`tools/check_protected_paths.py`, which runs on every PR. You can check your
own branch before pushing:

```bash
python tools/check_protected_paths.py
```

If a change to one of these files is genuinely necessary and has been agreed in
an issue, the owner applies the `approved-pipeline-change` label to the PR to
release the check.

---

## Development Setup

```bash
git clone https://github.com/dinacaran/canscope.git
cd canscope
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt -r requirements-dev.txt
python tests/install_hooks.py   # runs the test suite before every commit
python app.py
```

Optional — for MF4/MDF support:
```bash
pip install asammdf>=7.0
```

Run the tests any time with:
```bash
python -m pytest tests/
```

The same suite runs on every pull request, along with a full PyInstaller build,
so a PR that breaks either will not merge.

---

## Project Layout

```
canscope/
├── app.py                    # Entry point, APP_NAME, APP_VERSION
├── core/
│   ├── readers/              # One reader class per format
│   │   ├── __init__.py       # reader_factory() + format registry
│   │   ├── base.py           # MeasurementReader protocol
│   │   ├── blf_can_reader.py
│   │   ├── asc_can_reader.py
│   │   ├── mdf_reader.py / mdf_can_reader.py
│   │   ├── db_format.py
│   │   └── csv_reader.py
│   ├── blf_reader.py         # RawFrame dataclass + BLFReaderService
│   ├── dbc_decoder.py        # DBCDecoder + DecodedSignalSample
│   ├── vectorized_decoder.py # Bulk numpy decode path
│   ├── raw_frame_store.py    # RawFrameStore (CAN Trace)
│   ├── signal_store.py       # SignalStore + SignalSeries
│   ├── load_worker.py        # QThread worker
│   ├── calculated_signals.py # User-defined derived signals
│   ├── export.py             # CSV export
│   └── diagnostics/          # AI diagnostics engine
│       ├── config_loader.py  # YAML rule loading
│       ├── engine.py         # Rule evaluation
│       ├── rules/            # One module per rule type
│       ├── agent/            # Agent loop, knowledge, prompts
│       └── llm/              # GitHub Models client + token store
├── gui/
│   ├── main_window.py        # MainWindow (QMainWindow)
│   ├── plot_widget.py        # PlotPanel (pyqtgraph)
│   ├── signal_tree.py        # SignalTreeWidget
│   ├── dbc_manager.py        # DBC/ARXML database management
│   ├── raw_frame_dialog.py   # RawFrameDialog
│   └── diagnostics/          # Diagnostics window, panels, worker
├── config/diagnostics/       # YAML fault rules (no Python needed)
├── tools/                    # Repo tooling (protected-path check)
└── tests/                    # pytest suite + fixtures
```

Deeper detail lives in [docs/Project_structure.md](docs/Project_structure.md)
and [docs/AI_diagnostic.md](docs/AI_diagnostic.md).

---

## Adding a New File Format

1. Create `core/readers/myformat_reader.py` implementing the
   `MeasurementReader` protocol (see `core/readers/base.py`).
2. Register the new extension in `core/readers/__init__.py` inside
   `reader_factory()`.
3. Set `has_raw_frames = False` unless your format yields raw CAN bytes.
4. No changes to `gui/` are needed — the GUI is fully format-agnostic.

---

## Code Style

- Python 3.11+, type-annotated throughout.
- `from __future__ import annotations` at the top of every module.
- `dataclass(slots=True)` for data-holding classes.
- No external formatters are enforced, but please follow the existing style.

---

## Versioning

> **Contributors: do not modify `APP_VERSION` in `app.py` or `CHANGELOG.md`.**
> The project owner updates both at release time. PRs that touch them are
> blocked by CI.

This is deliberate. If every PR bumped the version, two open PRs would always
conflict on the same two lines, and the merge order would decide the version
number. Leaving it to the owner keeps releases coherent.

For reference, the version format is `vXX.YY.ZZ` (e.g. `v00.00.02`):

- **ZZ** — bug fixes and minor tweaks
- **YY** — new features or format support added
- **XX** — breaking changes or major redesigns

Describe your change in the PR body instead; the owner uses that to write the
changelog entry.

---

## Commit Messages

Use the conventional prefix style:

| Prefix | When to use |
|--------|-------------|
| `fix:` | Bug fix |
| `feat:` | New feature |
| `docs:` | Documentation only |
| `ci:` | Workflow / build changes |
| `refactor:` | Code restructure without behaviour change |
| `chore:` | Dependency bumps, version bumps |

Example: `fix: hide C1/C2 stacked labels when cursor is disabled`

---

## Reporting Bugs

Open a GitHub issue and include:

- OS and Python version
- Measurement file format (BLF / ASC / MF4 / CSV)
- Steps to reproduce
- Full error message or traceback
- No proprietary measurement data required — a minimal description is enough

---

## Code of Conduct

Be constructive and respectful. Feedback on code is not feedback on the person.
