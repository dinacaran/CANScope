# Changelog

All notable changes to CAN Scope are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Version format: `vXX.YY.ZZ` — ZZ = patch, YY = feature, XX = breaking.

---

## [v00.00.03] — 2025-04-18

### Fixed
- `CANScope.spec`: syntax error (`"asammdf",,` double comma) that crashed
  the PyInstaller build step in GitHub Actions

---

## [v00.00.02] — 2025-04-18

### Added
- `CHANGELOG.md`, `CONTRIBUTING.md`, `DISCLAIMER.md`, `LICENSE`, `.gitignore`
- `.github/workflows/build.yml` — automated Windows build on tagged release

### Changed
- `APP_VERSION` bumped to `v00.00.02`

---

## [v00.00.01] — 2025-04-18

### Added
- **Multi-format support** — BLF, ASC, MF4, MDF, CSV in one tool
  - `core/readers/` package with `MeasurementReader` protocol and `reader_factory()`
  - `BLFCANReader` — wraps existing BLF + DBC pipeline
  - `ASCCANReader` — Vector CANalyzer ASCII log + DBC pipeline
  - `MDFReader` — ASAM MDF v3/v4 via `asammdf` (optional dependency)
  - `CSVSignalReader` — auto-detects wide (columnar) vs narrow (our export) format
- **Dual-cursor measurement system** — draggable C1 (yellow) and C2 (cyan)
  with ΔT time delta readout; cursors centre in current view on toggle
- **Stacked plot mode** — per-row `InfiniteLine` instances (fixes Qt scene
  ownership crash); C1/C2 labels hidden when cursor is toggled off
- **Fit Vertical** (`V` key) — rescales Y only to data in visible X range;
  works in Normal, Multi-Axis, and Stacked modes
- **Smart signal search** — substring match (no wildcards needed);
  wildcards (`*`, `?`) still work
- **Signal table** — 4 columns: Signal · Cursor 1 · Cursor 2 · Unit;
  Cursor 2 column auto-shown/hidden with cursor state
- **Save / Load configuration** — persists BLF/DBC paths, signals, colors,
  plot mode, cursor states, column widths; color fully restored on load
- **Streaming decode** — signal tree updates every 2,000 frames;
  live plot refresh every 5,000 frames; signals plottable before decode finishes
- **Performance optimisations** in `DBCDecoder` —
  candidate ID cache, choices cache, kwargs cache; `array.array('d')` storage
  (3× less memory than Python list); inline timestamp normalisation

### Changed
- Application renamed from **BLF Viewer** to **CAN Scope**
- "Open BLF" button replaced with **"Open File"** supporting all formats
- DBC file is now optional for MF4/MDF/CSV formats
- "Remove Selected Plot" button label shortened to **"Remove Selected"**
- Toolbar "Raw Frames" button removed; accessible via `Ctrl+Shift+R`
- `LoadWorker` decoupled from BLF/DBC — accepts any `MeasurementReader`
- Log file renamed `canscope_dev.log`; default config `canscope_config.json`
- Version format changed to `vXX.YY.ZZ`

### Fixed
- BLF timestamps normalised to t=0 (previously showed Unix epoch ~1.29e9 s)
- Stacked plot cursor C1/C2 labels no longer visible when cursor is toggled off
- Stacked plot cursor crash fixed — each row owns its own `InfiniteLine` instance
- Cursor re-centres in current view when toggled ON after pan/zoom

---

## Prior history (BLF Viewer, internal versions)

| Version | Date | Summary |
|---------|------|---------|
| v0.2.2.4 | 2025-04 | Color restore in config, Fit Vertical, Remove Selected label |
| v0.2.2.3 | 2025-04 | Background color in right-click menu, stacked axis alignment, versioning |
| v0.2.2.x | 2025-04 | Stacked cursor crash fix (per-row InfiniteLine), bg color improvements |
| v0.2.1.x | 2025-04 | Cursor system, Cursor 1/2 toggle buttons, centre-on-toggle |
| v0.2.1 | 2025-04 | Raw Frames hidden from GUI, smart search, dual cursors, delta time |
| v0.2.0 | 2025-04 | Performance optimisations, streaming decode, drag-and-drop to table |
| v0.1.0 | 2025-03 | Initial MVP — BLF + DBC, interactive plot, signal tree, CSV export |
