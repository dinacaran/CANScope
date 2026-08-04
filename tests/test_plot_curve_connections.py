"""Regression tests for accidental first/last plot connections."""
from __future__ import annotations

import array

import numpy as np
from pyqtgraph import functions as pg_functions

from core.signal_store import SignalSeries
from gui.plot_widget import PlotPanel, PlottedSignal


def _series(timestamps, values) -> SignalSeries:
    return SignalSeries(
        channel=1,
        message_name="Msg",
        message_id=0x100,
        signal_name="FuelLevel",
        unit="%",
        timestamps=array.array("d", timestamps),
        values=array.array("d", values),
    )


def test_curve_connect_mask_breaks_before_timestamp_reset():
    """A trailing sample at t=0 must not connect back from the recording end."""
    ts = np.array([0.0, 1.0, 2.0, 3.0, 0.0])
    vs = np.array([0.0, 70.0, 70.0, 70.0, 0.0])

    connect = PlotPanel._curve_connect_mask(ts, vs)

    assert connect.tolist() == [True, True, True, False, False]

    path = pg_functions.arrayToQPath(ts, vs, connect=connect)
    reset_element = path.elementAt(4)
    assert reset_element.isMoveTo(), "timestamp reset must start a new subpath"


def test_curve_connect_mask_breaks_on_either_non_finite_endpoint():
    ts = np.array([0.0, 1.0, 2.0, 3.0])
    vs = np.array([10.0, np.nan, 30.0, 40.0])

    connect = PlotPanel._curve_connect_mask(ts, vs)

    assert connect.tolist() == [False, False, True, False]


def test_plot_curve_receives_open_segment_mask():
    """The integration path must pass the reset-aware mask to pyqtgraph."""
    class CurveSpy:
        def setData(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class PanelStub:
        _hide_lines = False
        _show_points = False
        _LINE_WIDTH = 2.8
        _SELECTED_LINE_WIDTH = 5.0
        _curve_connect_mask = staticmethod(PlotPanel._curve_connect_mask)

    curve = CurveSpy()
    plotted = PlottedSignal(
        key="fuel",
        series=_series(
            [0.0, 1.0, 2.0, 3.0, 0.0],
            [0.0, 70.0, 70.0, 70.0, 0.0],
        ),
        curve=curve,
        color="#ff0000",
    )
    PlotPanel._apply_curve_style(PanelStub(), plotted)

    connect = curve.kwargs["connect"]
    assert isinstance(connect, np.ndarray)
    assert connect.tolist() == [True, True, True, False, False]
