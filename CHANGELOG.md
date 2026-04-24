# Changelog

All notable changes to CAN Scope are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Version format: `vXX.YY.ZZ` — ZZ = patch, YY = feature, XX = breaking.

---

## [v00.00.26] — 2025-04-24

### Fixed
- **Selected signal stays thick after clicking plot area** (v00.00.25 regression).
  Root cause: `table.clearSelection()` removes the visual highlight but
  `table.currentRow()` keeps returning the last-clicked row number.
  When `clearSelection()` fired `itemSelectionChanged` → `_emit_selection`,
  `currentRow()` still pointed at the old row, so the curve was immediately
  re-thickened.
  Two fixes applied:
  1. `_emit_selection` now calls `item.isSelected()` before treating a row
     as active — if the row exists but is no longer selected, it calls
     `_refresh_highlight()` and returns without re-highlighting anything.
  2. Both deselect paths (`_on_plot_area_click` and the stacked left-click
     handler) now wrap the clear in `table.blockSignals(True/False)` and
     also call `table.setCurrentCell(-1, -1)` to truly clear the current
     row before calling `_refresh_highlight()`.

---

## [v00.00.25] — 2025-04-24

### Added
- **Selected signal drawn thicker** — when a signal is selected in the
  selected-signal panel, its curve is drawn at width 5.0 (normal = 2.8),
  making it immediately visible in all plot modes (normal, multi-axis,
  stacked).
- **Click anywhere in plot area to deselect** — left-clicking the plot
  area (normal / multi-axis mode) or the stacked plot area clears the
  table selection and restores all curves to normal thickness.  The
  mechanism: `sigMouseClicked` on the main ViewBox + left-button handling
  in the stacked scene click handler.

### Implementation notes
- `_apply_curve_style(plotted, selected=False)` — added `selected`
  parameter; `width = 5.0 if selected else 2.8`.
- `_refresh_highlight()` — new lightweight method that iterates `_items`
  and calls `_apply_curve_style` with the correct flag.  Updates only the
  pen, no data reload or layout recalculation.
- Called from: `_emit_selection` (table click), `_rebuild_curves` (after
  add/remove/reorder), `_on_plot_area_click` (plot left-click),
  `_on_stacked_scene_click` left-button path.

---

## [v00.00.24] — 2025-04-24

### Fixed
- **Panel toggle buttons invisible on light/white background** —
  `left_edge_btn` and `bottom_edge_btn` used `setAutoRaise(True)` which
  renders the button as a flat, transparent widget inheriting the system
  theme.  On light or white plot backgrounds the Unicode arrow text blended
  in and disappeared.  Both buttons now have an explicit QSS stylesheet:
  dark charcoal background (`#2d3a4a`), white text (`#e0e8f0`), a visible
  `1px solid #4a6080` border, and blue hover/pressed states.  Visible on
  any plot background color, light or dark.  Tooltips added.

- **Right-click on signal table does nothing after decode** — `_show_table_menu`
  returned early when `selected_keys` was empty, so right-clicking without
  first selecting a signal silently did nothing.  Menu now always opens.
  Signal-specific actions are present but **greyed out** when nothing is
  selected:
  - *Change signal color* — disabled unless exactly one signal selected
  - *Move selected up / down* — disabled unless a signal is selected
  - *Remove selected signal* — disabled unless a signal is selected
  - *Signal name display*, *Set plot background color* — **always enabled**

---

## [v00.00.23] — 2025-04-23

### Changed — Option B: on-disk indexed raw frame store (no frame cap)

Replaced the 100,000-frame in-memory `list[RawFrameEntry]` with an
on-disk indexed store (`core/raw_frame_store.py`).  All CAN frames are
now stored regardless of file size.

**Architecture:**

| | Before (capped list) | After (on-disk index) |
|---|---|---|
| Storage | Python objects in RAM | Compact arrays + temp file |
| RAM / frame | ~700 B → ~70 MB/100k | 18 B → ~54 MB / 3M frames |
| Disk / frame | — | 64 B → ~192 MB / 3M frames |
| Frame cap | 100,000 | **None** |
| Filter | Python loop over RawFrameEntry | Vectorised numpy on in-memory arrays |
| Signal decode | Pre-stored per frame | Lazy on-demand when row is expanded |

**`core/raw_frame_store.py`** (new):
- In-memory per frame (18 B): `timestamps`, `channels`, `arb_ids`, `dlcs`,
  `directions`, `flags`, `name_ids` — all `array.array`, no Python objects.
- `name_table`: list of unique message names (~100–500 entries).
- Disk temp file: 64 raw data bytes per frame, `mmap`-accessed for O(1)
  random reads.  Auto-deleted on `close()` / GC.
- `build_match_mask(needle, channel)`: returns numpy bool array using
  vectorised operations — no Python loop, no disk access for most filters.
- `get_window(indices)`: reads exactly the visible 5,000 frames from disk.
- `seal()`: called after decode to open the mmap for random access.

**Signal decode**: clicking ▶ on a row now decodes that single frame
on-demand using the warm `DBCDecoder` cache (~1 ms per frame).  Previously
all 5,000 signal children were pre-rendered on every window change.

---

## [v00.00.22] — 2025-04-23

### Baseline
- Rolled back to v00.00.19 as baseline.
  All v00.00.20 and v00.00.21 DBC matching changes reverted.
  Original broad matching (exact + &0x1FFFFFFF + &0x7FF + J1939 PGN
  fallback) restored.

### Fixed
- **Channel numbering starts from 1** — python-can returns 0-indexed
  channels from BLF/ASC files; Vector hardware uses 1-indexed (CAN 1,
  CAN 2…).  Fixed in `core/blf_reader.py` and
  `core/readers/asc_can_reader.py`.

---

## [v00.00.19] — 2025-04-22

### Added
- **CAN Trace toolbar button** — sits next to Shortcuts; opens the Raw CAN
  Frame viewer directly.  Greyed out until a BLF/ASC file has been decoded
  (MF4/CSV have no raw frames so it stays grey for those formats).

### Changed
- **Cursors off by default** — both Cursor 1 and Cursor 2 start unchecked
  when the application opens.  Config save/load defaults updated to match.
- **Toolbar greyed-out states** — actions that are not yet usable are
  disabled (visually greyed) until the required precondition is met:
  - Always enabled: Open File, Load Config
  - Enabled after measurement file is selected: Open DBC, Load + Decode
  - Enabled after decode completes: Save Config, Export CSV, Clear Plots
  - CAN Trace: enabled only after a BLF/ASC decode (raw frames present)
  `_update_action_states()` is called at startup, on file select, on decode
  complete, and when the store is cleared.

---

## [v00.00.18] — 2025-04-22

### Fixed
- **Jump field not accepting keyboard input** — replaced `QDoubleSpinBox`
  (which intercepts arrow keys and competes with the tree widget for focus)
  with a plain `QLineEdit`.  Type any value in seconds and press Enter or
  click Go.  Invalid input turns the border red; it clears on the next valid
  entry.  Accepts formats like `33.5`, `33.456`, `0`.
- **Hang when scrolling after Expand All** — `_set_window()` now checks
  `_is_expanded` and calls `tree.collapseAll()` (instant C++ operation)
  before rebuilding the window.  Without this, clearing and reinserting
  5,000 top-level items each carrying 8 expanded children (≈40,000 visible
  rows) caused a multi-second freeze on every scroll step.  The tree
  auto-collapses on any navigation action; the user can re-expand the new
  window contents at will.  The Collapse All button also clears the flag.

---

## [v00.00.17] — 2025-04-21

### Changed
- **Raw frame dialog — scrollbar now visible** — styled the section scrollbar
  to match button height (30 px), with a clear `#4a6080` border, blue-toned
  drag handle, hover/press states, and always-visible arrow buttons on both
  ends.  Previously it rendered as a near-invisible 1 px line on dark themes.
- **Raw frame dialog — removed Filter dropdown** — the
  "All Frames / Decoded Only / Undecoded Only" combo added little value and
  cluttered the toolbar.  Removed entirely; search + channel filter are
  sufficient for narrowing frames.

---

## [v00.00.16] — 2025-04-21

### Changed
- **Raw CAN Frame viewer — full sliding window navigation**
  - Removed the hard 5,000-row cap that previously prevented viewing the
    rest of the file.  All matching frames are accessible via navigation.
  - **Section scrollbar** spans the entire filtered frame list; dragging it
    moves the 5,000-row visible window to any position in the data.
  - **Time-labelled nav buttons** — each button shows the absolute timestamp
    of its destination above it:
    - `[◀◀ Start]` → shows recording start time (e.g. `0.000 s`)
    - `[◀ −1000]`  → shows time 1,000 frames before current window
    - `[+1000 ▶]`  → shows time 1,000 frames after current window
    - `[End ▶▶]`   → shows recording end time
  - **Jump to time** — type any timestamp in seconds and press Go (or Enter)
    to centre the window on the nearest frame.  Binary search used for speed.
  - **Status bar** shows current frame range and time span:
    `Showing frames 12,450–17,450 of 29,015 matching  (33.450 s → 38.512 s)`
  - **Expand All** operates only on the visible 5,000-row window,
    not the entire file.

---

## [v00.00.15] — 2025-04-21

### Fixed
- **Blank splash screen in portable EXE** — two root causes:
  1. `resources/splashscreen.png` was not listed in `CANScope.spec`'s
     `datas` — only `app_icon.ico` was conditionally added.  Added an
     unconditional entry for `splashscreen.png` so PyInstaller always
     bundles it into the `resources/` subfolder of the extraction directory.
  2. `gui/splash.py` resolved the image path using
     `Path(__file__).resolve().parents[1]` which points to the source tree
     in dev mode but is wrong inside a frozen EXE (where `__file__` is
     inside `sys._MEIPASS`).  Added `_resource_root()` helper that returns
     `Path(sys._MEIPASS)` when frozen and the project root otherwise.
     Same fix applied to the icon path in `app.py`.

---

## [v00.00.14] — 2025-04-21

### Changed
- **Reverted lazy import of cantools** — restored v00.00.11 startup behaviour.
  `core/readers/base.py` re-links to `core.dbc_decoder` (which imports
  `cantools` at startup) rather than the lightweight `core.models`.
  Reason: the first BLF/ASC file load felt slightly slower in v00.00.13
  because cantools was deferred until that point; startup + first-load
  combined felt less responsive than the original eager-load pattern.
  Splash screen (`gui/splash.py`, `resources/splashscreen.png`) is retained.
  `core/models.py` is retained (used by all other modules).

---

## [v00.00.13] — 2025-04-20

### Fixed
- **BLF/ASC load crash** — `SyntaxError: from __future__ imports must occur
  at the beginning of the file` in `blf_reader.py`.  Caused by the v00.00.12
  refactor that prepended `from core.models import RawFrame` before the
  `from __future__ import annotations` line.  Fixed ordering so
  `from __future__` is always line 1.

---

## [v00.00.12] — 2025-04-20

### Added
- **Splash screen** — displayed immediately on launch before any heavy
  modules are loaded.  Shows the `resources/splashscreen.png` image scaled
  to 60% of screen height, with a live loading-status line rendered inside
  the frosted-glass panel area and the version string overlaid in the
  bottom-right corner.  Dismisses automatically when `MainWindow` is ready.

### Performance — startup time improvement
- **Lazy import of heavy dependencies** — extracted `RawFrame` and
  `DecodedSignalSample` dataclasses into a new `core/models.py` module that
  has zero heavy dependencies.  Previously, the import chain
  `app → main_window → load_worker → readers/base → dbc_decoder → cantools`
  caused `cantools` (and transitively `diskcache`) to be imported eagerly at
  startup even though they are only needed when a BLF/ASC file is actually
  loaded.  Now `cantools` and `python-can` are only imported inside
  `reader_factory()` when the first file is opened.
- **Splash shown before heavy imports** — `app.py` now shows the splash
  screen before `from gui.main_window import MainWindow`, so the user sees
  the splash image immediately while pyqtgraph, cantools and other packages
  finish loading in the background.

---

## [v00.00.11] — 2025-04-20

### Added
- **Undo** (`Ctrl+Z`) — restores the previous plot state, up to 3 levels deep.
  Covered actions: add signal, remove signal, remove selected, clear all plots,
  reorder (move up/down), change signal colour.  Each undo step restores signal
  key order and per-signal colour; signal data is never re-decoded.
  Undo history is cleared when a new file is loaded.
- **Shortcuts dialog** — new **Shortcuts** button on the toolbar (after Clear
  Plots). Opens a popup listing all keyboard shortcuts with descriptions.
  All shortcuts defined in one `_SHORTCUTS` list so the dialog is always in
  sync with the actual bindings.

---

## [v00.00.10] — 2025-04-20

### Changed
- **Cursor line colour** — both C1 and C2 are now blue (`#0000ff`).
  C1 = solid blue, C2 = dashed blue. Previously C1 was yellow and C2 cyan.
- **Stacked plot cursor labels** — "C1" / "C2" text labels now appear only
  on the **bottom-most row** instead of every row.  Every other row still
  shows the cursor line itself; the label clutter is gone.
  This applies both at initial render and when toggling cursors on/off.

---

## [v00.00.09] — 2025-04-20

### Fixed
- **EXE crash on startup** — `No module named 'diskcache'` when running the
  portable build (v00.00.07 regression).  cantools imports `diskcache`
  unconditionally at module level in its `__init__.py`, so PyInstaller must
  bundle it even though CAN Scope never activates the cache.
  Reverted `excludes=['diskcache']` in `CANScope.spec`.
  The Dependabot pickle-deserialization alert (diskcache ≤ 5.6.3) does not
  apply: `cache_dir` is never passed to `cantools.database.load_file()`, so
  no cache directory is ever created or read.  Dismissed with a comment on
  the GitHub alert.

### Includes all changes from v00.00.08_test01
- Stacked plot axis labels bottom-anchored and clipped at row top
  (`_BottomClippedTextItem`, `_StackedLeftAxis`)

---

## [v00.00.08] — 2025-04-19

### Fixed
- **Raw frame timestamp double-subtraction** — `normalize_timestamps
  (already_normalized=True)` was subtracting `base_ts` a second time from
  `RawFrameEntry.time_s`, which was already normalised by LoadWorker before
  `add_raw_frame()` was called. Result was large negative values (~−1.29e9 s).
  Fix: the `already_normalized=True` path now returns immediately with no
  further arithmetic.
- **Start of Frame column removed** — column was redundant (always identical
  to Time) and wasted space. Dialog is now 7 columns:
  `Time (s) | Chn | ID | Name | Dir | DLC | Data / Value`.
- **Raw frame dialog slow loading** — `addTopLevelItem()` called per row
  triggered one Qt layout recalculation per frame; 100k frames meant 100k
  reflows.  Fix: build all `QTreeWidgetItem` objects in memory first, then
  call `insertTopLevelItems(0, items)` once inside a
  `setUpdatesEnabled(False/True)` block — single layout pass regardless of
  row count.  Display is capped at **5,000 visible rows** with a status line
  showing "Showing X of Y matching frames — narrow the search to see more".
  Filtering (search, channel, decoded/undecoded) now runs on the raw list
  before any Qt objects are created, so searches on large files are fast.

---

## [v00.00.07] — 2025-04-19

### Security
- **Exclude `diskcache` from PyInstaller EXE** — cantools lists `diskcache`
  as a dependency for its optional DBC caching feature, but CAN Scope never
  passes `cache_dir` to `cantools.database.load_file()`, so the cache is
  never created or read.  Added `excludes=['diskcache']` to `CANScope.spec`
  so the package is not bundled in the portable EXE, eliminating the
  pickle-deserialization attack surface (Dependabot alert #1, CVE diskcache
  ≤ 5.6.3).

### Added
- **Signal name display options** in the selected-signal panel right-click menu.
  New sub-menu **Signal name display** with two checkboxes:
  - **Show channel** — prepends `CH0::` to the signal label
  - **Show message** — prepends `EEC1::` to the signal label
  Default is signal name only (e.g. `EngSpeed`).  Any combination works
  (`EEC1::EngSpeed`, `CH0::EngSpeed`, `CH0::EEC1::EngSpeed`).
  The full `CH::MSG::SIG` key is still stored internally so drag-and-drop,
  config save/load, cursor values, and all other features are unaffected.
  Selection is persisted in the configuration JSON.

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
