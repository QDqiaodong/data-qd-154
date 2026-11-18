"""Monotone least-squares curve fit and exact inversion.

For one direction the trial provides, per reference node (strictly increasing
``x``), the exact mean measured response ``y``.  The fitted node value is the
non-decreasing sequence minimising ``sum((y - fitted)**2)`` with every step
weighted equally (the associated sample count is irrelevant).  That is the
pool-adjacent-violators (PAVA) solution, implemented here in exact rational
arithmetic.

Between nodes the curve is the linear interpolation of the node values.  A
reading is a closed interval of responses; its inverse is the union of all
reference values inside the curve domain whose interpolated response falls in
that interval -- never extrapolated.  Because a segment may be flat or go
down, the inverse of one channel can be several closed intervals; only
touching/interpenetrating intervals are merged, never a real gap.
"""
from fractions import Fraction

from .exact import rational


def isotonic(values):
    """Return the equal-weight non-decreasing least-squares fit of ``values``."""
    blocks = []  # [sum, count] with strictly increasing block means
    for value in values:
        blocks.append([value, 1])
        while len(blocks) > 1:
            left, right = blocks[-2], blocks[-1]
            # Compare means exactly: left.sum/left.count > right.sum/right.count
            if left[0] * right[1] > right[0] * left[1]:
                blocks[-2:] = [[left[0] + right[0], left[1] + right[1]]]
            else:
                break
    fitted = []
    for total, count in blocks:
        fitted.extend([Fraction(total, count)] * count)
    return fitted


def inverse_intervals(nodes_x, nodes_y, low, high):
    """All closed ``[x0, x1]`` on the piecewise-linear curve with y in
    ``[low, high]``, sorted by start (one channel, one direction)."""
    pieces = []
    last = len(nodes_x) - 1
    for index in range(last):
        x0 = nodes_x[index]
        x1 = nodes_x[index + 1]
        y0 = nodes_y[index]
        y1 = nodes_y[index + 1]
        seg_low = min(y0, y1)
        seg_high = max(y0, y1)
        if high < seg_low or low > seg_high:
            continue  # reading interval misses this segment entirely
        if y0 == y1:
            pieces.append((x0, x1))
            continue
        span = y1 - y0
        u_lo = (low - y0) / span
        u_hi = (high - y0) / span
        if u_lo > u_hi:
            u_lo, u_hi = u_hi, u_lo
        u_lo = min(max(u_lo, Fraction(0)), Fraction(1))
        u_hi = min(max(u_hi, Fraction(0)), Fraction(1))
        start = x0 + u_lo * (x1 - x0)
        stop = x0 + u_hi * (x1 - x0)
        if start <= stop:
            pieces.append((start, stop))
    return merge_intervals(pieces)


def merge_intervals(intervals):
    """Merge intersecting or endpoint-touching closed intervals.

    Sorted by start, then extended only while the next interval starts at or
    before the current end.  A positive gap between two intervals is always
    preserved (it is never closed with an epsilon).
    """
    ordered = sorted(intervals)
    merged = []
    for start, stop in ordered:
        if merged and start <= merged[-1][1]:
            if stop > merged[-1][1]:
                merged[-1][1] = stop
        else:
            merged.append([start, stop])
    return merged


def classify(intervals):
    """``outside`` for none, ``unique`` for one zero-length, else ambiguous."""
    if not intervals:
        return "outside"
    if len(intervals) == 1 and intervals[0][0] == intervals[0][1]:
        return "unique"
    return "ambiguous"


def rational_interval(interval):
    return {"low": rational(interval[0]), "high": rational(interval[1])}
