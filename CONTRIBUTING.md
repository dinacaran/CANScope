# Contributing to CAN Scope

Thank you for taking the time to contribute.

---

## Before You Open a Pull Request

1. **Check existing issues** — your bug or feature may already be tracked.
2. **Open an issue first** for any non-trivial change so we can discuss the
   approach before you invest time writing code.
3. For small fixes (typos, one-line bugs) you can submit a PR directly.

---

## Development Setup

```bash
git clone https://github.com/dinacaran/canscope.git
cd canscope
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
python app.py
```

Optional — for MF4/MDF support:
```bash
pip install asammdf>=7.0
```

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
│   │   ├── mdf_reader.py
│   │   └── csv_reader.py
│   ├── blf_reader.py         # RawFrame dataclass + BLFReaderService
│   ├── dbc_decoder.py        # DBCDecoder + DecodedSignalSample
│   ├── signal_store.py       # SignalStore + SignalSeries
│   ├── load_worker.py        # QThread worker
│   └── export.py             # CSV export
└── gui/
    ├── main_window.py        # MainWindow (QMainWindow)
    ├── plot_widget.py        # PlotPanel (pyqtgraph)
    ├── signal_tree.py        # SignalTreeWidget
    └── raw_frame_dialog.py   # RawFrameDialog
```

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

Version format: `vXX.YY.ZZ` (e.g. `v00.00.02`).

- **ZZ** — bug fixes and minor tweaks (increment on every PR merged to main)
- **YY** — new features or format support added
- **XX** — breaking changes or major redesigns

Update `APP_VERSION` in `app.py` with every change. Add a matching entry to
`CHANGELOG.md`.

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
