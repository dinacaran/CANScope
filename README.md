# CAN Scope

> A portable, zero-install Windows tool for loading CAN/automotive measurement files,
> decoding signals with DBC databases, and plotting them interactively.

![Python](https://img.shields.io/badge/Python-3.12%2B-blue?logo=python)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey?logo=windows)
![License](https://img.shields.io/badge/License-MIT-green)
![Release](https://img.shields.io/github/v/release/dinacaran/canscope)

---

## Supported Formats

| Format | Extension | DBC Required |
|--------|-----------|--------------|
| Vector Binary Logging Format | `.blf` | Yes |
| Vector CANalyzer ASCII Log | `.asc` | Yes |
| ASAM MDF4 — bus logging (raw CAN frames) | `.mf4` | Yes — auto-detected |
| ASAM MDF4 / MDF3 — pre-decoded signals | `.mf4` / `.mdf` | No |
| CSV (narrow or wide columnar) | `.csv` | No |

> **MDF auto-detection:** CAN Scope automatically probes each MDF file on open.
> If it contains raw CAN bus frames (`CAN_DataFrame.*` channel groups) it routes
> through the bus-logging pipeline and requires a DBC. Pre-decoded MDF files load
> directly without a DBC.

---

## Screenshots

Stacked plot:
<img width="3193" height="2022" alt="CANScope_V35" src="https://github.com/user-attachments/assets/0f413b0b-a23a-439f-b8c8-41848969c99c" />

Multiple Axis plot:
<img width="2376" height="1300" alt="Multi_plot" src="https://github.com/user-attachments/assets/efc7b429-befa-4344-bed3-4d5c2151ca98" />

CAN Trace / Raw Frame viewer:
<img width="1543" height="1019" alt="CANScope_RawCAN Frame" src="https://github.com/user-attachments/assets/69ad9749-ecec-445d-a31f-4b47901855cf" />

---

## Features

### File & Decoding
- **Multi-format loading** — BLF, ASC, MF4 (bus-logged and pre-decoded), MDF3, and CSV in one tool
- **Multi-channel DBC mapping** — assign a different DBC to each CAN channel via the DBC Manager; channel 0 acts as an "All Channels" fallback
- **DBC match quality indicators** — the DBC Manager shows per-channel decode coverage bars with J1939 PGN fallback scoring
- **Streaming decode** — signal tree and plots update in real time while large files are still decoding
- **Persistent channel config** — DBC-to-channel assignments saved as `.canscope_ch` JSON and reloaded automatically between sessions

### Plot Modes
- **Normal** — all signals share one Y axis
- **Multi-Axis** — independent Y axis per signal, all on the same X axis
- **Stacked** — INCA/CANdb-style lanes, one row per signal with independent Y scaling

### Cursors & Navigation
- **Dual cursors** — draggable C1 and C2 with live value readout in the signal table
- **ΔT display** — time delta between C1 and C2 shown in the toolbar
- **Fit to Window** — rescales X and Y to the full extent of all visible data
- **Fit Vertical** — rescales Y only to match data in the current X view (X range unchanged)
- Both fit operations work correctly across all three plot modes including multi-axis floating ViewBoxes

### Signal Table
- **5-column table** — Visibility ☑ | Signal | Cursor 1 | Cursor 2 | Unit
- **Signal grouping** — assign signals to named groups; collapse/expand groups with the arrow; toggle group visibility via the checkbox in the same column
- **Drag-and-drop reorder** — drag signals or use Ctrl+Up / Ctrl+Down
- **Visibility toggle** — check/uncheck individual signals or an entire group; cursor position and all other signal values remain stable during toggle
- **Signal name display options** — optionally prefix signal names with channel number and/or message name

### Configuration
- **Save / Load configuration** — fully persists: file paths, channel-DBC mapping, plotted signal list, group assignments, per-signal visibility, signal colors, plot background color, plot mode, cursor state, table column widths, signal name display flags
- **Undo** — up to 3 levels of undo for plot actions (Ctrl+Z)

### CAN Trace (Raw Frame Viewer)
- **On-disk indexed store** — no frame cap; 18 B/frame RAM, 64 B/frame on disk with memory-mapped O(1) access
- **Sliding window display** — shows 5 000 frames at a time with scroll
- **Filter by ID and channel** — vectorised numpy mask, no disk access for filtering
- **On-demand signal decode** — click a frame row to see decoded signals using the per-channel DBC

### Portable Build
- **Single-folder `.exe`** — no Python installation required on the target machine
- **Splash screen** — shows loading progress; minimisable, appears in taskbar, responds to Win+D / Show Desktop
- **Auto-build CI** — GitHub Actions builds and uploads a release ZIP on every `v*.*.*` tag push

---

## Getting Started

### Run from Source

```bash
git clone https://github.com/dinacaran/canscope.git
cd canscope
pip install -r requirements.txt
python app.py
```

For MDF bus-logging support (raw CAN frames inside MF4) also install:
```bash
pip install canmatrix
```

### Download Portable .exe

Go to [Releases](https://github.com/dinacaran/canscope/releases) and download
the latest `CANScope_vXX.XX.XX_Windows.zip`. Unzip anywhere and run `CANScope.exe` — no installer needed.

---

## Usage

| Step | Action |
|------|--------|
| 1 | **Open File** — select `.blf`, `.asc`, `.mf4`, `.mdf`, or `.csv` |
| 2 | **Open DBC** — required for BLF, ASC, and MF4 bus-logged files |
| 3 | **DBC Manager** *(optional)* — assign a different DBC per CAN channel |
| 4 | **Load + Decode** — background decode; signal tree populates live |
| 5 | Double-click or drag a signal to plot it, or select and press Space |
| 6 | Drag **Cursor 1** to read time and signal value in the table |
| 7 | Toggle **Cursor 2** to measure ΔT between two points |
| 8 | Switch between **Normal**, **Multi-Axis**, and **Stacked** plot modes |
| 9 | **Fit to Window** or **Fit Vertical** to rescale the view |
| 10 | **Save Config** (Ctrl+S) to persist the full session state |
| 11 | **Export CSV** to save selected signal data to a time-aligned file |
| 12 | **CAN Trace** to browse raw CAN frames (BLF / ASC / MF4 bus-log) |

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `F` | Fit to Window — rescale X and Y to all data |
| `V` | Fit Vertical — rescale Y only, keep current X |
| `Space` | Plot selected signal(s) from the signal tree |
| `Delete` | Remove selected signal from plot |
| `Ctrl+Z` | Undo last plot action (up to 3 levels) |
| `Ctrl+S` | Save configuration |
| `Ctrl+Up` | Move selected signal up |
| `Ctrl+Down` | Move selected signal down |
| `Ctrl+Shift+R` | Open Raw CAN Frame viewer |

---

## Build Portable .exe

```bash
pip install pyinstaller
pyinstaller CANScope.spec
```

Output in `dist/CANScope/`. The GitHub Actions workflow (`.github/workflows/build.yml`)
builds and uploads the ZIP automatically on every tag push matching `v*.*.*`.

---

## Project Structure

```
canscope/
├── app.py                        # Entry point, APP_NAME="CAN Scope"
├── core/
│   ├── models.py                 # RawFrame, DecodedSignalSample dataclasses
│   ├── channel_config.py         # ChannelConfig: {channel → DBC}, decoder cache, save/load .canscope_ch
│   ├── load_worker.py            # QThread: CAN-raw / bulk-array / sample-loop decode paths
│   ├── signal_store.py           # SignalStore, SignalSeries (array.array storage)
│   ├── raw_frame_store.py        # On-disk indexed store: 18 B/frame RAM + 64 B/frame disk, mmap
│   ├── dbc_decoder.py            # DBCDecoder with 3-level cache
│   ├── blf_reader.py             # BLFReaderService (wraps python-can)
│   ├── export.py                 # CSV export
│   └── readers/
│       ├── __init__.py           # reader_factory() + dbc_required_for() — format detection
│       ├── base.py               # MeasurementReader protocol
│       ├── blf_can_reader.py     # BLF + DBC pipeline
│       ├── asc_can_reader.py     # ASC + DBC pipeline
│       ├── mdf_reader.py         # MF4/MDF pre-decoded via asammdf + is_bus_logging() probe
│       ├── mdf_can_reader.py     # MDF4 bus logging via python-can MF4Reader + DBC
│       └── csv_reader.py         # Wide and narrow CSV
├── gui/
│   ├── main_window.py            # MainWindow, toolbar, config save/load, ChannelConfig integration
│   ├── plot_widget.py            # PlotPanel: normal / multi-axis / stacked, dual cursors, groups
│   ├── signal_tree.py            # SignalTreeWidget with live search
│   ├── raw_frame_dialog.py       # Sliding-window CAN Trace (RawFrameStore, no cap)
│   ├── dbc_manager.py            # DBC Manager dialog: per-channel DBC, match quality bars
│   └── splash.py                 # CANScopeSplash — minimisable, taskbar-visible splash screen
├── resources/
│   ├── splashscreen.png          # 1635 × 962 splash image
│   ├── CANScope_ICON.png         # 1254 × 1254 app icon source
│   └── app_icon.ico              # Multi-resolution ICO (256/128/64/48/32/16 px)
├── requirements.txt
├── CANScope.spec                 # PyInstaller spec, bundles resources/
└── .github/workflows/build.yml  # Auto-build on v*.*.* tag push
```

---

## Dependencies

| Package | Version | Purpose | License |
|---------|---------|---------|---------|
| PySide6 | ≥ 6.7 | GUI framework | LGPL v3 |
| pyqtgraph | ≥ 0.13 | Interactive plots | MIT |
| python-can | ≥ 4.6 | BLF / ASC / MF4 bus-log reading | LGPL v3 |
| cantools | ≥ 39.4 | DBC decoding | MIT |
| numpy | ≥ 1.26 | Array operations | BSD |
| asammdf | ≥ 7.0 | MF4 / MDF pre-decoded signals | MIT |
| canmatrix | any | MF4 bus-log support (optional) | LGPL v3 |
| pyinstaller | ≥ 6.0 | Portable .exe build | GPL v2 (build-only) |

---

## Code Signing Policy

⚠️ Release builds are currently unsigned. Windows SmartScreen may show a warning on first run — click **More info → Run anyway**.

---

## License

MIT — see [LICENSE](LICENSE).
