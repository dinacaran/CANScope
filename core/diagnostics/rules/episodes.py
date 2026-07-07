"""
Episode segmentation — shared by the rule processors.

A rule condition frequently matches many samples that belong to *several*
distinct fault events separated by long quiet stretches.  Reporting one finding
that spans the whole recording (or, worse, a zero-width point at the first
sample) hides every event after the first from the evidence builder and the
agent loop.

``segment`` / ``index_ranges`` split a sorted array of fault-sample timestamps
into contiguous **episodes**: a gap larger than ``merge_gap_s`` starts a new
episode.  The default ``merge_gap_s`` a caller should pass is the active
domain's ``context_window_s`` — two events closer than one context window are
treated as the same episode.

numpy only; fully vectorised.
"""
from __future__ import annotations

import numpy as np

#: Hard cap on episode findings emitted per rule.  When a rule produces more
#: episodes than this, callers keep the first ``MAX_FINDINGS_PER_RULE`` and add a
#: single summary finding covering the remainder (see ``overall_span``).
MAX_FINDINGS_PER_RULE: int = 20


def index_ranges(ts_fault: np.ndarray, merge_gap_s: float) -> list[tuple[int, int]]:
    """Split sorted fault timestamps into contiguous ``[start, end]`` index ranges.

    Parameters
    ----------
    ts_fault:
        Timestamps of the fault samples, ascending.  (The rule masks are applied
        to already-sorted store timestamps, so this holds in practice.)
    merge_gap_s:
        A gap strictly greater than this starts a new episode.  A gap exactly
        equal to it does **not** split (so ``context_window_s`` is inclusive).

    Returns
    -------
    list of ``(start_idx, end_idx)`` inclusive index pairs into *ts_fault*.
    Empty input -> ``[]``; a single sample -> ``[(0, 0)]``.
    """
    ts = np.asarray(ts_fault, dtype=np.float64)
    n = ts.size
    if n == 0:
        return []
    if n == 1:
        return [(0, 0)]

    breaks = np.where(np.diff(ts) > merge_gap_s)[0]
    if breaks.size == 0:
        return [(0, n - 1)]

    ranges: list[tuple[int, int]] = []
    start = 0
    for b in breaks:
        ranges.append((start, int(b)))
        start = int(b) + 1
    ranges.append((start, n - 1))
    return ranges


def segment(ts_fault: np.ndarray, merge_gap_s: float) -> list[tuple[float, float]]:
    """Return ``(episode_start, episode_end)`` times for each episode.

    Thin wrapper over :func:`index_ranges` for callers that only need the time
    bounds.
    """
    ts = np.asarray(ts_fault, dtype=np.float64)
    return [(float(ts[a]), float(ts[b])) for a, b in index_ranges(ts, merge_gap_s)]


def overall_span(times: np.ndarray, ranges: list[tuple[int, int]]) -> tuple[float, float]:
    """Overall ``(start, end)`` covered by a list of index ranges.

    Used to describe the episodes dropped by the ``MAX_FINDINGS_PER_RULE`` cap.
    *times* is the timestamp array the ranges index into.
    """
    ts = np.asarray(times, dtype=np.float64)
    first = ranges[0][0]
    last = ranges[-1][1]
    return float(ts[first]), float(ts[last])
