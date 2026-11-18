"""Streaming step means, curve fits and reading inversions.

Each channel of the package is calibrated while the verified payload streams
through once; per-step, per-channel means are accumulated as exact scaled
integers (see :mod:`exact`), so no complete waveform or complete channel is
ever retained.  Fits and inversions are exact rational computations, and a
tolerance is compared as ``value <= tolerance`` on rationals: a gap of any
positive size stays a gap.
"""
import math
from fractions import Fraction
from itertools import islice

from ..excursions.manifest import validate_manifest
from ..excursions.package import (
    calibrate_channel,
    check_raw_path,
    iter_source_chunks,
    read_manifest,
)
from .curves import (
    classify,
    inverse_intervals,
    isotonic,
    merge_intervals,
    rational_interval,
)
from .errors import CalibrationError
from .exact import mean_from_scaled, rational, scaled_int


def _fail(message):
    raise CalibrationError(message)


class Package:
    __slots__ = ("path", "manifest", "manifest_sha256", "raw_path",
                 "channels")

    def __init__(self, path, manifest, manifest_sha256, raw_path):
        self.path = path
        self.manifest = manifest
        self.manifest_sha256 = manifest_sha256
        self.raw_path = raw_path
        self.channels = manifest["channels"]

    @property
    def points(self):
        return self.manifest["points"]

    @property
    def raw_sha256(self):
        return self.manifest["raw_sha256"]

    def channel_index(self, name):
        for index, spec in enumerate(self.channels):
            if spec["name"] == name:
                return index
        return None


def load_package(package_dir):
    """Verify the package manifest and locate its raw payload."""
    manifest_obj, manifest_sha = read_manifest(package_dir)
    manifest = validate_manifest(manifest_obj)
    raw_path = check_raw_path(package_dir)
    return Package(package_dir, manifest, manifest_sha, raw_path)


def validate_readings(trial, package):
    """Every reading must name a real channel of the package."""
    for reading in trial.readings:
        if package.channel_index(reading.channel) is None:
            _fail("reading %s references unknown channel %r of the package"
                  % (reading.reading_id, reading.channel))


def _binary64_scaled(value):
    """Exact scaled integer of one engineering reading as a binary64 value.

    The baseline decoder emits the calibrated point as a binary64
    (``float``); an exact integer calibration result is rounded to that same
    value here before it contributes to an exact sum.  A calibration result
    that cannot be represented as a finite binary64 rejects the package.
    """
    try:
        reading = float(value)
    except (OverflowError, ValueError):
        raise CalibrationError(
            "calibrated value is not a finite binary64 number") from None
    if not math.isfinite(reading):
        _fail("calibrated value is not a finite binary64 number")
    return scaled_int(reading)


class StepMeans:
    """Per-step, per-channel exact sums of scaled calibrated values."""

    def __init__(self, steps, channel_count):
        self.steps = steps
        self.channel_count = channel_count
        self.totals = [[0] * channel_count for _ in steps]

    def add_chunk(self, segments, calibrated, channel_index):
        column = self.totals
        for step_index, first, last in segments:
            if first < last:
                column[step_index][channel_index] += \
                    sum(map(_binary64_scaled,
                            islice(calibrated, first, last)))

    def mean(self, step_index, channel_index):
        step = self.steps[step_index]
        return mean_from_scaled(self.totals[step_index][channel_index],
                                step.stop - step.start)


def _chunk_segments(steps, chunk_start, chunk_stop, cursor):
    """Return ``(segments, next_cursor)`` for one decoded chunk.

    ``segments`` are ``(step_index, first, last)`` slice bounds relative to
    the chunk.  Steps partition the sampled range, so each chunk touches a
    consecutive run of them.
    """
    count = len(steps)
    while cursor < count and steps[cursor].stop <= chunk_start:
        cursor += 1
    segments = []
    index = cursor
    next_cursor = cursor
    while index < count and steps[index].start < chunk_stop:
        first = max(steps[index].start, chunk_start) - chunk_start
        last = min(steps[index].stop, chunk_stop) - chunk_start
        if first < last:
            segments.append((index, first, last))
        # A step that straddles the chunk boundary resumes in the next one.
        if steps[index].stop > chunk_stop:
            next_cursor = index
        else:
            next_cursor = index + 1
        index += 1
    return segments, next_cursor


def accumulate_means(package, steps):
    """Stream the verified raw payload once and accumulate all step means."""
    channel_count = len(package.channels)
    means = StepMeans(steps, channel_count)
    cursor = 0
    points = package.points
    for point_base, frames, arr in iter_source_chunks(package.manifest,
                                                      package.raw_path):
        chunk_stop = point_base + frames
        if chunk_stop > points:
            _fail("decoded more points than the manifest declares")
        segments, cursor = _chunk_segments(steps, point_base, chunk_stop,
                                           cursor)
        for channel_index, spec in enumerate(package.channels):
            calibrated = calibrate_channel(
                arr, channel_index, channel_count, spec,
                package.manifest["dtype"], point_base)
            if len(calibrated) != frames:
                _fail("decoded channel length mismatch")
            means.add_chunk(segments, calibrated, channel_index)
    if cursor != len(steps):
        _fail("decoded payload does not cover every step segment")
    return means


def fit_direction(step_means, steps, channel_index, direction, references):
    """Fit one direction on the shared strictly increasing reference nodes.

    Each step contributes the node value of *its own* reference, so a pass
    that visits references in descending order is handled exactly like an
    ascending one.
    """
    pairs = [(step.reference, step_means.mean(index, channel_index))
             for index, step in enumerate(steps)
             if step.direction == direction]
    pairs.sort(key=lambda pair: pair[0])
    node_refs = [ref for ref, _ in pairs]
    node_means = [mean for _, mean in pairs]
    if node_refs != list(references):
        _fail("internal error: %s nodes do not match the shared references"
              % direction)
    return node_refs, node_means, isotonic(node_means)


def channel_curves(step_means, steps, channel_index, references):
    """Return ``(up, down)`` fits and both exact tolerance maxima."""
    up_refs, up_means, up_fitted = fit_direction(
        step_means, steps, channel_index, "up", references)
    down_refs, down_means, down_fitted = fit_direction(
        step_means, steps, channel_index, "down", references)
    residual = Fraction(0)
    for refs, means, fitted in ((up_refs, up_means, up_fitted),
                                (down_refs, down_means, down_fitted)):
        for measured, model in zip(means, fitted):
            residual = max(residual, abs(measured - model))
    hysteresis = Fraction(0)
    for up_value, down_value in zip(up_fitted, down_fitted):
        hysteresis = max(hysteresis, abs(up_value - down_value))
    up = (up_refs, up_means, up_fitted)
    down = (down_refs, down_means, down_fitted)
    return up, down, residual, hysteresis


def invert_reading(reading, up_fit, down_fit, node_refs):
    """Invert one reading against the curve(s) the reading direction allows."""
    low, high = reading.low, reading.high
    up_intervals = []
    down_intervals = []
    if reading.direction in ("up", "unknown"):
        up_intervals = inverse_intervals(node_refs, up_fit[2], low, high)
    if reading.direction in ("down", "unknown"):
        down_intervals = inverse_intervals(node_refs, down_fit[2], low, high)
    if reading.direction == "unknown":
        intervals = merge_intervals(up_intervals + down_intervals)
    elif reading.direction == "up":
        intervals = up_intervals
    else:
        intervals = down_intervals
    return intervals


def curve_report(fit):
    """Render one direction's nodes as reference, measured mean, fitted."""
    refs, means, fitted = fit
    return [
        {"reference": rational(ref),
         "measured_mean": rational(mean),
         "fitted": rational(model)}
        for ref, mean, model in zip(refs, means, fitted)
    ]


def build_certificate(package, trial, step_means, references):
    """Assemble the certificate object from exact results."""
    channel_entries = []
    channel_fits = {}
    for channel_index, spec in enumerate(package.channels):
        up, down, residual, hysteresis = channel_curves(
            step_means, trial.steps, channel_index, references)
        qualified = (residual <= trial.residual_tolerance
                     and hysteresis <= trial.hysteresis_tolerance)
        channel_fits[spec["name"]] = (up, down, references)
        channel_entries.append({
            "channel": spec["name"],
            "qualified": qualified,
            "residual_max": rational(residual),
            "hysteresis_max": rational(hysteresis),
            "up": curve_report(up),
            "down": curve_report(down),
        })

    readings = []
    for reading in sorted(trial.readings, key=lambda item: item.reading_id):
        up, down, node_refs = channel_fits[reading.channel]
        intervals = invert_reading(reading, up, down, node_refs)
        readings.append({
            "id": reading.reading_id,
            "channel": reading.channel,
            "classification": classify(intervals),
            "intervals": [rational_interval(interval)
                          for interval in intervals],
        })

    return {
        "manifest_sha256": package.manifest_sha256,
        "raw_sha256": package.raw_sha256,
        "channels": channel_entries,
        "readings": readings,
    }
