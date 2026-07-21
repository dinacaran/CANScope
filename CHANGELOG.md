# Changelog
## [v00.00.51] — 2026-07-21:new feature "New Signal" signal generation by formula added 
## [v00.00.50] — 2026-07-15: Fast load of MDF, BLF,ASC Fast loading CAN trace enabled 
## [v00.00.49] — 2026-07-12: build check
## [v00.00.48] — 2026-07-11
### MF4 buslogging file decoding changed from CANtool to ASMMDF, vectorize batch conversion implement
### multi signal check/uncheck added

## [v00.00.47] — 2026-07-07

### Added — General **Export** feature: export plotted signals to CSV or Excel (.xlsx) as a wide, time-aligned table (shared Time column, one column per signal).

### Changed — "Show Data Points" renders adaptively (only visible points, capped per curve) for fast plotting on multi-million-sample signals, including stacked mode.

### Changed — Removed the Move Up / Move Down buttons — reorder signals by dragging rows in the plot list.

### Fixed — Token dialog "Test & Save" no longer hangs on "Testing token…".

---

## [v00.00.46] — 2026-07-03

### Changed — Multi-axis mode: shared Y axis per unit, axis-color swatch column, Individual axis in right-click menu.

**Unit grouping** (`gui/plot_widget.py` — `PlotPanel._rebuild_overlay`)

**`fit_to_window` / `fit_vertical`** — multi-axis Y-fit now computes the union
range across all signals sharing a ViewBox, ensuring the axis is not
over-zoomed to just the first signal in each group.

---

## [v00.00.45] — 2026-06-30

### Added — Test suite

Introduced an automated test suite (`tests/`) with pytest infrastructure:

- Binary test fixtures for BLF and ASC formats generated on first run via
  `tests/fixtures/_generate.py` (fixtures are not committed).
- Pre-commit hook installer (`tests/install_hooks.py`) runs the suite before
  each commit.
- `pytest.ini` and `requirements-dev.txt` added for test configuration and
  developer dependencies.

### Fixed — Adding or removing a signal resets the plot view (X and Y ranges)

Adding or removing a signal from an already-populated plot snapped the X
range back to the full file extent and reset all Y axis zooms, causing the
user to lose their current time-window focus.

**Root cause (main\_window.py):** `add_signal_to_plot` and
`add_signals_to_plot` called `fit_to_window()` unconditionally after every
add, overriding the view ranges that `_rebuild_curves` had already saved and
restored.

- Both call sites now capture `was_empty` (whether the plot had no signals)
  before the add. `fit_to_window()` is only called when `was_empty` is true —
  i.e., the first signal being added to a blank plot.
- Adding subsequent signals to an already-populated plot no longer touches the
  viewport.

**Root cause (plot\_widget.py, multi-axis mode):** creating a new `ViewBox`
for an extra Y axis left auto-range enabled by default. The first `setData`
call on the curve triggered an auto-range pass that propagated through the
`setXLink` chain and reset the main plot's X range. The subsequent
save/restore could correct Y but could not undo the X clobber.

- `enableAutoRange(x=False, y=False)` and `setAutoVisible(x=False, y=False)`
  are now called on each new `ViewBox` immediately after construction and
  **before** `setXLink` — cutting the propagation path before it is formed.
- Newly added signals (not present in the saved Y-range dict) have their Y
  range auto-fitted from their own data after the rebuild, so the new axis
  shows the signal correctly without affecting the main X range.

### Fixed — Signal visibility toggle (ON) does not render the signal

Enabling a signal's visibility checkbox after it had been hidden was a no-op:
the signal remained invisible. Disable worked correctly.

**Root cause:** `_apply_visibility` used a lightweight path that called
`curve.setVisible(True)` on existing `PlotDataItem` objects. Invisible signals
have `curve = None` (set by `_rebuild_overlay` which skips them to keep the
multi-axis slot count correct), so the toggle had nothing to act on.

- `_apply_visibility` now checks whether any newly-enabled signal has
  `curve is None`. If so it falls through to a full `_rebuild_curves` call,
  which creates the missing curve (and, in multi-axis mode, the missing
  `ViewBox` and `AxisItem`) and restores all existing view ranges via the
  save/restore mechanism.
- The lightweight `setVisible` path is retained for the hide direction and
  for cases where all visible signals already have curves, avoiding
  unnecessary rebuild overhead.

---

## [v00.00.44] — 2026-06-24

### Fixed — Multi-Axis mode: Y-axis drag, label, and visibility bugs

**Y-axis drag only panned the first signal (all axes)**

In multi-axis mode the main left `AxisItem` is widened via `setWidth()` to
reserve margin space for the floating extra axes.  This caused it to cover
the entire left margin and intercept all mouse drag events, so dragging on
any Y axis always panned the first signal's ViewBox.

- `_LeftAxis` (the main left axis class) now overrides `mouseDragEvent`.
  At drag start it checks the button-down scene position against each visible
  floating axis's `sceneBoundingRect()`; if a match is found it routes all
  subsequent events of that gesture to the matched ViewBox via
  `vb.mouseDragEvent(event, axis=1)`.  Dragging on the main axis falls
  through to the default behaviour.
- `_LeftAxis._panel` back-reference set in `PlotPanel.__init__` gives the
  override access to `self._extra_axes` at runtime.

**Main axis label changed to selected signal instead of its own signal**

Clicking any row in the signal table called `_set_axis_label` which always
labelled the main left axis from `_current_key` (the selected row).  In
multi-axis mode this overwrote the first signal's label with whatever row
was clicked.

- `_set_axis_label` now branches on `_multi_axis`.  In the multi-axis
  branch the main axis is labelled from the first visible signal with
  `view_box is None` (the signal that actually uses it), regardless of
  which row is selected.  Extra floating axes still take their labels from
  their own signal.  The non-multi-axis (overlay) path is unchanged.

**Unchecking the first signal's "Axis" checkbox collapsed the entire left margin**

`_toggle_axis_visibility` called `plotted.axis.setVisible(False)` for every
signal.  For the first signal this hid the main `PlotItem` layout member,
collapsing the left margin to zero and destroying the space needed by all
floating extra axes.

- `_toggle_axis_visibility` now branches on `plotted.view_box is None`
  (main axis vs floating axis).  When hiding the main axis it blanks visual
  content (`setLabel('')`, `setTextPen(pg.mkPen(None))`,
  `setStyle(showValues=False)`) without calling `setVisible(False)`, keeping
  the layout intact.  Restoring re-applies the signal's label, color, and
  tick visibility.
- `_set_axis_label` also respects `axis_visible=False` for the main axis so
  a selection-change event cannot accidentally re-show a deliberately hidden
  main axis.

---

### Performance — MF4/MDF file loading (2–5× speedup)

**Batch channel reads via `mdf.select()` (biggest win)**

`MDFReader._iter_arrays` previously called `mdf.get()` once per channel.
asammdf stores channels in "channel groups" that share a single compressed
data block; each `mdf.get()` decompressed the entire block and discarded
all channels except one — O(channels_per_group) wasted decompressions.

- Per-group channel specs are now collected from metadata (no I/O), then
  `mdf.select(all_channels, raw=False)` reads the entire group in **one**
  file pass.  A second `mdf.select(enum_channels, raw=True)` pass fetches
  raw integers for enum channels only (skipped for all-numeric groups).
- Falls back to individual `mdf.get()` calls per channel if `select()` raises.
- For groups with many channels the improvement is 5–20×.

**Numeric `disp_list` allocation eliminated**

Pure numeric channels previously called `num_arr.tolist()` — an O(n) Python
list allocation for every channel.  `SignalStore.add_series_bulk` already
supports `has_labels=False` which skips the `raw_values` storage entirely.

- Numeric channels now yield `disp_list = []`.
- `_run_bulk_array` passes `has_labels = len(disp_list) > 0` to
  `add_series_bulk`, skipping the allocation for ~90% of channels.

**Tree rebuild gated on interval + dirty flag**

`_run_bulk_array` previously called `store.build_tree_payload()` (full sort
of all signals) for every channel.  With 200+ channels that is 200+ sorts.

- `tree_update` is now emitted only every `_BULK_PLOT_INTERVAL` (10)
  channels **and** only when `store.is_tree_dirty()`, matching the pattern
  used by the CAN decode path.

**`is_bus_logging()` result cached**

`MDFReader.is_bus_logging()` opens the file header to detect
`CAN_DataFrame.*` channels.  The function is called by `prescan_measurement`,
`dbc_required_for`, and `reader_factory` for the same file — three header
opens before a single byte of signal data is read.

- `MDFReader._bus_logging_cache` (class-level dict) stores the result keyed
  by resolved absolute path.  Subsequent calls for the same path return
  immediately from the cache.

**Enum double-fetch batched**

Enum channels previously made a second `mdf.get(raw=True)` call per channel.
With `select()`-based loading these are now batched: all enum channels in a
group are fetched together in one `mdf.select(enum_channels, raw=True)` call.

---

### Performance — BLF file loading (2–4× speedup on Pass 1)

**Zero-object frame ingestion via `iter_raw_tuples()`**

Pass 1 previously constructed a `RawFrame` dataclass per frame
(8-field object allocation + `bytes(msg.data)` copy + `getattr` calls + type
casts) — repeated 1–2 million times for large BLF files.

- `BLFReaderService.iter_raw_tuples()` yields plain tuples
  `(timestamp, channel_byte, arb_id, dlc, direction_int, is_extended, is_fd, data)`
  directly from python-can `Message` objects with no intermediate objects:
  `channel_byte` pre-computed as `(int(ch)+1) & 0xFF` or `255`; `direction_int`
  as `0/1/2` from `is_rx`; `data` passed as the raw bytearray from python-can.
- `BLFCANReader.iter_raw_tuples()` exposes this to `LoadWorker`.
- `LoadWorker._run_can_raw_vectorized` detects `iter_raw_tuples` via
  `hasattr` and uses it; falls back to the legacy `iter_frames_only` path for
  other reader types.

**Zero-allocation disk write in `RawFrameStore.append_raw()`**

`RawFrameStore.append()` created three temporary `bytes` objects per frame
(conversion → slice → zero-pad concat) × 1–2 M frames.

- New `append_raw()` method uses a single pre-allocated `bytearray(64)` write
  buffer (`self._write_buf`).  Each frame: `buf[:] = _ZEROS_64` (C-level
  64-byte memcopy of a module constant), then `buf[:n] = data[:n]` (C-level
  payload copy) — zero Python object allocations per frame.
- `append_raw()` also skips the name-table dict lookup (always `nid=0` in the
  2-pass path) and the string direction comparison (accepts `direction_int`
  directly), saving additional per-frame Python work.

**`note_frame()` removed from Pass 1 hot loop**

`store.note_frame()` ran 6 operations per frame (counter increment,
`set.add`, dict increment, conditional, f-string, list append) for every
frame in Pass 1.

- The hot loop now only calls `rfs.append_raw()` and the progress emit.
- After `rfs.seal()`, `_bulk_compute_store_stats(store, rfs)` replicates
  the equivalent bookkeeping in a single numpy pass:
  `np.unique` on the channel array for channel set + frame counts; a 20-
  iteration Python loop for `first_frame_ids`.  Pass 2 still overwrites
  `decoded_frames` and `unmatched_frames` as before.

**Choices label lookup vectorised (Pass 2)**

DBC enum signals previously built `disp_list` with a Python `for v in
numeric_arr` loop — up to 500 K iterations for common status signals.

- For the common DBC case (non-negative integer keys, max key < 65 536) a
  numpy LUT is built once from the choices dict (`lut[k] = str(v)`,
  `lut_valid[k] = True`), then the per-sample work is done entirely via numpy
  fancy indexing: `lut[safe_idx[has_val]]`.  No Python loop over samples.
  A Python loop fallback is kept for non-integer keys or very large key values.

---

## [v00.00.43] — 2026-05-12

### Changed — Diagnostics rule syntax redesigned (expression engine)

**Rule format is now a plain condition expression string**

The previous YAML rule system (three separate rule types: `fault_signal`,
`range_check`, `message_loss`, with `signal_map` and dict-based operator
syntax) has been replaced by a single unified expression engine.

- `condition:` is now the only required field per rule.  Everything else
  (`id`, `title`, `severity`, `description`, `suggested_action`,
  `plot_signals`, `enabled`) is optional.
- Supported operators: `>` `<` `>=` `<=` `=` `!=`
- Boolean connectors: `and` / `or` (left-to-right, no parentheses required
  for simple rules).
- Signal names inside the condition are matched **case-insensitively** against
  the full `CH<n>::<MsgName>::<SigName>` store key.  A partial name is enough.
  Rules for absent signals are silently skipped.
- `signal_map` block is no longer needed or used (accepted but ignored for
  backward compatibility).
- `type:` field removed — all rules are expression rules.
- `context_window_s:` domain-level key controls how many seconds of data
  before/after each fault are captured for AI diagnosis context (default 2.0 s).

**`config/diagnostics/README.md` fully rewritten** to document the current
expression syntax, all optional fields, severity aliases, and `plot_signals`
behaviour.

---

## [v00.00.42] — 2026-05-11

### Added — Multi-axis Y-axis spacing and per-axis visibility toggle

**Proper axis spacing in Multi-Axis mode**

Previously all extra Y axes were positioned at a fixed `55 × idx` offset from
the viewport left edge, causing every axis to stack on top of the others.

- `_update_multi_axis_views` now assigns axes to consecutive **slots** rather
  than fixed index offsets: each visible axis occupies its own non-overlapping
  panel (55 px wide, 4 px gap between panels).
- `_rebuild_overlay` expands the left layout margin via
  `plot.getAxis('left').setWidth(55 + n_visible × 59)` so the viewport shifts
  right and makes room — extra axes are never clipped or overlapping the data area.
- Hidden axes are parked off-screen (`x = −9999`) so no blank gap is left in
  the axis area.

**"Axis" column in the signal table (Multi-Axis mode only)**

- Signal table expanded from 5 to 6 columns; column 5 is **"Axis"** (36 px fixed,
  checkbox only).
- The column is hidden in Normal and Stacked modes; it becomes visible
  automatically when Multi-Axis is enabled.
- Unchecking an axis checkbox hides that signal's Y axis and frees its layout
  slot — the plot area expands horizontally to fill the recovered space.
- Re-checking restores the axis in the next available slot.
- The `axis_visible` state is persisted through undo snapshots and restored
  correctly after mode switches and curve rebuilds.
- `PlottedSignal` gains `axis_visible: bool = True` field (slot-safe).

### Fixed

- **Plot area did not expand when axes were hidden** — `setWidth` was computed
  once at build time for all extra axes (visible or not).  `_toggle_axis_visibility`
  now recomputes the required margin from currently-visible axes only and calls
  `setWidth` immediately so the layout shrinks and the viewport grows.

---

## [v00.00.41] — 2026-05-11

### Added — Fault auto-plot, zoom to trigger point

- **Fault detection auto-plot** — when a diagnostic rule fires, the signals
  listed under `plot_signals:` in the YAML rule are automatically added to the
  main plot and the view zooms to the trigger point (0→1 transition) with a
  ±0.5 s margin.  Signal list is fully configurable per rule.
- **`plot_signals:` field in rule YAML** — list signal names to auto-plot when
  a fault fires.  Names are matched case-insensitively against the loaded
  measurement.  If omitted, falls back to the fault signal itself.
- **`MainWindow.plot_finding(finding)`** — adds signals and calls
  `plot_panel.zoom_to_time(t0, t1)`.
- **`PlotPanel.zoom_to_time(t_start, t_end, margin=0.5)`** — zooms X to
  `[t_start − margin, t_end + margin]` and calls `fit_vertical()` to rescale Y.
  Works in all three plot modes (Normal, Multi-Axis, Stacked).
- **`DiagnosticContext.resolve_signal_key(name)`** — silent case-insensitive
  lookup that returns the full store key without writing to the progress log.
- **`Finding.plot_signals`** — new field (`list[str]`) carrying resolved store
  keys for the signals to auto-plot.

### Fixed — Cursor drag responsiveness

- **`_nearest_index`** replaced Python `while` binary-search loop with
  `np.searchsorted` (C-level, O(log n)).
- **`_row_lookup`** — key → table-row mapping rebuilt once in `_refresh_table`
  and cached; `_update_table_values` uses it directly (eliminates O(rows) scan
  on every drag event).
- **`SignalProxy` removed** — direct `scene.sigMouseMoved.connect(slot)`
  replaces `pg.SignalProxy(rateLimit=60)`, eliminating up to 16 ms of
  artificial throttle.  `_mouse_moved` signature changed from `(event: tuple)`
  to `(pos)` to match the direct signal.
- **`setUpdatesEnabled(False/True)`** wraps `_update_table_values` so the
  entire cursor-value column update triggers one repaint instead of one per cell.

### Fixed — Show/hide data points hang on large files (>150 MB)

- **Style-only update path** — `_apply_curve_style(set_data=False)` uses
  per-property setters (`setPen`, `setSymbol`, `setSymbolSize`, etc.) instead
  of `setData(full_arrays)`.  The data arrays are never re-read; toggle is instant.
- **`setClipToView(True)` + `setDownsampling(auto=True, method='peak')`** added
  to every new `PlotDataItem` via `_configure_curve()`.  Only viewport-visible
  samples are rendered; out-of-view samples are skipped by pyqtgraph.
- **`np.asarray` instead of `np.frombuffer`** in the data-set path — zero-copy
  via buffer protocol and returns a writable array as required by pyqtgraph
  internals (`np.frombuffer` returns read-only).

---

## [v00.00.40] — 2026-05-07

### Added — DBC Manager channel pre-scan

- **Measurement pre-scan on file open** — when a BLF/ASC/MDF file is selected,
  a lightweight scan reads up to 50 000 frames (~< 1 s) to extract CAN channel
  numbers and arbitration IDs *before* Load+Decode.  The DBC Manager now shows
  the real channels from the measurement file instead of a hardcoded [1, 2].

- **Channel info banner in DBC Manager** — shows "Channels detected: CAN 1,
  CAN 2, …" or a hint to load a measurement file first.

- **`prescan_measurement()` in `core/readers/__init__.py`** — new public
  function reusable by any caller that needs channel/ID data before decoding.

### Changed

- **`_collect_channel_data()` priority chain** — now checks (1) RawFrameStore,
  (2) SignalStore, (3) pre-scan cache — so the DBC Manager always has real
  channel data regardless of decode state.

- **DBC Manager fallback removed** — dropdown no longer shows phantom CAN 1/2
  when no measurement is loaded; "All Channels" remains available as default.

---

## [v00.00.39] — 2026-05-07

### Added — Vectorized BLF/ASC decode (30× faster)

- **Two-pass vectorized decode pipeline** — Pass 1 drains all raw frames into
  RawFrameStore (no DBC decode); Pass 2 groups frames by (channel, arb_id)
  using numpy argsort/diff and applies numpy bit-extraction across entire
  groups at once.  ~30× faster than per-frame cantools decode.

- **`core/vectorized_decoder.py`** — `SignalExtractor` performs little-endian
  bit extraction (shifts, masks, sign-extension, scale+offset) fully vectorized
  across N frames.  `MessageVectorDecoder` verifies vectorized output vs
  cantools on the first frame; any mismatch permanently falls back to per-row
  cantools.  Big-endian and multiplexed signals use cantools fallback.

- **`SignalStore.add_series_bulk()`** — inserts a complete channel time-series
  via `array.array.frombytes(ndarray.tobytes())` — single C-level memcopy,
  no per-sample Python loop.

### Changed

- **Cached message series refs** — `_msg_series_cache[(channel, msg_name)]`
  caches ordered `list[SignalSeries]` so the hot loop avoids string-key
  construction and dict lookups per signal sample.

- **Selective `raw_values`** — `has_labels` flag on `SignalSeries` skips
  `raw_values.append()` for ~90% of signals (plain numeric), saving hundreds
  of MB on large files.  `display_value_at(idx)` falls back to `values[idx]`.

- **Delta-only tree updates** — `build_tree_payload()` only called when
  `_tree_dirty` flag is set (new signals seen since last emit).

---

## [v00.00.37] — 2026-05-07

### Fixed — BLF decode performance for large files (>20 MB)

- **Per-channel DBC config in vectorized path** — the two-pass vectorized
  decoder now resolves the correct DBC per channel using `ChannelConfig`,
  matching the per-row path behaviour.

- **Choices lookup built before decode** — `store.set_choices_lookup()` is
  called with the full (channel, msg_name, sig_name) → choices map before
  the vectorized decode loop, so `has_labels` is correctly set on first
  series creation.

---

## [v00.00.36] — 2026-05-07

### Fixed — Raw Frame DBC decode with per-channel config

- **`dbc_required_for(path)`** now probes MDF files instead of returning
  a hardcoded value — correctly routes bus-logging MDF to the DBC path.

- **`iter_frames_only()` on ASCCANReader** — refactored so the two-pass
  vectorized path works for ASC files (was previously only on BLFCANReader).

---

## [v00.00.35] — 2025-04-25

### Fixed — Cursor columns and timestamp normalisation (v00.00.33/34 regressions)

- **Cursor 2 column hidden after enabling** — `set_cursor2_enabled(True)` was
  calling `setColumnHidden(2, False)` (old column index).  Fixed to col 3.

- **Cursor 1 values written to wrong column** — `_on_cursor1_moved` and
  `_on_stacked_c1_moved` were calling `_update_table_values(x, col=3)`.
  This wrote cursor 1 values into the hidden cursor 2 column, making them
  invisible.  Root cause: the column-index replacement chain in v00.00.33
  ran `col=1→col=2` then immediately `col=2→col=3`, cascading both steps
  onto cursor 1 lines.  Fixed to `col=2` for cursor 1, `col=3` for cursor 2.

- **Time axis showing large negative values (~−1.29×10⁹ s)** — double
  timestamp normalisation introduced in v00.00.33.  In the new multi-DBC
  `_run_can_raw` loop, `frame.timestamp -= base_ts` normalised the frame
  first, then `decoder.decode_frame(frame)` created samples using the
  already-normalised timestamp, then `s.timestamp -= base_ts` subtracted
  base_ts a second time — giving `original_ts − 2×base_ts` ≈ −1.29×10⁹.
  Fixed by removing the redundant per-sample subtraction; a comment explains
  why it must not be re-added.

---

## [v00.00.34] — 2025-04-25

### Fixed — Signal grouping and visibility (v00.00.33 bugs)

- **Group name not visible** — the group name was placed in col 0 (the
  26px checkbox column) and truncated to `...`.  Fixed: col 0 now shows
  only the ▶/▼ collapse arrow; the group name is rendered in col 1
  (Signal column) in bold light-blue (`#8ab8e0`), clearly readable.
  Double-clicking either col 0 or col 1 of a group header opens the rename
  dialog.

- **Unchecking a signal removed cursors from all signals** — visibility
  toggle called `_rebuild_curves()` which tears down every curve and cursor
  line and recreates them from scratch.  C1/C2 lines were lost for all
  signals.  Fixed with `_apply_visibility()`:
  - **Normal / multi-axis mode:** calls `curve.setVisible(False/True)` on
    the existing `PlotDataItem` — cursor lines are completely untouched.
  - **Stacked mode:** still calls `_rebuild_curves()` because stacked rows
    must be physically added/removed, but this is the expected behaviour
    (stacked has no persistent cursors between rows anyway).

---

## [v00.00.33] — 2025-04-25

### Added — Signal grouping and visibility toggle

**Signal table now has 5 columns:**
`[☑] | Signal | Cursor 1 | Cursor 2 | Unit`

**Visibility checkbox (column 0)**
- Checked (default) — signal curve plotted normally.
- Unchecked — curve removed from plot; in stacked mode the entire row is
  removed; cursor table hides the value for that signal.
- Group checkbox (right side of group header, col 4) toggles all signals
  in the group at once — tri-state: all checked / mixed / all unchecked.
- Fit to Window only considers visible signals for Y-range calculation.
- Visibility persisted in config JSON alongside color.

**Signal groups**
- Select one or more signals → right-click → **Group selected…** → type name.
- Group header row appears with a collapse/expand arrow (▶/▼) in bold.
- Click the arrow to collapse/expand — visual only, plot is unchanged.
- Double-click the group name to rename inline.
- Right-click group header → **Rename** or **Ungroup** (signals return to
  flat list).
- Groups saved/restored in config JSON.

**Implementation notes**
- `PlottedSignal` gains `visible: bool = True` and `group: str = ''` fields.
- `_rebuild_overlay` skips invisible signals; `_rebuild_stacked` builds its
  ordered list from visible signals only.
- `_refresh_table` interleaves group header rows between signal rows;
  `_apply_collapse_state` shows/hides rows after each table build.
- `_on_table_item_changed` handles checkbox state changes with undo support.
- Config backwards-compatible: old `signals: [key, ...]` format still loads.

---

## [v00.00.32] — 2025-04-25

### Added — MDF4 bus logging support (raw CAN frames in MDF)

MDF4 files recorded with CANoe, CANedge, or any ASAM MDF bus logger
store raw CAN frames using ``CAN_DataFrame.*`` channels rather than
pre-decoded engineering values.  These files now follow the full BLF/ASC
workflow in CAN Scope — DBC required, full RawFrameStore, CAN Trace dialog.

**Auto-detection — no user action required:**
- On file open, ``MDFReader.is_bus_logging()`` probes channel group metadata
  for ``CAN_DataFrame.*`` channels (< 50 ms, no data read).
- Bus logging MDF → routed to ``MDFCANReader`` (DBC required, same pipeline
  as BLF/ASC, CAN Trace available).
- Pre-decoded MDF → routed to existing ``MDFReader`` (no DBC, vectorised
  fast path unchanged).
- Status bar shows *"MDF bus log detected — DBC required"* when appropriate.

**New: ``core/readers/mdf_can_reader.py`` — MDFCANReader**
- Uses ``python-can``'s ``MF4Reader`` which wraps asammdf internally and
  yields ``can.Message`` objects — identical interface to BLFReader/ASCReader.
- ``iter_with_frames()`` constructs ``RawFrame`` objects and calls
  ``DBCDecoder.decode_frame()`` per frame — no changes to LoadWorker,
  RawFrameStore, or DBCDecoder needed.
- ``BusChannel`` field from MDF is already 1-indexed (ASAM convention) —
  no +1 adjustment unlike BLF/ASC.
- ``decoder`` property exposed for on-demand signal decode in CAN Trace.

**Changed: ``core/readers/__init__.py``**
- ``dbc_required_for(path)`` now probes MDF files instead of returning
  hardcoded False.
- ``reader_factory()`` routes MDF4 files through the probe; raises a clear
  ``ValueError`` with instructions if bus logging MDF opened without DBC.

**Channel config (DBC Manager) works identically** for bus logging MDF —
the same ``ChannelConfig`` / per-channel decoder used for BLF/ASC applies.

**Requirements:** ``python-can >= 4.6`` (already required) + ``asammdf >= 7.0``
(already required).  The ``canmatrix`` package may be needed by python-can's
MF4Reader internals — noted in ``requirements.txt``.

---

## [v00.00.31] — 2025-04-25

### Fixed — DBC Manager match quality: J1939 PGN fallback

Exact ID matching alone gave near-0% match for J1939 files because
ECUs broadcast on different source addresses than the DBC template value
(e.g. DBC defines `0x18FEBD00`, file contains `0x18FEBDFE`).
Result: "3 / 549 IDs" at 0% for a fully compatible DBC.

`_compute_match` now uses a two-pass strategy (match display only — actual
decode via DBCDecoder remains exact-match):

1. **Pass 1 — Exact 29-bit ID match** (standard and J1939 specific-source)
2. **Pass 2 — J1939 PGN fallback** for IDs that didn't match exactly.
   For extended frames where `PF >= 0xF0` (J1939 PDU2 format), the
   source address byte is stripped and only the PGN (`bits 8–23`) is
   compared.  `0x18FEBD00` and `0x18FEBDFE` both yield PGN `0xFEBD` →
   counted as a match.

The match bar now shows realistic percentages for J1939 files with
mixed source addresses.

---

## [v00.00.30] — 2025-04-25

### Fixed — DBC Manager Refresh Match not working

Root cause: `_ids_per_channel` was captured once at dialog open time.
Clicking "↻ Refresh Match" called `_refresh_all_matches()` but re-used the
same empty dict — so match quality stayed 0/0 regardless.

Fix: `data_provider` callable pattern.

- `MainWindow._collect_channel_data()` — new reusable method that reads
  channel numbers and per-channel arb-ID sets from `raw_frame_store`
  (most accurate) or `store.channels` (fallback).
- `DBCManagerDialog` accepts an optional `data_provider` kwarg (a callable
  returning `(channels, ids_per_channel)`).
- `_refresh_all_matches()` calls `self._data_provider()` first to fetch
  **live** data from the main window at refresh time.  This works even if
  decode completed after the dialog was opened.
- New channels discovered on refresh are added to each row's channel dropdown.
- If still no measurement data, a clear message is shown:
  *"Load and decode a BLF/ASC measurement first, then click Refresh Match."*

---

## [v00.00.29] — 2025-04-25

### Fixed — DBC Manager: channel list and match quality

- **Only one channel shown on first open** — `channels_in_file` was built
  only from `raw_frame_store`, which is `None` before a file is decoded.
  `choose_dbc` now collects channels from three sources in priority order:
  1. `raw_frame_store` arrays (most accurate — per-channel arb IDs available)
  2. `store.channels` set (available after any decode, including MF4/CSV)
  3. Fallback: `[1, 2]` minimum so the user can still assign DBCs before
     any measurement is loaded.
- **Match quality bar always 0% after loading a DBC** — `_compute_match`
  ran once at row creation time but `_ids_per_channel` was empty when the
  dialog opened before a file was decoded.  Two fixes:
  - `_refresh_all_matches()` — new method that recomputes match quality and
    updates each row's bar and auto-suggest channel dropdown.  Called
    automatically when the dialog opens if `ids_per_channel` is non-empty.
  - **"↻ Refresh Match" button** — manual trigger so the user can re-run
    the match computation at any time (e.g. after loading a new measurement
    while the dialog is still open, or after adding a DBC via "+ Add DBC").
  - `_bar` and `_match_lbl` are now stored as instance attributes on
    `_DBCRow` so `update_match(pct, text)` can update them externally.

---

## [v00.00.28] — 2025-04-25

### Added — Multi-DBC channel configuration

**New: `core/channel_config.py` — ChannelConfig**
- Maps CAN channel numbers to DBC file paths.  Separates the "vehicle
  configuration" (which DBC goes on which bus) from the "measurement session"
  (which file to analyse + which signals to plot).
- `ALL_CHANNELS_KEY = 0` — assign a DBC as fallback for any channel not
  explicitly configured.
- DBCDecoder instances are created lazily and cached per DBC path — the same
  DBC shared across two channels reuses one decoder (saves RAM + parse time).
- Saved as a standalone `.canscope_ch` JSON file; loadable independently of
  the session config.
- `ChannelConfig.from_single_dbc(path)` — backward-compatible factory for
  the old single-DBC workflow.

**New: `gui/dbc_manager.py` — DBC Manager dialog**
- Replaces the single "Open DBC file" picker.
- Shows one row per DBC with: file name, channel assignment dropdown,
  match quality bar (% of DBC IDs seen in the measurement), and match
  count label (e.g. "34 / 37 IDs").
- Channel assignment is **auto-suggested**: the channel with the most
  matching IDs is pre-selected — user only needs to correct mistakes.
- "All Channels" option in each dropdown for DBC files covering multiple buses.
- **Save Channel Config…** / **Load Channel Config…** buttons save/load the
  mapping as a `.canscope_ch` file, independent of the session config.

**Changed: `core/load_worker.py`**
- Accepts `ChannelConfig` instead of a single `dbc_path`.
- Per-frame decode: `cfg.decoder_for(frame.channel)` selects the right
  DBCDecoder before calling `decode_frame()`.
- All decoders are pre-built before the decode loop (`build_all_decoders()`)
  so no frame pays the DBC parse cost.

**Changed: `gui/main_window.py`**
- `self.channel_config: ChannelConfig` persists between measurement loads —
  loading a new BLF file does **not** clear the DBC mapping.
- "Open DBC" button opens the DBC Manager dialog instead of a plain picker.
- `save_configuration` includes the full channel config in JSON.
- `load_configuration` restores it; falls back to `from_single_dbc` for
  configs saved by older versions (backward compatible).
- "Open DBC" is now always enabled (previously greyed until a file was selected).

**Workflow for repeated measurements:**
  1. Load any measurement, click Open DBC → configure channel mapping once.
  2. Click Save Channel Config → `MyVehicle.canscope_ch`.
  3. Load next measurement → DBC mapping is already in memory, click
     Load + Decode immediately.
  4. Or: Load Channel Config to restore a previously saved mapping.

---

## [v00.00.27] — 2025-04-24

### Added
- **Application icon** — `resources/app_icon.ico` (multi-resolution:
  256 / 128 / 64 / 48 / 32 / 16 px, all embedded in one ICO file) and
  `resources/CANScope_ICON.png` (full 1254×1254 RGBA source).
  Both files are bundled in the portable EXE via `CANScope.spec`.
  At runtime `app.py` loads the PNG first (sharper on HiDPI displays),
  falling back to the ICO.  The ICO is embedded in the EXE binary by
  PyInstaller so Windows Explorer, the taskbar, and Alt+Tab all show
  the icon without the app being running.

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
