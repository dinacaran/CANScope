"""
Adaptive data-point display in PlotPanel.

"Show Data Points" must never feed the full multi-million-sample array to a
scatter. Points are drawn on a dedicated ScatterPlotItem that only ever holds
the samples visible in the current X viewport, and only when that count is at
or below PlotPanel._POINTS_VISIBLE_THRESHOLD. These tests drive
_update_adaptive_points() directly (bypassing the debounce timer) and assert
the scatter's point count in each layout mode.

Requires a Qt platform plugin; skipped (not failed) if none is available.
"""
from __future__ import annotations

import numpy as np
import pytest


# A moderately large monotonic series — big enough that a full-extent view is
# far above the threshold, small enough to keep the test fast.
_N = 200_000
_TS = np.linspace(0.0, 2000.0, _N)
_VS = np.sin(_TS * 0.5)


def _make_series(name: str, offset: float = 0.0, unit: str = ""):
    from core.signal_store import SignalSeries
    return SignalSeries(
        channel=1, message_name="Msg", message_id=0x100,
        signal_name=name, unit=unit,
        timestamps=_TS, values=_VS + offset, raw_values=[], has_labels=False,
    )


def _scatter_len(plotted) -> int | None:
    sc = plotted.scatter
    if sc is None:
        return None
    x, _y = sc.getData()
    return 0 if x is None else len(x)


def _set_xrange(panel, x0, x1):
    if panel._stacked_mode:
        panel._stacked_plots[0].setXRange(x0, x1, padding=0)
    else:
        panel.plot.setXRange(x0, x1, padding=0)


def _populate(panel, qapp):
    panel.add_series("A", _make_series("A", unit="rpm"))
    panel.add_series("B", _make_series("B", offset=3.0, unit="V"))
    qapp.processEvents()


@pytest.mark.parametrize("mode", ["normal", "multi_axis", "stacked"])
def test_line_curves_clip_and_downsample(panel, qapp, mode):
    """Every rendered line curve must keep clipToView + autoDownsample on.

    Regression guard: PlotItem.addItem() overwrites an item's clip/downsample
    with the PlotItem's own (off) defaults, so _configure_curve must run AFTER
    the curve is added. If that ordering regresses (as it historically did in
    stacked mode), a zoomed-in view renders every sample and the plot crawls on
    large files.
    """
    if mode == "multi_axis":
        panel.set_multi_axis(True)
    elif mode == "stacked":
        panel.set_stacked(True)
    _populate(panel, qapp)

    # Zoom into a narrow window: with clip on, the displayed dataset must be a
    # tiny fraction of the full series, not the whole thing.
    span = (_TS[-1] - _TS[0]) * (400.0 / _N)
    x0 = _TS[_N // 2]
    _set_xrange(panel, x0, x0 + span)
    qapp.processEvents()

    for plotted in panel._items.values():
        curve = plotted.curve
        assert curve.opts["clipToView"] is True
        assert curve.opts["autoDownsample"] is True
        disp = curve.getData()[0]
        n_disp = 0 if disp is None else len(disp)
        # Clipped to the visible window (+ a small downsample margin), nowhere
        # near the full _N samples.
        assert n_disp < _N // 100, f"{mode}: {n_disp} pts displayed — clip not applied"


@pytest.mark.parametrize("mode", ["normal", "multi_axis", "stacked"])
def test_adaptive_points_per_mode(panel, qapp, mode):
    if mode == "multi_axis":
        panel.set_multi_axis(True)
    elif mode == "stacked":
        panel.set_stacked(True)
    _populate(panel, qapp)

    panel.set_show_points(True)
    qapp.processEvents()

    # Zoomed OUT to full extent → far above threshold → scatter empty (line only)
    _set_xrange(panel, _TS[0], _TS[-1])
    panel._update_adaptive_points()
    qapp.processEvents()
    assert _scatter_len(panel._items["A"]) == 0
    assert _scatter_len(panel._items["B"]) == 0

    # Zoomed IN to a narrow window → scatter holds exactly the sliced count
    span = (_TS[-1] - _TS[0]) * (400.0 / _N)   # ~400 points
    x0 = _TS[_N // 2]
    x1 = x0 + span
    _set_xrange(panel, x0, x1)
    panel._update_adaptive_points()
    qapp.processEvents()

    lo = int(np.searchsorted(_TS, x0, side="left"))
    hi = int(np.searchsorted(_TS, x1, side="right"))
    expected = hi - lo
    assert 0 < expected <= panel._POINTS_VISIBLE_THRESHOLD
    assert _scatter_len(panel._items["A"]) == expected
    assert _scatter_len(panel._items["B"]) == expected


def test_toggle_off_removes_scatters(panel, qapp):
    _populate(panel, qapp)
    panel.set_show_points(True)
    qapp.processEvents()
    assert panel._items["A"].scatter is not None

    panel.set_show_points(False)
    qapp.processEvents()
    assert panel._items["A"].scatter is None
    assert panel._items["B"].scatter is None


def test_line_curve_never_carries_symbols(panel, qapp):
    """The PlotDataItem must stay line-only — points live on the scatter."""
    _populate(panel, qapp)
    panel.set_show_points(True)
    qapp.processEvents()
    # Zoom in so points are actually shown.
    span = (_TS[-1] - _TS[0]) * (100.0 / _N)
    x0 = _TS[_N // 2]
    _set_xrange(panel, x0, x0 + span)
    panel._update_adaptive_points()
    qapp.processEvents()
    for plotted in panel._items.values():
        assert plotted.curve.opts.get("symbol") is None
    # And the dedicated scatter does hold points.
    assert _scatter_len(panel._items["A"]) > 0


def test_series_color_change_recolors_scatter(panel, qapp):
    _populate(panel, qapp)
    panel.set_show_points(True)
    span = (_TS[-1] - _TS[0]) * (100.0 / _N)
    x0 = _TS[_N // 2]
    _set_xrange(panel, x0, x0 + span)
    panel._update_adaptive_points()
    qapp.processEvents()

    from PySide6.QtGui import QColor
    panel.set_series_color("A", "#123456")
    qapp.processEvents()
    brush = panel._items["A"].scatter.opts["brush"]
    assert QColor(brush.color()).name() == "#123456"
