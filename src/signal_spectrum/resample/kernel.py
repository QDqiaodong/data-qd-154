"""Exact output grid and the fixed radius-16 Blackman-windowed sinc kernel.

Everything about *where* an output point sits in the source is derived from
the reduced integer ratio ``target/source = p/q``:

* output point ``m`` (a global, non-negative integer) sits at time
  ``t = m / target_rate`` and at source coordinate ``x_m = m*q/p``;
* the kernel taps are the integers ``j`` with ``|j - x_m| <= R``; the whole
  kernel window is used only when it fits inside the source indices, i.e.
  ``R <= x_m <= points-1-R``;
* ``x_m`` is never accumulated in floating point -- for each ``m`` the phase
  ``(m*q) mod p`` is an exact integer and ``d = j - x_m`` is formed from
  integers as ``(j*p - m*q)/p``.

Taps are laid out over the fixed offsets ``k = -R..R`` (source index
``floor(x_m)+k``); ``d = k - phase/p`` so the same layout works for every
phase.  The Blackman window vanishes at ``|d| = R`` and the taps strictly
outside ``|d| <= R`` receive zero weight, so the fixed layout reproduces the
contract's ``|d| <= R`` tap set for every phase.

The kernel is

    h(d) = 2*fc * sinc(2*fc*d) * w(d)
    w(d) = 0.42 + 0.5*cos(pi*d/R) + 0.08*cos(2*pi*d/R)
    fc   = 0.5 * min(1, target_rate/source_rate)

and every output value is normalised per point,
``sum(input[j]*h(d)) / sum(h(d))``, so a constant input keeps its exact
level.  Weights depend only on the fractional phase, so a bounded,
demand-driven cache reuses them when ``p`` is small; when the cache budget is
reached new phases are computed directly instead of growing without bound.
"""
import math
from fractions import Fraction

RADIUS = 16
MAX_OUTPUT_POINTS = 5_000_000
MAX_OUTPUT_BYTES = 640 * 1024 * 1024
# 33 doubles per phase plus dict overhead is < 1 KiB per entry; this keeps the
# phase cache well under 2 MiB, independent of how large p is.
PHASE_CACHE_LIMIT = 2048
_TAP_COUNT = 2 * RADIUS + 1


class Grid:
    """Planned output region for one source point count and rate pair."""

    __slots__ = ("source_rate", "target_rate", "p", "q", "gcd", "points",
                 "first_index", "last_index", "output_points")

    def __init__(self, source_rate, target_rate, p, q, gcd, points,
                 first_index, last_index):
        self.source_rate = source_rate
        self.target_rate = target_rate
        self.p = p
        self.q = q
        self.gcd = gcd
        self.points = points
        self.first_index = first_index
        self.last_index = last_index
        self.output_points = last_index - first_index + 1


def reduced_ratio(source_rate, target_rate):
    """Return ``(p, q, gcd)`` with ``target/source == p/q`` in lowest terms."""
    common = math.gcd(source_rate, target_rate)
    return target_rate // common, source_rate // common, common


def plan_grid(points, source_rate, target_rate, radius=RADIUS):
    """Return the exact output grid, or ``None`` when no point qualifies.

    ``x_m = m*q/p``; the full kernel window ``[x_m-R, x_m+R]`` must lie within
    ``[0, points-1]``.  That is the closed interval condition

        R <= m*q/p <= points-1-R

    so the first valid ``m`` is ``ceil(R*p/q)`` and the last is
    ``floor((points-1-R)*p/q)``.  Both bounds are integer arithmetic.
    """
    if points - 1 - radius < 0:
        return None
    p, q, common = reduced_ratio(source_rate, target_rate)
    first = -(-(radius * p) // q)          # ceil(R*p/q)
    last = ((points - 1 - radius) * p) // q  # floor((points-1-R)*p/q)
    if first > last:
        return None
    return Grid(source_rate, target_rate, p, q, common, points, first, last)


def _weights_for_phase(phase, p, two_fc):
    """Windowed-sinc tap weights for one exact fractional phase.

    ``phase/p`` is the fractional part of ``x_m``; tap ``k`` in
    ``-R..R`` corresponds to ``d = k - phase/p``.  Taps outside ``|d| <= R``
    get exactly zero weight (so a single 2R+1 layout can always be used).
    The phase is converted to binary64 exactly once -- never accumulated.
    """
    fraction = phase / p if p else 0.0
    weights = []
    for k in range(-RADIUS, RADIUS + 1):
        d = k - fraction
        if d < -RADIUS or d > RADIUS:
            weights.append(0.0)
            continue
        window = (0.42
                  + 0.5 * math.cos(math.pi * d / RADIUS)
                  + 0.08 * math.cos(2.0 * math.pi * d / RADIUS))
        argument = two_fc * d
        if argument == 0.0:
            sinc = 1.0
        else:
            scaled = math.pi * argument
            sinc = math.sin(scaled) / scaled
        weights.append(two_fc * sinc * window)
    return tuple(weights)


class Kernel:
    """Cutoff, normalisation and a bounded exact-phase weight cache."""

    __slots__ = ("p", "q", "source_rate", "target_rate", "cutoff", "fc",
                 "_two_fc", "_cache")

    def __init__(self, grid):
        self.p = grid.p
        self.q = grid.q
        self.source_rate = grid.source_rate
        self.target_rate = grid.target_rate
        if grid.target_rate < grid.source_rate:
            self.cutoff = Fraction(grid.p, 2 * grid.q)
        else:
            self.cutoff = Fraction(1, 2)
        self.fc = float(self.cutoff)
        self._two_fc = 2.0 * self.fc
        self._cache = {}

    def cutoff_rational(self):
        return {"num": self.cutoff.numerator,
                "den": self.cutoff.denominator}

    def weights(self, phase):
        """Return ``(weights, denominator)`` for an exact phase numerator."""
        entry = self._cache.get(phase)
        if entry is not None:
            return entry
        weights = _weights_for_phase(phase, self.p, self._two_fc)
        denominator = math.fsum(weights)
        entry = (weights, denominator)
        if len(self._cache) < PHASE_CACHE_LIMIT:
            self._cache[phase] = entry
        return entry

    def describe(self):
        return {
            "kind": "symmetric_windowed_sinc",
            "window": "blackman",
            "radius": RADIUS,
            "taps": _TAP_COUNT,
            "cutoff": self.cutoff_rational(),
            "normalization": "per_output_point_sum_of_weights",
            "formula": ("h(d)=2*fc*sinc(2*fc*d)*w(d); "
                        "w(d)=0.42+0.5*cos(pi*d/R)+0.08*cos(2*pi*d/R)"),
        }
