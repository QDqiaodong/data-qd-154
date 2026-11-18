"""Exact-value primitives for ``signal-calibration``.

Two things must never go through an epsilon:

* the engineering values of a decoded sample are the exact binary64 values
  produced by the baseline decoder (``raw*scale + offset``); sums and means
  are exact rational sums of those values, and
* the STEPS reference values, reading bounds and tolerances are the exact
  decimal values written in the file (exponent notation allowed).

Binary64 values are summed as integers scaled by ``2**1074`` (the smallest
positive subnormal).  ``num << (1074 - exponent)`` is exact for every finite
binary64, so the accumulated integer divided by ``count * 2**1074`` is the
exact rational mean -- no rounding, and independent of how the payload was
split into decoding blocks.
"""
from fractions import Fraction

# Exponent of the smallest positive binary64 subnormal.
SCALE_SHIFT = 1074
SCALE_DENOMINATOR = 1 << SCALE_SHIFT


def scaled_int(value):
    """Return ``value * 2**1074`` as an exact integer for a finite float."""
    num, den = value.as_integer_ratio()
    return num << (SCALE_SHIFT - (den.bit_length() - 1))


def mean_from_scaled(total, count):
    """Exact rational mean from a scaled integer sum and a positive count."""
    return Fraction(total, count * SCALE_DENOMINATOR)


def rational(value):
    """Render an exact rational as a reduced ``{num, den}`` with den > 0."""
    frac = Fraction(value)
    return {"num": frac.numerator, "den": frac.denominator}
