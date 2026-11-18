"""Strict reader for the ``signal-calibration`` reference-step trial file.

The trial file is a JSON object with exactly ``steps``, ``readings``,
``residual_tolerance`` and ``hysteresis_tolerance``.  Duplicate keys, unknown
fields, booleans posing as numbers, ``NaN``/``Infinity`` and malformed sample
segments are rejected before any package byte is trusted.

Every reference value, reading bound and tolerance is kept as an exact
:class:`fractions.Fraction` built from the literal decimal (exponent notation
allowed), never as a binary float.  A numeric literal may carry at most 32
mantissa digits and an explicit exponent whose absolute value is at most 308.
"""
import json
import re
from fractions import Fraction
from decimal import Decimal

from ..waveform import protocol as wp
from .errors import CalibrationError

TRIAL_MAX_BYTES = 2 * 1024 * 1024
MAX_STEPS = 1024
MAX_READINGS = 4096
MIN_DISTINCT_REFERENCES = 3
MAX_MANTISSA_DIGITS = 32
MAX_EXPONENT = 308
DIRECTIONS = ("up", "down")
READING_DIRECTIONS = ("up", "down", "unknown")

_TOP_KEYS = ("steps", "readings", "residual_tolerance",
             "hysteresis_tolerance")
_STEP_KEYS = ("start", "stop", "reference", "direction")
_READING_KEYS = ("id", "channel", "direction", "low", "high")


def _fail(message):
    raise CalibrationError(message)


def _check_literal(token):
    """Enforce the 32-mantissa-digit / exponent-308 literal bounds."""
    split = re.split("[eE]", token, maxsplit=1)
    mantissa = split[0].lstrip("+-").replace(".", "")
    if len(mantissa) > MAX_MANTISSA_DIGITS:
        _fail("numeric literal has more than %d mantissa digits: %s"
              % (MAX_MANTISSA_DIGITS, token))
    if len(split) == 2:
        exponent = split[1].lstrip("+-")
        significant = exponent.lstrip("0")
        if len(significant) > 3:
            _fail("numeric literal exponent exceeds absolute value %d: %s"
                  % (MAX_EXPONENT, token))
        magnitude = int(significant) if significant else 0
        if magnitude > MAX_EXPONENT:
            _fail("numeric literal exponent exceeds absolute value %d: %s"
                  % (MAX_EXPONENT, token))


def _parse_bytes(raw):
    if len(raw) > TRIAL_MAX_BYTES:
        _fail("trial file exceeds %d bytes" % TRIAL_MAX_BYTES)

    def unique(pairs):
        seen = set()
        for key, value in pairs:
            if key in seen:
                _fail("duplicate JSON key in trial file: " + str(key))
            seen.add(key)
        return dict(pairs)

    def reject_constant(value):
        _fail("non-finite JSON constant in trial file: " + value)

    def parse_int(token):
        _check_literal(token)
        return int(token)

    def parse_float(token):
        _check_literal(token)
        return Fraction(Decimal(token))

    try:
        obj = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=reject_constant,
            parse_int=parse_int,
            parse_float=parse_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CalibrationError("invalid trial JSON: " + str(error)) from None
    return obj


def _require_exact(obj, keys, label):
    if not isinstance(obj, dict):
        _fail("%s must be a JSON object" % label)
    if set(obj) != set(keys):
        _fail("%s must contain exactly %s"
              % (label, ",".join(sorted(keys))))


def _number(value, label):
    """Return an exact Fraction for a non-boolean finite JSON number."""
    if type(value) is int:
        return Fraction(value)
    if isinstance(value, Fraction):
        return value
    _fail("%s must be a finite JSON number" % label)


def _index(value, label):
    if type(value) is not int:
        _fail("%s must be an integer sample index" % label)
    if value < 0:
        _fail("%s must not be negative" % label)
    return value


def _ident(value, label):
    try:
        return wp.strict_name(value, label)
    except wp.ProtocolError as error:
        raise CalibrationError(str(error)) from None


class Step:
    __slots__ = ("start", "stop", "reference", "direction")

    def __init__(self, start, stop, reference, direction):
        self.start = start
        self.stop = stop
        self.reference = reference
        self.direction = direction


class Reading:
    __slots__ = ("reading_id", "channel", "direction", "low", "high")

    def __init__(self, reading_id, channel, direction, low, high):
        self.reading_id = reading_id
        self.channel = channel
        self.direction = direction
        self.low = low
        self.high = high


class Trial:
    __slots__ = ("steps", "readings", "residual_tolerance",
                 "hysteresis_tolerance")

    def __init__(self, steps, readings, residual_tolerance,
                 hysteresis_tolerance):
        self.steps = steps
        self.readings = readings
        self.residual_tolerance = residual_tolerance
        self.hysteresis_tolerance = hysteresis_tolerance


def _parse_steps(entries):
    if type(entries) is not list or len(entries) > MAX_STEPS:
        _fail("steps must be an array of at most %d entries" % MAX_STEPS)
    result = []
    seen = {}
    for index, entry in enumerate(entries):
        label = "steps[%d]" % index
        _require_exact(entry, _STEP_KEYS, label)
        start = _index(entry["start"], label + ".start")
        stop = _index(entry["stop"], label + ".stop")
        reference = _number(entry["reference"], label + ".reference")
        direction = entry["direction"]
        if direction not in DIRECTIONS:
            _fail("%s.direction must be up or down" % label)
        if stop <= start:
            _fail("%s segment [%d,%d) is empty" % (label, start, stop))
        key = (reference, direction)
        if key in seen:
            _fail("%s repeats reference %s direction %s, but each reference "
                  "needs exactly one up and one down segment"
                  % (label, reference, direction))
        seen[key] = True
        result.append(Step(start, stop, reference, direction))
    return result


def _parse_readings(entries):
    if type(entries) is not list or len(entries) > MAX_READINGS:
        _fail("readings must be an array of at most %d entries"
              % MAX_READINGS)
    result = []
    ids = set()
    for index, entry in enumerate(entries):
        label = "readings[%d]" % index
        _require_exact(entry, _READING_KEYS, label)
        reading_id = _ident(entry["id"], label + ".id")
        if reading_id in ids:
            _fail("duplicate reading id: " + reading_id)
        ids.add(reading_id)
        channel = _ident(entry["channel"], label + ".channel")
        direction = entry["direction"]
        if direction not in READING_DIRECTIONS:
            _fail("%s.direction must be up, down or unknown" % label)
        low = _number(entry["low"], label + ".low")
        high = _number(entry["high"], label + ".high")
        if low > high:
            _fail("%s.low must not exceed high" % label)
        result.append(Reading(reading_id, channel, direction, low, high))
    return result


def _check_segments(steps, points):
    """Sorted by start, the half-open segments must partition [0, points)."""
    ordered = sorted(steps, key=lambda step: step.start)
    expected = 0
    for step in ordered:
        if step.start != expected:
            _fail("steps do not partition [0, %d): segment starts at %d "
                  "but %d was expected" % (points, step.start, expected))
        expected = step.stop
    if expected != points:
        _fail("steps do not partition [0, %d): they end at %d"
              % (points, expected))
    references = {step.reference for step in steps}
    if len(references) < MIN_DISTINCT_REFERENCES:
        _fail("a trial needs at least %d distinct reference values"
              % MIN_DISTINCT_REFERENCES)
    for reference in references:
        for direction in DIRECTIONS:
            if not any(step.reference == reference
                       and step.direction == direction for step in steps):
                _fail("reference %s has no %s segment"
                      % (reference, direction))
    return ordered


def validate_trial(obj, points):
    """Validate a parsed trial object against a package's point count."""
    _require_exact(obj, _TOP_KEYS, "trial")
    residual_tolerance = _number(obj["residual_tolerance"],
                                 "residual_tolerance")
    hysteresis_tolerance = _number(obj["hysteresis_tolerance"],
                                   "hysteresis_tolerance")
    if residual_tolerance < 0:
        _fail("residual_tolerance must not be negative")
    if hysteresis_tolerance < 0:
        _fail("hysteresis_tolerance must not be negative")
    steps = _parse_steps(obj["steps"])
    readings = _parse_readings(obj["readings"])
    ordered = _check_segments(steps, points)
    return Trial(ordered, readings, residual_tolerance, hysteresis_tolerance)


def load_trial(trial_path, points):
    """Read, bound-check and validate one trial file against ``points``."""
    try:
        with open(trial_path, "rb") as stream:
            raw = stream.read(TRIAL_MAX_BYTES + 1)
    except OSError as error:
        raise CalibrationError("cannot read trial file: " + str(error)) \
            from None
    return validate_trial(_parse_bytes(raw), points)
