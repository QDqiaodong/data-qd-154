"""Build an immutable excursion archive from published waveform packages.

Pipeline per source package, all streaming with bounded memory:

1. ``manifest.json`` (<= 64 KiB, never a symlink) is parsed and validated
   against the schema_version=1 package contract, including the
   ``meta_sha256`` self-consistency fingerprint.
2. ``raw.bin`` (never a symlink) is streamed exactly once: every byte is
   hashed and the total length and SHA256 must match the manifest, while
   frames are decoded with the existing endianness/interleaving semantics
   and calibrated per channel with ``raw*scale + offset`` -- the same finite
   binary64 values the acquisition decoder produces, stored unquantized as
   little-endian float64 snapshot files.
3. Per-channel leaf summaries (<= 256 samples) roll up into a persistent
   interval-summary tree inside ``catalog.db``.

``samples.ndjson`` is never consulted, no device is contacted and the source
packages are opened read-only.  The archive directory is published by an
atomic rename only after every source verified and every index row
committed; a failed build removes its staging directory and never leaves a
queryable half-product behind.
"""
import array
import hashlib
import json
import math
import os
import shutil
import sqlite3
import struct
import sys
from pathlib import Path

from ..waveform import protocol as wp
from .archive import (
    ARCHIVE_VERSION,
    CATALOG_NAME,
    FANOUT,
    LEAF_SIZE,
    SCHEMA,
    STAGING_SUFFIX,
    VALUES_DIR,
    IndexBuilder,
    NodeBatcher,
    values_file_name,
)
from .errors import ExcursionError
from .manifest import (
    MANIFEST_NAME,
    RAW_NAME,
    parse_manifest_bytes,
    validate_manifest,
)
from .sources import MAX_RAW_TOTAL_BYTES, load_sources

_READ_CHUNK = 1024 * 1024
_FLUSH_VALUES = 8192  # multiple of LEAF_SIZE; per-channel buffer bound
_TYPECODES = {"int16": "h", "uint16": "H", "float32": "f"}


def _fail(message):
    raise ExcursionError(message)


def _fsync_dir(path):
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _read_manifest(package_dir):
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


def _check_raw_path(package_dir):
    raw_path = package_dir / RAW_NAME
    if raw_path.is_symlink():
        _fail("raw.bin is a symbolic link: %s" % raw_path)
    if not raw_path.is_file():
        _fail("package has no raw.bin: %s" % package_dir)
    return raw_path


class _ChannelSink:
    """Receives calibrated values for one channel in arrival order.

    Buffers up to ``_FLUSH_VALUES`` values, then packs them verbatim as
    little-endian float64 into the snapshot file, folds them into
    LEAF_SIZE-sample leaf summaries and feeds the streaming index builder.
    Neither the whole channel nor the whole index is ever held in memory.
    """

    def __init__(self, values_path, batcher):
        try:
            self._handle = open(values_path, "wb")
        except OSError as error:
            raise ExcursionError(
                "cannot create snapshot: " + str(error)) from None
        self._hasher = hashlib.sha256()
        self._batcher = batcher
        self._builder = IndexBuilder(batcher.emit)
        self._buffer = []
        self._base = 0  # absolute index of buffer[0]
        self.top_level = 0

    def feed(self, values):
        self._buffer.extend(values)
        while len(self._buffer) >= _FLUSH_VALUES:
            self._drain(_FLUSH_VALUES)

    def _drain(self, count):
        block = self._buffer[:count]
        del self._buffer[:count]
        payload = struct.pack("<%dd" % count, *block)
        try:
            self._handle.write(payload)
        except OSError as error:
            raise ExcursionError(
                "cannot write snapshot: " + str(error)) from None
        self._hasher.update(payload)
        for offset in range(0, count, LEAF_SIZE):
            group = block[offset:offset + LEAF_SIZE]
            peak = max(group)
            first = group.index(peak)
            floor = min(group)
            start = self._base + offset
            self._builder.feed_leaf(
                (start, start + len(group), peak, start + first, floor))
        self._base += count

    def finish(self):
        if self._buffer:
            self._drain(len(self._buffer))
        self.top_level = self._builder.finish()
        self._batcher.flush()
        try:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._handle.close()
        except OSError as error:
            raise ExcursionError(
                "cannot finish snapshot: " + str(error)) from None

    def abort(self):
        try:
            self._handle.close()
        except OSError:
            pass

    @property
    def values_sha256(self):
        return self._hasher.hexdigest()


def _calibrate(arr, channel_index, channel_count, spec, dtype, point_base):
    """Calibrate one channel out of an interleaved native-order array.

    Produces exactly the decoder's per-point ``raw*scale + offset`` values;
    non-finite raw or calibrated values reject the whole build.
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


def _decode_and_index(manifest, raw_path, sinks):
    """Stream raw.bin once: verify length/SHA, calibrate, feed the sinks."""
    channel_specs = manifest["channels"]
    channel_count = len(channel_specs)
    width = wp.WIDTH[manifest["dtype"]]
    frame_size = channel_count * width
    typecode = _TYPECODES[manifest["dtype"]]
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
                chunk = stream.read(_READ_CHUNK)
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
            for channel_index, spec in enumerate(channel_specs):
                calibrated = _calibrate(arr, channel_index, channel_count,
                                        spec, manifest["dtype"], points)
                sinks[channel_index].feed(calibrated)
            points += frames
    if carry:
        _fail("raw.bin has %d trailing bytes that do not form a whole frame"
              % len(carry))
    if total != manifest["raw_bytes"]:
        _fail("raw.bin length %d does not match manifest raw_bytes %d"
              % (total, manifest["raw_bytes"]))
    if hasher.hexdigest() != manifest["raw_sha256"]:
        _fail("raw.bin SHA256 does not match manifest raw_sha256; "
              "refusing to archive an unverified payload")
    if points != manifest["points"]:
        _fail("decoded %d points but manifest declares %d"
              % (points, manifest["points"]))


def _process_source(connection, staging, source_seq, source_id, manifest,
                    manifest_sha, raw_path):
    channels = manifest["channels"]
    sinks = []
    try:
        for channel_index in range(len(channels)):
            values_path = staging / values_file_name(source_seq,
                                                     channel_index)
            batcher = NodeBatcher(connection, source_seq, channel_index)
            sinks.append(_ChannelSink(values_path, batcher))
        _decode_and_index(manifest, raw_path, sinks)
        for sink in sinks:
            sink.finish()
    except BaseException:
        for sink in sinks:
            sink.abort()
        raise

    connection.execute(
        "INSERT INTO sources(source_seq, source_id, capture_id, "
        "sample_rate, points, dtype, byte_order, raw_bytes, raw_sha256, "
        "manifest_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source_seq, source_id, manifest["capture_id"],
         manifest["sample_rate"], manifest["points"], manifest["dtype"],
         manifest["byte_order"], manifest["raw_bytes"],
         manifest["raw_sha256"], manifest_sha))
    for channel_index, (spec, sink) in enumerate(zip(channels, sinks)):
        connection.execute(
            "INSERT INTO channels(source_seq, channel_index, name, scale, "
            "offset, top_level, values_file, values_sha256) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (source_seq, channel_index, spec["name"], float(spec["scale"]),
             float(spec["offset"]), sink.top_level,
             values_file_name(source_seq, channel_index),
             sink.values_sha256))


def build_archive(sources_path, archive_dir):
    """Build the archive; return the JSON-ready publication summary."""
    entries = load_sources(sources_path)
    archive_dir = Path(archive_dir)
    if archive_dir.exists() or archive_dir.is_symlink():
        _fail("archive directory already exists; refusing to overwrite: %s"
              % archive_dir)
    staging = archive_dir.with_name(archive_dir.name + STAGING_SUFFIX)
    if staging.exists():
        _fail("staging directory already exists from a previous failed "
              "build; remove it first: %s" % staging)

    # Pass 1: read and validate every manifest before touching any raw
    # payload, so the aggregate limits are enforced up front.
    plans = []
    total_raw = 0
    for source_id, package_dir in entries:
        manifest_obj, manifest_sha = _read_manifest(package_dir)
        manifest = validate_manifest(manifest_obj)
        raw_path = _check_raw_path(package_dir)
        total_raw += manifest["raw_bytes"]
        if total_raw > MAX_RAW_TOTAL_BYTES:
            _fail("aggregate raw_bytes of all sources exceeds 1 GiB")
        plans.append((source_id, manifest, manifest_sha, raw_path))

    connection = None
    published = False
    try:
        staging.mkdir(parents=True)
        (staging / VALUES_DIR).mkdir()
        connection = sqlite3.connect(str(staging / CATALOG_NAME))
        connection.executescript(SCHEMA)
        connection.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            [("archive_version", str(ARCHIVE_VERSION)),
             ("leaf_size", str(LEAF_SIZE)),
             ("fanout", str(FANOUT)),
             ("source_count", str(len(plans)))])
        for source_seq, (source_id, manifest, manifest_sha,
                         raw_path) in enumerate(plans):
            _process_source(connection, staging, source_seq, source_id,
                            manifest, manifest_sha, raw_path)
            connection.commit()
        connection.close()
        connection = None
        _fsync_dir(staging)
        archive_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, archive_dir)
        _fsync_dir(archive_dir.parent)
        published = True
    finally:
        if connection is not None:
            connection.close()
        if not published:
            # A failed build never leaves a queryable half-product: the
            # staging directory is removed (an abrupt kill may leave the
            # identifiable *.staging remnant, which is never renamed).
            shutil.rmtree(staging, ignore_errors=True)

    return {
        "archive": str(archive_dir),
        "source_count": len(plans),
        "total_raw_bytes": total_raw,
        "sources": [
            {
                "source_id": source_id,
                "capture_id": manifest["capture_id"],
                "points": manifest["points"],
                "channels": [spec["name"] for spec in manifest["channels"]],
                "raw_sha256": manifest["raw_sha256"],
                "manifest_sha256": manifest_sha,
            }
            for source_id, manifest, manifest_sha, _ in plans
        ],
    }
