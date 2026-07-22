# CAN Scope

> A portable Windows application for loading automotive measurements, decoding CAN signals, inspecting raw frames, and plotting signal data interactively.

![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey?logo=windows)
![License](https://img.shields.io/badge/License-MIT-green)
![Release](https://img.shields.io/github/v/release/dinacaran/canscope)

## Screenshots

Stacked plot:
<img width="2299" height="1588" alt="Stacked plot" src="https://github.com/user-attachments/assets/57baedc2-0382-4f63-a7e9-cc786e18ded8" />


Multiple Axis plot:
<img width="2214" height="1590" alt="multi-axis plot" src="https://github.com/user-attachments/assets/7d3ef351-1b02-45e0-85f5-4dfd1217ad6e" />

CAN Trace / Raw Frame viewer:
<img width="1543" height="1019" alt="CANScope_RawCAN Frame" src="https://github.com/user-attachments/assets/69ad9749-ecec-445d-a31f-4b47901855cf" />

## Supported formats

| Measurement | Content | Database |
|---|---|---|
| BLF (`.blf`) | Raw CAN frames | DBC or ARXML required |
| ASC (`.asc`) | Raw CAN frames | DBC or ARXML required |
| MDF4/MDF3 (`.mf4`, `.mdf`) | Raw CAN bus logging or pre-decoded signals | Required only for raw bus logging |
| CSV (`.csv`) | Raw CAN export, narrow signals, or wide signals | Required only for raw CAN data |

MDF and CSV content is detected automatically. Database Manager supports per-channel DBC/ARXML assignments, mixed database formats, decode-coverage indicators, and a channel-0 fallback.


## Current capabilities

- Fast bulk loading and decoding for BLF, ASC, MDF/MF4, and CSV measurements.
- Searchable channel/message/signal tree with multi-select plotting and drag-and-drop.
- Indexed **CAN Trace** viewer for raw frames, filtering, and on-demand signal decode.
- Formula-based **New Signal** generation with arithmetic, comparisons, and logical expressions; generated definitions are saved in configurations.
- CSV and Excel (`.xlsx`) export of plotted signals on a shared time axis.
- JSON configuration save/load for measurement paths, database mapping, generated signals, plotted order, groups, colors, visibility, axes, cursors, and display settings.
- YAML rule-based diagnostics for expressions, ranges, fault flags, and message loss; optional GitHub Models analysis is available through `Ctrl+Shift+A`.

### Plotting

- **Stacked** is the default mode and gives each signal its own lane and Y scale.
- **Normal** overlays signals on one Y axis.
- **Multi-Axis** groups axes by unit, with an optional individual axis per signal.
- Dual draggable cursors provide values and delta time.
- Fit-to-window and vertical-fit work across all plot modes while preserving the relevant view range.
- Signals can be grouped, reordered by dragging, recolored, hidden, or removed.
- **Show Data Points** uses adaptive markers and thinner lines.
- **Hide Line** is enabled only when data points are shown; when ON, it displays points without signal lines.

## Install and run

### Portable release

Download the latest Windows ZIP from [Releases](https://github.com/dinacaran/canscope/releases), extract it, and run `CANScope.exe`. No installer or Python installation is required.

### From source

```powershell
git clone https://github.com/dinacaran/canscope.git
cd canscope
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

The portable build and CI release build use Python 3.12.

## Basic workflow

1. Select **Open File** and choose a supported measurement.
2. If the file contains raw CAN frames, select **Open Database** and configure DBC/ARXML mapping.
3. Select **Load + Decode**.
4. Search the signal tree, then double-click, drag, press `Space`, or use the context menu to plot signals.
5. Use cursors, plot modes, data points, grouping, and fit controls as needed.
6. Select **Save Config** to preserve the session or **Export** to write CSV/Excel data.
7. Open **CAN Trace** when the source contains raw CAN frames.

## Keyboard shortcuts

| Key | Action |
|---|---|
| `F` | Fit X and Y to all visible data |
| `V` | Fit Y to the current X range |
| `Space` | Plot selected signal-tree entries |
| `C` | Change the selected signal color |
| `R` | Toggle both cursors |
| `Delete` | Remove selected plotted signals |
| `Ctrl+Z` | Undo the last plot action |
| `Ctrl+S` | Save configuration |
| `Ctrl+Shift+R` | Open CAN Trace |
| `Ctrl+Shift+-` / `Ctrl+Shift++` | Collapse / expand signal-tree messages |


## Test and build

```powershell
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```

Build the portable application with:

```powershell
.\build_portable.bat
```

Output is written to `dist\CANScope\`. Pushing a `v*.*.*` tag runs the Windows build workflow and publishes a release ZIP.

## Repository layout

```text
app.py                 Application entry point and version
core/readers/          BLF, ASC, MDF/MF4, and CSV readers
core/                  Decode, signal storage, export, and generated-signal logic
core/diagnostics/      Rule engine and optional AI-assisted diagnostics
gui/                   Main window, plots, database manager, CAN Trace, diagnostics
config/diagnostics/    User-editable diagnostic rules and documentation
resources/             Application icon and splash assets
tests/                 Pytest regression suite
```

Runtime dependencies are maintained in [requirements.txt](requirements.txt). 

## Security and license

Release binaries are currently unsigned, so Windows SmartScreen may show a warning on first launch.

CAN Scope is licensed under the [MIT License](LICENSE).
