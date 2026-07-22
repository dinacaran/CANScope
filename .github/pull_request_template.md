## What changed and why

<!-- One or two sentences. Link the issue if there is one: Fixes #123 -->

## How it was tested

<!-- Which file did you load? BLF / ASC / MF4 / MDF / CSV? What did you check? -->

## Checklist

- [ ] `python -m pytest tests/` passes locally
- [ ] `python app.py` launches and loads a real measurement file with no regression
- [ ] No files from the protected list in `CLAUDE.md` are touched
      (loading/decoding pipeline, `core/signal_store.py`, `CANScope.spec`, `requirements.txt`)
- [ ] `APP_VERSION` in `app.py` and `CHANGELOG.md` are **not** modified —
      the owner updates those at release time
- [ ] Screenshot attached below if anything in the UI changed

## Screenshot

<!-- Required for UI changes. There is currently no automated UI regression
     test beyond tests/test_plot_view_preserve.py, so a before/after image is
     the only practical review signal. Delete this section if not applicable. -->
