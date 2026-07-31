# PR Policy
 This document This document meant to communicate The files and structures that are protected, Basic functionality and feature of this tool is not altered.

## What NOT to Modify Without permission:
Protected loading/decoding files and areas:

- `core/load_worker.py`
- `core/channel_config.py`
- `core/dbc_decoder.py`
- `core/vectorized_decoder.py`
- `core/raw_frame_store.py`
- `core/blf_reader.py`
- `core/readers/__init__.py`
- `core/readers/base.py`
- `core/readers/db_format.py`
- `core/readers/blf_can_reader.py`
- `core/readers/asc_can_reader.py`
- `core/readers/mdf_reader.py`
- `core/readers/mdf_can_reader.py`
- The **Load + Decode**, pre-scan, reader selection, worker wiring, progress,
  partial-update, completion, signal-tree handoff, and CAN Trace behavior in
  `gui/main_window.py`
- Loading/decoding dependency versions in `requirements.txt`

Protected behavior includes format detection, channel numbering, timestamp
normalization, DBC/ARXML selection, native asammdf extraction, ASC direct-array
parsing, BLF batched extraction, vectorized decoding, multiplexed-signal
filtering, bulk SignalStore insertion, RawFrameStore/CAN Trace construction,
fallback paths, decoded signal/sample counts, and loading progress messages.

Read `docs/Project_structure.md` for the accepted loading/decoding architecture.

## What NOT to Modify Without Owner's permission:
- `CANScope.spec` — PyInstaller build spec; modifying breaks the portable build.
- The `APP_NAME` constant in `app.py` — affects window title and branding.

## Owner-only: release metadata
- `CHANGELOG.md` and `APP_VERSION` in `app.py`. The owner writes both at release
  time, and `tools/check_version_consistency.py` requires them to agree with the
  release tag. Contributors leaving them alone also keeps concurrent PRs from
  colliding on the same two lines.

## Owner-only: CI, release, and policy enforcement
- `.github/` — workflows, `CODEOWNERS`, and the PR template.
- `tools/` — the protected-path guard, the version check, and the git hooks.

`build.yml` publishes releases and runs with `contents: write` when a `v*.*.*`
tag is pushed. Only the owner can push that tag, but a merged PR that changed
the workflow would get to run its own steps under the owner's next release,
holding the release token — so the tag being owner-only is not by itself enough.

`tools/` is protected for the same reason one step removed: the guard cannot be
relied on to police edits to itself. Without this rule a PR could delete entries
from `tools/check_protected_paths.py` and the guard would pass its own diff.

`CODEOWNERS` lists both paths, but it only *requests* review. "Require review
from Code Owners" is deliberately not enabled — with a single owner, GitHub's
ban on self-approval would block the owner's own PRs. CI is therefore the only
enforcement point, which is why these paths are in the guard and not just in a
review rule.

If a change here is genuinely needed, open an issue first. The owner either
makes the change directly or applies the `approved-pipeline-change` label to
release the check.