"""
PlotPanel.center_on_time and PlotPanel.remove_series_many.

center_on_time backs the "click a finding, zoom to the trigger" flow: it must
center the X axis on the trigger with a fixed +/-half_width span, clamped to
the data's time bounds (shifting rather than shrinking the window).

remove_series_many backs the "replace, don't accumulate" finding-plot flow:
it must remove several keys in a single undo step / single rebuild.

Requires a Qt platform plugin; skipped (not failed) if none is available.
"""
from __future__ import annotations

import numpy as np
import pytest


def _make_series(n: int = 100, signal_name: str = "Sig", t_max: float = 1.0) -> object:
    """Return a minimal SignalStore-compatible series spanning [0, t_max]."""
    from core.signal_store import SignalSeries
    ts = np.linspace(0.0, t_max, n)
    vs = np.sin(ts * 10)
    return SignalSeries(
        channel=1,
        message_name="Msg",
        message_id=0x100,
        signal_name=signal_name,
        unit="",
        timestamps=ts,
        values=vs,
        raw_values=[],
        has_labels=False,
    )


def _get_x_range(panel):
    return list(panel.plot.plotItem.vb.viewRange()[0])


# ── center_on_time ───────────────────────────────────────────────────────────

class TestCenterOnTime:
    def test_centers_within_bounds(self, panel, qapp):
        panel.add_series("A", _make_series(t_max=100.0))
        qapp.processEvents()

        panel.center_on_time(20.0, 1.0, data_min=0.0, data_max=100.0)
        qapp.processEvents()

        x0, x1 = _get_x_range(panel)
        assert x0 == pytest.approx(19.0, abs=1e-6)
        assert x1 == pytest.approx(21.0, abs=1e-6)

    def test_clamps_near_data_start_shifts_not_shrinks(self, panel, qapp):
        panel.add_series("A", _make_series(t_max=100.0))
        qapp.processEvents()

        # Trigger at t=0.3 with half=1.0 would naively span [-0.7, 1.3].
        panel.center_on_time(0.3, 1.0, data_min=0.0, data_max=100.0)
        qapp.processEvents()

        x0, x1 = _get_x_range(panel)
        assert x0 == pytest.approx(0.0, abs=1e-6)
        assert x1 == pytest.approx(2.0, abs=1e-6)  # width preserved, just shifted

    def test_clamps_near_data_end_shifts_not_shrinks(self, panel, qapp):
        panel.add_series("A", _make_series(t_max=100.0))
        qapp.processEvents()

        panel.center_on_time(99.5, 1.0, data_min=0.0, data_max=100.0)
        qapp.processEvents()

        x0, x1 = _get_x_range(panel)
        assert x0 == pytest.approx(98.0, abs=1e-6)
        assert x1 == pytest.approx(100.0, abs=1e-6)

    def test_span_wider_than_data_clamps_to_full_span(self, panel, qapp):
        panel.add_series("A", _make_series(t_max=1.0))
        qapp.processEvents()

        panel.center_on_time(0.5, 10.0, data_min=0.0, data_max=1.0)
        qapp.processEvents()

        x0, x1 = _get_x_range(panel)
        assert x0 == pytest.approx(0.0, abs=1e-6)
        assert x1 == pytest.approx(1.0, abs=1e-6)

    def test_falls_back_to_plotted_items_when_bounds_omitted(self, panel, qapp):
        panel.add_series("A", _make_series(t_max=2.0))
        qapp.processEvents()

        # No data_min/data_max given — should derive [0, 2] from self._items
        # and clamp a near-start trigger against it.
        panel.center_on_time(0.2, 1.0)
        qapp.processEvents()

        x0, x1 = _get_x_range(panel)
        assert x0 == pytest.approx(0.0, abs=1e-6)
        assert x1 == pytest.approx(2.0, abs=1e-6)


class TestCenterOnTimeStackedMode:
    def test_centers_exactly_in_stacked_mode(self, panel, qapp):
        """Rows 1+ are X-linked to row 0 (see _rebuild_stacked). zoom_to_time
        must set the range once (not loop setXRange across every linked
        view) or the settled range drifts off the requested window."""
        for name in ("A", "B", "C"):
            panel.add_series(name, _make_series(signal_name=name, t_max=100.0))
        qapp.processEvents()
        panel.set_stacked(True)
        qapp.processEvents()
        # A prior fit_to_window (as plot_finding's add_signals_to_plot batch
        # path triggers) is what originally exposed the link-feedback drift.
        panel.fit_to_window()
        qapp.processEvents()

        panel.center_on_time(20.0, 1.0, data_min=0.0, data_max=100.0)
        qapp.processEvents()

        assert panel._stacked_plots, "expected stacked plots to exist"
        x0, x1 = panel._stacked_plots[0].viewRange()[0]
        assert x0 == pytest.approx(19.0, abs=1e-6)
        assert x1 == pytest.approx(21.0, abs=1e-6)


# ── remove_series_many ──────────────────────────────────────────────────────

class TestRemoveSeriesMany:
    def test_removes_only_present_keys(self, panel, qapp):
        for name in ("A", "B", "C"):
            panel.add_series(name, _make_series(signal_name=name))
        qapp.processEvents()

        panel.remove_series_many(["B", "does-not-exist"])
        qapp.processEvents()

        assert set(panel.plotted_keys()) == {"A", "C"}

    def test_noop_when_no_keys_present(self, panel, qapp):
        panel.add_series("A", _make_series(signal_name="A"))
        qapp.processEvents()

        panel.remove_series_many(["not-plotted"])
        qapp.processEvents()

        assert set(panel.plotted_keys()) == {"A"}

    def test_single_undo_step_restores_all(self, panel, qapp):
        """Removing several keys via remove_series_many should be one undo
        step (unlike looping remove_series, which would push one per key)."""
        for name in ("A", "B", "C"):
            panel.add_series(name, _make_series(signal_name=name))
        qapp.processEvents()

        panel.remove_series_many(["A", "B", "C"])
        qapp.processEvents()
        assert panel.plotted_keys() == []

        panel.undo()
        qapp.processEvents()
        assert set(panel.plotted_keys()) == {"A", "B", "C"}
