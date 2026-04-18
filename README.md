# CAN Scope

> A portable, zero-install Windows tool for loading CAN/automotive measurement files,
> decoding signals with DBC databases, and plotting them interactively.

![Python](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey?logo=windows)
![License](https://img.shields.io/badge/License-MIT-green)
![Release](https://img.shields.io/github/v/release/dinacaran/canscope)

---

## Supported Formats

| Format | Extension | DBC Required |
|--------|-----------|-------------|
| Vector Binary Logging Format | `.blf` | Yes |
| Vector CANalyzer ASCII Log | `.asc` | Yes |
| ASAM MDF version 4 | `.mf4` | No — pre-decoded |
| ASAM MDF version 3 | `.mdf` | No — pre-decoded |
| CSV (narrow or wide columnar) | `.csv` | No — pre-decoded |

---
## Screenshots
Stacked plot:
<img width="3000" height="1500" alt="Stacked_plot" src="https://github.com/user-attachments/assets/91ec3794-cbb9-46ba-a6d2-531d02278bd2" />

Multiple Axis plot:
<img width="2376" height="1300" alt="Multi_plot" src="https://github.com/user-attachments/assets/efc7b429-befa-4344-bed3-4d5c2151ca98" />

Full tool view:
<img width="2556" height="1400" alt="BLF Viewer full view" src="https://github.com/user-attachments/assets/239b7dd1-948a-498b-a309-07e967dcc94d" />
---

## Features

- **Multi-format loading** — BLF, ASC, MF4, MDF, and CSV in one tool
- **DBC signal decoding** — full physical value conversion including factor, offset, unit, and enum labels
- **Streaming decode** — signal tree and plots update in real time while large files are still decoding
- **Three plot modes** — Normal (shared Y), Multi-Axis (independent Y per signal), Stacked (INCA/CANdb-style lanes)
- **Dual cursors** — draggable C1 (yellow) and C2 (cyan) with live value readout and ΔT time delta
- **Fit to Window / Fit Vertical** — full range or Y-only fit for visible X range
- **Signal search** — instant substring filter, no wildcards needed
- **Export to CSV** — time-aligned, all selected signals in one file
- **Save / Load configuration** — persists file paths, signals, colors, plot mode, cursor state
- **Portable `.exe`** — single folder, no Python installation required on target machine

---

## Getting Started

### Run from Source

```bash
git clone https://github.com/dinacaran/canscope.git
cd canscope
pip install -r requirements.txt
python app.py
```

For MF4/MDF support also install:
```bash
pip install asammdf>=7.0
```

### Download Portable .exe

Go to [Releases](https://github.com/dinacaran/canscope/releases) and download
the latest `CANScope_vXX.XX.XX_Windows.zip`. Unzip and run `CANScope.exe`.

---

## Usage

| Step | Action |
|------|--------|
| 1 | **Open File** — select `.blf`, `.asc`, `.mf4`, `.mdf`, or `.csv` |
| 2 | **Open DBC** — required only for BLF and ASC |
| 3 | **Load + Decode** — background decode, signal tree populates live |
| 4 | Double-click or drag a signal to plot it |
| 5 | Drag **Cursor 1** (yellow) to read time and signal value |
| 6 | Toggle **Cursor 2** (cyan) to measure ΔT between two points |
| 7 | **Multi-Axis** or **Stacked** for different layout modes |
| 8 | **Export CSV** to save selected signal data |

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `F` | Fit to Window |
| `V` | Fit Vertical (Y only) |
| `Space` | Plot selected signal(s) |
| `Delete` | Remove selected signal |
| `Ctrl+S` | Save configuration |
| `Ctrl+Up/Down` | Reorder signal |
| `Ctrl+Shift+R` | Raw Frames viewer (BLF/ASC only) |

---

## Build Portable .exe

```bash
pip install pyinstaller
pyinstaller CANScope.spec
```

Output in `dist/CANScope/`. The GitHub Actions workflow builds and uploads
the zip automatically on every tagged release.

---

## Project Structure

```
canscope/
├── app.py
├── core/
│   ├── readers/
│   │   ├── __init__.py          # reader_factory() — format detection
│   │   ├── base.py              # MeasurementReader protocol
│   │   ├── blf_can_reader.py    # BLF + DBC pipeline
│   │   ├── asc_can_reader.py    # ASC + DBC pipeline
│   │   ├── mdf_reader.py        # MF4/MDF via asammdf
│   │   └── csv_reader.py        # Wide and narrow CSV
│   ├── blf_reader.py
│   ├── dbc_decoder.py
│   ├── signal_store.py
│   ├── load_worker.py
│   └── export.py
├── gui/
│   ├── main_window.py
│   ├── plot_widget.py
│   ├── signal_tree.py
│   └── raw_frame_dialog.py
├── requirements.txt
├── CANScope.spec
└── .github/workflows/build.yml
```

---

## Dependencies

| Package | Version | Purpose | License |
|---------|---------|---------|---------|
| python-can | ≥ 4.6 | BLF / ASC reading | LGPL v3 |
| cantools | ≥ 39.4 | DBC decoding | MIT |
| PySide6 | ≥ 6.7 | GUI framework | LGPL v3 |
| pyqtgraph | ≥ 0.13 | Interactive plots | MIT |
| numpy | ≥ 1.26 | Array operations | BSD |
| asammdf | ≥ 7.0 | MF4/MDF (optional) | MIT |

---

## Code Signing Policy

Release builds are signed by **GDLabs** via SignPath Foundation.

---

## License

MIT — see [LICENSE](LICENSE).
