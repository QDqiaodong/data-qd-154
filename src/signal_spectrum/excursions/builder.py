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
import hashlib
import json
import os
import shutil
import sqlite3
import struct
from pathlib import Path

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
from .manifest import validate_manifest
from .package import (
    calibrate_channel,
    check_raw_path,
    iter_source_chunks,
    read_manifest,
)
from .sources import MAX_RAW_TOTAL_BYTES, load_sources

_FLUSH_VALUES = 8192  # multiple of LEAF_SIZE; per-channel buffer bound


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


def _decode_and_index(manifest, raw_path, sinks):
    """Stream raw.bin once: verify length/SHA, calibrate, feed the sinks."""
    channel_specs = manifest["channels"]
    channel_count = len(channel_specs)
    for point_base, _, arr in iter_source_chunks(manifest, raw_path):
        for channel_index, spec in enumerate(channel_specs):
            calibrated = calibrate_channel(
                arr, channel_index, channel_count, spec,
                manifest["dtype"], point_base)
            sinks[channel_index].feed(calibrated)


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
        manifest_obj, manifest_sha = read_manifest(package_dir)
        manifest = validate_manifest(manifest_obj)
        raw_path = check_raw_path(package_dir)
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
