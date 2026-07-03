"""
Test that adding/removing signals in PlotPanel does not reset the view range.

These tests require a Qt platform plugin. On headless CI set
QT_QPA_PLATFORM=offscreen before running. If no display is found the
tests are skipped rather than erroring out.
"""
from __future__ import annotations

import os
import sys
import pytest
import numpy as np


# ---------------------------------------------------------------------------
# Skip machinery — skip the whole module if Qt cannot open a display
# ---------------------------------------------------------------------------

def _qt_available() -> bool:
    # Force offscreen when no display is set (CI / headless)
    if sys.platform != "win32" and not os.environ.get("DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication(sys.argv[:1])
        return app is not None
    except Exception:
        return False


if not _qt_available():
    pytest.skip("No Qt display available — skipping GUI tests", allow_module_level=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv[:1])
    return app


@pytest.fixture()
def panel(qapp):
    from gui.plot_widget import PlotPanel
    p = PlotPanel()
    p.resize(800, 400)
    p.show()
    qapp.processEvents()
    yield p
    p.close()
    qapp.processEvents()


def _make_series(n: int = 100, signal_name: str = "Sig", unit: str = "") -> object:
    """Return a minimal SignalSeries-compatible object."""
    from core.signal_store import SignalSeries
    ts = np.linspace(0.0, 1.0, n)
    vs = np.sin(ts * 10)
    return SignalSeries(
        channel=1,
        message_name="Msg",
        message_id=0x100,
        signal_name=signal_name,
        unit=unit,
        timestamps=ts,
        values=vs,
        raw_values=[],
        has_labels=False,
    )


def _get_xy_range(panel):
    """Return ([x0,x1], [y0,y1]) from the main plot ViewBox."""
    vr = panel.plot.plotItem.vb.viewRange()
    return list(vr[0]), list(vr[1])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestViewPreservedOnAddRemove:
    def test_add_first_signal_autofits(self, panel, qapp):
        """Adding the very first signal should auto-fit the view."""
        s = _make_series(signal_name="A")
        panel.add_series("A", s)
        qapp.processEvents()
        xr, yr = _get_xy_range(panel)
        # Both ranges should be finite (not the default [0, 1] for an empty plot)
        assert xr[1] - xr[0] > 0.5, "X range should span the data after first add"

    def test_add_second_signal_preserves_range(self, panel, qapp):
        """Adding a second signal must not snap X or Y back to full extent."""
        s1 = _make_series(signal_name="A")
        panel.add_series("A", s1)
        qapp.processEvents()
        panel.fit_to_window()
        qapp.processEvents()

        # Zoom into a narrow X window
        panel.plot.setXRange(0.1, 0.3, padding=0)
        panel.plot.setYRange(-0.5, 0.5, padding=0)
        qapp.processEvents()

        xr_before, yr_before = _get_xy_range(panel)

        s2 = _make_series(signal_name="B")
        panel.add_series("B", s2)
        qapp.processEvents()

        xr_after, yr_after = _get_xy_range(panel)

        assert abs(xr_after[0] - xr_before[0]) < 0.01, "X min must not change on add"
        assert abs(xr_after[1] - xr_before[1]) < 0.01, "X max must not change on add"
        assert abs(yr_after[0] - yr_before[0]) < 0.05, "Y min must not change on add"
        assert abs(yr_after[1] - yr_before[1]) < 0.05, "Y max must not change on add"

    def test_remove_signal_preserves_range(self, panel, qapp):
        """Removing a signal must not reset the zoom."""
        for name in ("A", "B", "C"):
            panel.add_series(name, _make_series(signal_name=name))
        qapp.processEvents()
        panel.fit_to_window()
        qapp.processEvents()

        panel.plot.setXRange(0.2, 0.5, padding=0)
        panel.plot.setYRange(-0.8, 0.8, padding=0)
        qapp.processEvents()

        xr_before, yr_before = _get_xy_range(panel)

        panel.remove_series("C")
        qapp.processEvents()

        xr_after, yr_after = _get_xy_range(panel)

        assert abs(xr_after[0] - xr_before[0]) < 0.01, "X min must not change on remove"
        assert abs(xr_after[1] - xr_before[1]) < 0.01, "X max must not change on remove"
        assert abs(yr_after[0] - yr_before[0]) < 0.05, "Y min must not change on remove"
        assert abs(yr_after[1] - yr_before[1]) < 0.05, "Y max must not change on remove"

    def test_remove_last_signal_then_add_autofits(self, panel, qapp):
        """After removing the last signal, the next add should auto-fit."""
        panel.add_series("A", _make_series(signal_name="A"))
        qapp.processEvents()
        panel.fit_to_window()
        qapp.processEvents()

        # Zoom to a tiny window
        panel.plot.setXRange(0.45, 0.55, padding=0)
        qapp.processEvents()

        # Remove the only signal
        panel.remove_series("A")
        qapp.processEvents()

        # Add a fresh signal — should auto-fit
        panel.add_series("B", _make_series(signal_name="B"))
        qapp.processEvents()

        xr, _ = _get_xy_range(panel)
        # After auto-fit the X range should span roughly the full data (0–1 s)
        assert xr[1] - xr[0] > 0.5, "X range should auto-fit after adding to empty plot"

    def test_fit_to_window_still_works(self, panel, qapp):
        """The manual Fit to window command must still auto-range."""
        for name in ("A", "B"):
            panel.add_series(name, _make_series(signal_name=name))
        qapp.processEvents()

        # Zoom in
        panel.plot.setXRange(0.0, 0.05, padding=0)
        qapp.processEvents()

        panel.fit_to_window()
        qapp.processEvents()

        xr, _ = _get_xy_range(panel)
        assert xr[1] - xr[0] > 0.5, "fit_to_window() should restore full X extent"
