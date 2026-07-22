canscope/
├── app.py                        # Entry point, APP_NAME="CAN Scope", APP_VERSION
├── config/
│   └── diagnostics/
│       ├── motor_control.yaml    # Fault rules for motor/inverter domain (user-editable)
│       └── README.md             # Rule authoring guide
├── core/
│   ├── models.py                 # RawFrame, DecodedSignalSample dataclasses
│   ├── channel_config.py         # ChannelConfig: {channel → DBC or ARXML}, decoder cache, save/load .canscope_ch
│   ├── load_worker.py            # QThread: native MDF arrays + batched CAN-raw vectorized decode paths
│   ├── signal_store.py           # SignalStore, SignalSeries (array.array storage)
│   ├── raw_frame_store.py        # Batched CAN Trace store: compact metadata + 64 B/frame mmap payload
│   ├── dbc_decoder.py            # DBCDecoder with 3-level cache — accepts .dbc and .arxml
│   ├── vectorized_decoder.py     # NumPy DBC decode, sparse multiplex filtering, cantools fallback
│   ├── blf_reader.py             # python-can BLF decompression into packed column batches
│   ├── export.py                 # CSV export
│   ├── readers/
│   │   ├── __init__.py           # reader_factory() + dbc_required_for() — format detection
│   │   ├── db_format.py          # SUPPORTED_DB_SUFFIXES, is_database_file(), db_format_label()
│   │   ├── base.py               # MeasurementReader protocol
│   │   ├── blf_can_reader.py     # BLF packed-batch + DBC/ARXML vectorized pipeline
│   │   ├── asc_can_reader.py     # Direct classic CAN/CAN-FD ASC column-array parser
│   │   ├── mdf_reader.py         # Pre-decoded MDF batched arrays + bus-logging probe
│   │   ├── mdf_can_reader.py     # Native asammdf bus extraction; python-can raw fallback
│   │   └── csv_reader.py         # Wide and narrow CSV
│   └── diagnostics/              # AI-powered diagnostics engine (Ctrl+Shift+A)
│       ├── __init__.py
│       ├── config_loader.py      # YAML domain parser — infers rule type, auto-generates id/title
│       ├── context.py            # DiagnosticContext — read-only SignalStore adapter for rule processors
│       ├── engine.py             # DiagnosticEngine — orchestrates rule runs and evidence building
│       ├── evidence.py           # EvidenceBuilder — reduces large data to <5 KB LLM snippets
│       ├── models.py             # Finding, Severity, AnalysisResult dataclasses
│       ├── rules/
│       │   ├── __init__.py       # RULE_PROCESSORS dispatch table
│       │   ├── expression.py     # Free-form condition evaluation (>, <, =, !=, and, or)
│       │   ├── fault_signal.py   # fault_when operator evaluation
│       │   ├── range_check.py    # min/max boundary check
│       │   └── message_loss.py   # Gap detection between CAN samples
│       └── llm/
│           ├── __init__.py
│           ├── client.py         # GitHubModelsClient — streaming OpenAI-compatible REST
│           └── prompts.py        # Analysis and chat follow-up prompt builders
├── gui/
│   ├── main_window.py            # MainWindow, toolbar, config save/load, plot_finding()
│   ├── plot_widget.py            # PlotPanel: normal / multi-axis / stacked, dual cursors, zoom_to_time()
│   ├── signal_tree.py            # SignalTreeWidget with live search
│   ├── raw_frame_dialog.py       # Sliding-window CAN Trace (RawFrameStore, no cap)
│   ├── dbc_manager.py            # Database Manager dialog: per-channel DBC/ARXML, match quality bars
│   ├── splash.py                 # CANScopeSplash — minimisable, taskbar-visible splash screen
│   └── diagnostics/              # Diagnostics UI (non-modal window)
│       ├── activation.py         # Wires Ctrl+Shift+A shortcut in MainWindow
│       ├── window.py             # DiagnosticsWindow — domain selector, run controls, auto-plot on fault
│       ├── findings_panel.py     # Left panel — severity-coloured finding list + details pane
│       ├── chat_panel.py         # Right panel — streaming LLM chat with status label
│       └── worker.py             # AnalysisWorker / LLMWorker (background thread helpers)
├── resources/
│   ├── splashscreen.png          # 1635 × 962 splash image
│   ├── CANScope_ICON.png         # 1254 × 1254 app icon source
│   └── app_icon.ico              # Multi-resolution ICO (256/128/64/48/32/16 px)
├── requirements.txt
├── CANScope.spec                 # PyInstaller spec, bundles resources/ and config/
└── .github/workflows/build.yml  # Auto-build on v*.*.* tag push


## Validated loading and decoding architecture — protected

The BLF, ASC, MF4, and MDF loading/decoding implementation described below is
fully tested and accepted. It must not be changed without explicit permission
from the project owner. See `AGENTS.md` for the complete protected-file list.

### Entry and format selection

1. `gui/main_window.py` owns file selection, lightweight channel/ID pre-scan,
   database configuration, and the **Load + Decode** worker lifecycle.
2. `core/readers/__init__.py` detects the measurement format, distinguishes
   pre-decoded MDF from MDF bus logging, enforces DBC/ARXML requirements, and
   constructs the correct reader.
3. `core/channel_config.py` maps physical CAN channels to DBC/ARXML files and
   caches decoders. Channel `0` is the all-channels fallback.

### LoadWorker dispatch

`core/load_worker.py` is the central background pipeline and selects exactly
one accepted path:

- **MF4/MDF bus logging:** `MDFCANReader.iter_decoded_channel_arrays()` calls
  `asammdf.MDF.extract_bus_logging()` once and bulk-imports decoded NumPy
  arrays. It preserves channel/message metadata and value-to-text conversion.
  The older raw-frame vectorized path remains a compatibility fallback.
- **Pre-decoded MF4/MDF:** `MDFReader.iter_channel_arrays()` batches channels
  by MDF channel group and imports arrays directly without a DBC.
- **ASC + DBC/ARXML:** `ASCCANReader.iter_raw_batches()` parses classic CAN and
  CAN-FD ASC text directly into packed column batches without allocating a
  `can.Message` or `RawFrame` per record.
- **BLF + DBC/ARXML:** `BLFReaderService.iter_raw_batches()` uses python-can for
  BLF container decompression and emits packed column batches without
  per-frame tuples at the LoadWorker boundary.

### CAN-raw storage and decode

1. `RawFrameStore.append_raw_batch()` bulk-appends compact metadata and writes
   packed 64-byte payload records. After sealing, the payload file is memory
   mapped for CAN Trace access and vectorized decoding.
2. `VectorizedDBC` groups frames by `(channel, arbitration_id)` and performs
   NumPy signal extraction. Simple little-endian multiplexing is vectorized;
   inactive multiplex branches and failed rows are not inserted as samples.
   Unsupported layouts use the verified cantools fallback.
3. `SignalStore.add_series_bulk()` receives complete timestamp/value arrays.
   Do not alter `core/signal_store.py` as part of loading/decoding work without
   separate explicit permission.
4. The final tree payload and partial/completion signals are delivered to the
   GUI only through the existing LoadWorker signal wiring.

### Accepted behavioral invariants

- Physical CAN channel numbering remains consistent across BLF, ASC, and MDF.
- All decoded timestamps share one recording-wide zero origin.
- DBC/ARXML channel-specific mappings and all-channel fallback semantics remain
  unchanged.
- MF4/MDF native extraction, ASC direct-array parsing, BLF packed batches,
  sparse multiplex handling, fallback behavior, and progress logging remain
  unchanged.
- BLF/ASC continue to populate CAN Trace through `RawFrameStore`.
- MF4/MDF native fast loading does not materialize CAN Trace; its compatibility
  fallback behavior remains unchanged.
- Signal names, message names/IDs, units, enum display values, decoded sample
  counts, and tree hierarchy must remain stable for the validated fixtures.
