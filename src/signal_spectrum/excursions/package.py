"""Shared, fixed-baseline waveform-package verification and streaming decode.

These primitives are extracted from the excursion archive builder so every
consumer (the archive builder and ``signal-clock-join``) validates a
published package with the *same* code: the real schema_version=1 package
manifest, its ``meta_sha256`` self-consistency fingerprint, the declared raw
length/SHA256 and the existing endianness / point-major interleave /
``raw*scale + offset`` calibration semantics.

Nothing here trusts ``samples.ndjson``; only ``manifest.json`` and
``raw.bin`` are read.  Raw bytes are streamed through a fixed 1 MiB buffer
and decoded chunk by chunk, so neither the whole payload nor a whole channel
is ever resident in memory.
"""
import array
import hashlib
import math
import sys

from ..waveform import protocol as wp
from .errors import ExcursionError
from .manifest import (
    MANIFEST_NAME,
    RAW_NAME,
    parse_manifest_bytes,
    validate_manifest,
)

READ_CHUNK = 1024 * 1024
TYPECODES = {"int16": "h", "uint16": "H", "float32": "f"}


def _fail(message):
    raise ExcursionError(message)


def read_manifest(package_dir):
    """Read and hash one package manifest, rejecting symlinks and over-size."""
    manifest_path = package_dir / MANIFEST_NAME
    if manifest_path.is_symlink():
        _fail("manifest is a symbolic link: %s" % manifest_path)
    if not manifest_path.is_file():
        _fail("package has no manifest.json: %s" % package_dir)
    try:
        with open(manifest_path, "rb") as stream:
            raw = stream.read(65537)
    except OSError as error:
        raise ExcursionError("cannot read manifest: " + str(error)) from None
    manifest_sha = hashlib.sha256(raw).hexdigest()
    return parse_manifest_bytes(raw), manifest_sha


def check_raw_path(package_dir):
    raw_path = package_dir / RAW_NAME
    if raw_path.is_symlink():
        _fail("raw.bin is a symbolic link: %s" % raw_path)
    if not raw_path.is_file():
        _fail("package has no raw.bin: %s" % package_dir)
    return raw_path


def calibrate_channel(arr, channel_index, channel_count, spec, dtype,
                      point_base):
    """Calibrate one channel out of an interleaved native-order array.

    Produces exactly the decoder's per-point ``raw*scale + offset`` values;
    non-finite raw or calibrated values reject the whole package.
    """
    values = arr[channel_index::channel_count]
    if dtype == "float32":
        # NaN defeats min/max, so raw float32 values are checked one by one.
        if not all(map(math.isfinite, values)):
            for rel, raw_value in enumerate(values):
                if not math.isfinite(raw_value):
                    _fail("non-finite raw value at index %d channel %d"
                          % (point_base + rel, channel_index))
    calibrated = [raw * spec["scale"] + spec["offset"] for raw in values]
    if calibrated:
        low = min(calibrated)
        high = max(calibrated)
        if not (math.isfinite(low) and math.isfinite(high)):
            for rel, value in enumerate(calibrated):
                if not math.isfinite(value):
                    _fail("non-finite calibrated value at index %d channel %d"
                          % (point_base + rel, channel_index))
    return calibrated


def iter_source_chunks(manifest, raw_path):
    """Stream one verified ``raw.bin``, yielding decoded interleaved chunks.

    Yields ``(point_base, frames, array)`` where ``point_base`` is the
    absolute index of the first point in ``array`` and ``array`` holds
    ``frames * channel_count`` native-order values.  After the final chunk
    the total length, SHA256 and decoded point count are all checked
    against the manifest; any mismatch raises :class:`ExcursionError`.
    """
    channel_count = len(manifest["channels"])
    width = wp.WIDTH[manifest["dtype"]]
    frame_size = channel_count * width
    typecode = TYPECODES[manifest["dtype"]]
    swap = (manifest["byte_order"] == "little") != (sys.byteorder == "little")

    hasher = hashlib.sha256()
    total = 0
    points = 0
    carry = bytearray()
    try:
        stream = open(raw_path, "rb")
    except OSError as error:
        raise ExcursionError("cannot read raw.bin: " + str(error)) from None
    with stream:
        while True:
            try:
                chunk = stream.read(READ_CHUNK)
            except OSError as error:
                raise ExcursionError(
                    "cannot read raw.bin: " + str(error)) from None
            if not chunk:
                break
            hasher.update(chunk)
            total += len(chunk)
            carry.extend(chunk)
            usable = len(carry) - (len(carry) % frame_size)
            if usable == 0:
                continue
            block = bytes(carry[:usable])
            del carry[:usable]
            arr = array.array(typecode, block)
            if swap:
                arr.byteswap()
            frames = usable // frame_size
            yield points, frames, arr
            points += frames
    if carry:
        _fail("raw.bin has %d trailing bytes that do not form a whole frame"
              % len(carry))
    if total != manifest["raw_bytes"]:
        _fail("raw.bin length %d does not match manifest raw_bytes %d"
              % (total, manifest["raw_bytes"]))
    if hasher.hexdigest() != manifest["raw_sha256"]:
        _fail("raw.bin SHA256 does not match manifest raw_sha256; "
              "refusing to use an unverified payload")
    if points != manifest["points"]:
        _fail("decoded %d points but manifest declares %d"
              % (points, manifest["points"]))
