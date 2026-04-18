# Changelog

All notable changes to CAN Scope are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Version format: `vXX.YY.ZZ` — ZZ = patch, YY = feature, XX = breaking.

---

## [v00.00.06] — 2025-04-19

### Fixed
- **MF4 crash on large files** — `ValueError: could not convert string to float: np.bytes_(b"")`
  in `_iter_arrays` when a channel's object array contains `np.bytes_` scalars.
  Three-layer hardening:
  1. **Better detection** — `_is_text()` helper probes `arr.flat[0]` for
     `np.bytes_` / `bytes` / `str` values, not just `dtype.kind` which
     reported `"O"` without inspecting the element type.
  2. **Guarded `raw=True` cast** — `np.asarray(raw_int, dtype=float64)` now
     wraps in `try/except`; if the raw fetch also returns bytes the channel
     gets an index-based numeric array instead of crashing.
  3. **Numeric-path fallback** — if the engineering-value cast fails for
     any other reason, the channel is silently re-routed through the text
     path rather than raising an unhandled `ValueError`.
  All three paths now handle `np.bytes_` consistently in the display list.

---

## [v00.00.05] — 2025-04-19

### Added
- **Elapsed time logging** — total file load + decode time is now printed in
  the Log tab and appended to the Diagnostics tab after every decode.
  Format: `elapsed: X.X s` (or `X.X min` for long files).

### Performance — MF4/MDF (10–20× improvement)
- **Vectorised channel-array fast path** — `MDFReader.iter_channel_arrays()`
  yields one `(meta, ts_arr, num_arr, disp_list)` tuple per channel using
  numpy operations.  `SignalStore.add_series_bulk()` inserts the entire
  channel via `array.array.frombytes(ndarray.tobytes())` — a single C-level
  memcopy.  The old path allocated one Python `DecodedSignalSample` object
  per sample; for a 500-channel × 10k-sample MF4 that was 5 million heap
  allocations per decode.
- **Timestamp normalisation vectorised** — `ts_arr - base_ts` (numpy
  subtraction) replaces the old per-sample Python subtraction.
- **Memory** — `del ts_arr / num_arr / eng_arr` after each channel yield
  frees the source arrays before the next `mdf.get()` call; peak heap RAM
  is now bounded to ~2 channels at a time regardless of file size.
- **Streaming intervals adjusted for MDF** — tree update fires once per
  channel (instant signal discovery); plot refresh every 10 channels;
  progress log every 50 channels.  Previously intervals were in samples
  (2k/5k/10k) which caused hundreds of Qt widget rebuilds per channel.
- **BLF/ASC unaffected** — CAN-raw path is unchanged; same intervals (2k
  frames / 5k frames / 10k frames).

---

## [v00.00.04] — 2025-04-18

### Fixed
- **MF4/MDF enum signal plotting** — channels returning string labels
  (`"ControlMode"`, `"ReadyMode"` etc.) now plot correctly as integer step
  values.  Root cause: `asammdf` with `raw=False` returns the display string
  for enum channels, making `float(label)` fail and setting
  `numeric_value = nan` so nothing appeared on the plot.
  Fix: detect string-dtype arrays (`dtype.kind in "OUS"`) and fetch the
  same channel a second time with `raw=True` to obtain the integer key for
  plotting.  Numeric channels are unaffected (no extra I/O).
  The cursor table still shows the human-readable label (`value` field);
  the plot now shows the integer step (`numeric_value` field) — identical
  behaviour to enum signals decoded from BLF via DBC.

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
