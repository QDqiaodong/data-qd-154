"""Source verification, streaming resampling and output hashing.

The source is one published ``signal-waveform-fetch`` package: the
schema_version=1 ``manifest.json`` is validated with the fixed-baseline rules
(including the ``meta_sha256`` self-consistency fingerprint), ``raw.bin`` is
streamed exactly once through the baseline decoder (length, SHA256, whole
frames, endianness, point-major interleave and ``raw*scale + offset``
calibration are all re-checked) and every calibrated value must be finite.

Only a radius-16 neighbourhood per channel plus one fixed-size output block is
ever resident: calibrated values are consumed block by block, the kernel
window is sliced out of a bounded tail buffer, and finished output points are
packed into ``samples.f64le`` immediately.
"""
import hashlib
import math
import os
import struct
from pathlib import Path

from ..excursions.errors import ExcursionError
from ..excursions.manifest import validate_manifest
from ..excursions.package import (
    calibrate_channel,
    check_raw_path,
    iter_source_chunks,
    read_manifest,
)
from .errors import ResampleError
from .kernel import (
    MAX_OUTPUT_BYTES,
    MAX_OUTPUT_POINTS,
    RADIUS,
    Kernel,
    plan_grid,
)

SAMPLE_FILE = "samples.f64le"
LINEAGE_FILE = "lineage.json"
STAGING_SUFFIX = ".staging"
TARGET_RATE_MIN = 1
TARGET_RATE_MAX = 10 ** 9

# Frames decoded per calibration sub-block and output points per write flush.
_DECODE_FRAMES = 4096
_FLUSH_POINTS = 8192
_TAPS = 2 * RADIUS + 1


def _fail(message):
    raise ResampleError(message)


def _wrap(callable_, *args):
    try:
        return callable_(*args)
    except ExcursionError as error:
        raise ResampleError(str(error)) from None


class SourcePlan:
    """Everything verified about one source package before decoding starts."""

    __slots__ = ("manifest", "manifest_sha256", "raw_path", "grid", "kernel")

    def __init__(self, manifest, manifest_sha256, raw_path, grid, kernel):
        self.manifest = manifest
        self.manifest_sha256 = manifest_sha256
        self.raw_path = raw_path
        self.grid = grid
        self.kernel = kernel

    @property
    def channel_count(self):
        return len(self.manifest["channels"])


def validate_target_rate(target_rate):
    if type(target_rate) is not int or type(target_rate) is bool:
        _fail("target rate must be an integer number of Hz")
    if not TARGET_RATE_MIN <= target_rate <= TARGET_RATE_MAX:
        _fail("target rate outside allowed range %d..%d"
              % (TARGET_RATE_MIN, TARGET_RATE_MAX))
    return target_rate


def prepare_source(input_dir, target_rate):
    """Verify the package manifest and plan the exact output grid."""
    input_dir = Path(input_dir)
    if not input_dir.is_dir():
        _fail("input dataset is not a directory: %s" % input_dir)
    manifest_obj, manifest_sha = _wrap(read_manifest, input_dir)
    manifest = _wrap(validate_manifest, manifest_obj)
    raw_path = _wrap(check_raw_path, input_dir)
    grid = plan_grid(manifest["points"], manifest["sample_rate"],
                     target_rate, RADIUS)
    if grid is None:
        _fail("the source capture is too short for a complete radius-%d "
              "kernel window at %d Hz; no output point qualifies"
              % (RADIUS, target_rate))
    if grid.output_points > MAX_OUTPUT_POINTS:
        _fail("target output would contain %d points, above the %d point "
              "limit" % (grid.output_points, MAX_OUTPUT_POINTS))
    output_bytes = grid.output_points * len(manifest["channels"]) * 8
    if output_bytes > MAX_OUTPUT_BYTES:
        _fail("target output would be %d bytes, above the %d byte limit"
              % (output_bytes, MAX_OUTPUT_BYTES))
    return SourcePlan(manifest, manifest_sha, raw_path, grid,
                      Kernel(grid))


def _emit_point(plan, next_m, buffers, buffer_base):
    """Compute one output point from its exact integer phase."""
    grid = plan.grid
    base = (next_m * grid.q) // grid.p
    phase = (next_m * grid.q) % grid.p
    weights, denominator = plan.kernel.weights(phase)
    if not math.isfinite(denominator) or denominator == 0.0:
        _fail("kernel weights do not have a finite non-zero sum at output "
              "index %d" % next_m)
    position = base - RADIUS - buffer_base
    total = len(buffers[0])
    if position < 0 or position + _TAPS > total:
        _fail("internal kernel window is not resident at output index %d"
              % next_m)
    values = []
    for channel, buffer in enumerate(buffers):
        window = buffer[position:position + _TAPS]
        numerator = math.fsum([weight * value
                               for weight, value in zip(weights, window)])
        value = numerator / denominator
        if not math.isfinite(value):
            _fail("non-finite output value at index %d channel %d"
                  % (next_m, channel))
        values.append(value)
    return values


def _injected_failure(stage):
    """Finite fault-injection hook used only by tests and the demo."""
    if os.environ.get("SIGSPECTRUM_RS_FAIL") == stage:
        raise ResampleError("injected failure at stage %s" % stage)


def stream_output(plan, destination):
    """Resample the verified source into ``destination``; return digests.

    Returns ``(output_points, output_bytes, sha256_hex)``.  The file is
    flushed and fsynced before returning.
    """
    grid = plan.grid
    channel_count = plan.channel_count
    manifest = plan.manifest
    buffers = [[] for _ in range(channel_count)]
    buffer_base = 0
    next_m = grid.first_index
    last_m = grid.last_index
    hasher = hashlib.sha256()
    block = bytearray()
    written = 0
    packer = struct.Struct("<%dd" % channel_count)

    try:
        stream = open(destination, "wb")
    except OSError as error:
        raise ResampleError("cannot create %s: %s" % (SAMPLE_FILE, error)) \
            from None
    try:
        try:
            for point_base, frames, arr in iter_source_chunks(manifest,
                                                             plan.raw_path):
                offset = 0
                while offset < frames:
                    count = min(_DECODE_FRAMES, frames - offset)
                    sub = arr[offset * channel_count:
                              (offset + count) * channel_count]
                    block_base = point_base + offset
                    for channel_index, spec in enumerate(
                            manifest["channels"]):
                        calibrated = calibrate_channel(
                            sub, channel_index, channel_count, spec,
                            manifest["dtype"], block_base)
                        buffers[channel_index].extend(calibrated)
                    last_index = block_base + count - 1
                    # floor(x_m)+R is the highest source index this output
                    # point reads, so it is ready as soon as that index has
                    # been decoded.
                    while (next_m <= last_m
                           and (next_m * grid.q) // grid.p + RADIUS
                           <= last_index):
                        block += packer.pack(
                            *_emit_point(plan, next_m, buffers, buffer_base))
                        written += 1
                        next_m += 1
                        if written % _FLUSH_POINTS == 0:
                            hasher.update(block)
                            stream.write(block)
                            block.clear()
                    if next_m <= last_m:
                        keep_from = (next_m * grid.q) // grid.p - RADIUS
                        if keep_from < 0:
                            keep_from = 0
                    else:
                        keep_from = last_index + 1
                    # Never discard past the bytes actually decoded: under
                    # heavy decimation the next window can start beyond this
                    # block, and the buffer must stay a contiguous run that
                    # begins exactly at ``buffer_base``.
                    if keep_from > last_index + 1:
                        keep_from = last_index + 1
                    drop = keep_from - buffer_base
                    if drop > 0:
                        for buffer in buffers:
                            del buffer[:drop]
                        buffer_base = keep_from
                    offset += count
            if block:
                hasher.update(block)
                stream.write(block)
                block.clear()
            _injected_failure("after_samples")
            stream.flush()
            os.fsync(stream.fileno())
        except OSError as error:
            raise ResampleError("cannot write %s: %s"
                                % (SAMPLE_FILE, error)) from None
        except ExcursionError as error:
            raise ResampleError(str(error)) from None
    finally:
        try:
            stream.close()
        except OSError:
            # A close failure after an explicit flush+fsync is harmless; an
            # in-flight error must not be masked by it.
            pass

    if written != grid.output_points:
        _fail("produced %d output points but planned %d"
              % (written, grid.output_points))
    try:
        size = os.path.getsize(destination)
    except OSError as error:
        raise ResampleError("cannot stat %s: %s"
                            % (SAMPLE_FILE, error)) from None
    expected = written * channel_count * 8
    if size != expected:
        _fail("%s has %d bytes but %d were expected"
              % (SAMPLE_FILE, size, expected))
    return written, size, hasher.hexdigest()
