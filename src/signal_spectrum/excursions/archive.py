"""Archive storage: SQLite catalog, interval-summary index and value reader.

On-disk layout of a published archive directory::

    DIR/
        catalog.db          SQLite: meta, sources, channels, nodes
        values/
            <source_seq>_<channel_index>.bin
                            little-endian float64 calibrated snapshot,
                            exactly ``points`` values per channel

The ``nodes`` table is the persistent interval-summary index.  Level 0 nodes
are leaf segments of at most :data:`LEAF_SIZE` consecutive samples; each
parent summarizes up to :data:`FANOUT` consecutive children.  Every node
carries its own absolute sample range ``[start, stop)`` plus ``max_value``,
``max_index`` (earliest position of the maximum) and ``min_value`` -- nodes
are real interval summaries, never decorative directory entries.

All query-side storage access goes through :class:`ArchiveReader`, the single
layer that also accumulates the logical access counters (``node_reads``,
``leaf_reads``, ``value_reads``).  Counters record logical records/values
handed to the query engine; SQLite page caching or OS caching underneath
does not change them.
"""
import sqlite3
import struct
import urllib.parse
from pathlib import Path

from .errors import ExcursionError

ARCHIVE_VERSION = 1
LEAF_SIZE = 256
FANOUT = 64
CATALOG_NAME = "catalog.db"
VALUES_DIR = "values"
STAGING_SUFFIX = ".staging"
_VALUE_STRUCT_FMT = "<%dd"
_VALUE_WIDTH = 8

SCHEMA = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE sources (
    source_seq INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL UNIQUE,
    capture_id TEXT NOT NULL,
    sample_rate INTEGER NOT NULL,
    points INTEGER NOT NULL,
    dtype TEXT NOT NULL,
    byte_order TEXT NOT NULL,
    raw_bytes INTEGER NOT NULL,
    raw_sha256 TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL
);
CREATE TABLE channels (
    source_seq INTEGER NOT NULL,
    channel_index INTEGER NOT NULL,
    name TEXT NOT NULL,
    scale REAL NOT NULL,
    offset REAL NOT NULL,
    top_level INTEGER NOT NULL,
    values_file TEXT NOT NULL,
    values_sha256 TEXT NOT NULL,
    PRIMARY KEY (source_seq, channel_index),
    UNIQUE (source_seq, name)
);
CREATE TABLE nodes (
    source_seq INTEGER NOT NULL,
    channel_index INTEGER NOT NULL,
    level INTEGER NOT NULL,
    node_index INTEGER NOT NULL,
    start INTEGER NOT NULL,
    stop INTEGER NOT NULL,
    max_value REAL NOT NULL,
    max_index INTEGER NOT NULL,
    min_value REAL NOT NULL,
    PRIMARY KEY (source_seq, channel_index, level, node_index)
);
"""

NODE_INSERT = (
    "INSERT INTO nodes(source_seq, channel_index, level, node_index, "
    "start, stop, max_value, max_index, min_value) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def values_file_name(source_seq, channel_index):
    return "%s/%d_%d.bin" % (VALUES_DIR, source_seq, channel_index)


def merge_summaries(summaries):
    """Merge ordered, contiguous child summaries into their parent.

    ``summaries`` are ``(start, stop, max_value, max_index, min_value)``
    tuples in ascending order covering a contiguous range.  On tied maxima
    the earliest (leftmost) position wins.
    """
    start = summaries[0][0]
    stop = summaries[-1][1]
    max_value, max_index = summaries[0][2], summaries[0][3]
    min_value = summaries[0][4]
    for _, _, child_max, child_max_index, child_min in summaries[1:]:
        if child_max > max_value:
            max_value, max_index = child_max, child_max_index
        if child_min < min_value:
            min_value = child_min
    return (start, stop, max_value, max_index, min_value)


class IndexBuilder:
    """Streaming level-by-level index construction for one channel.

    Leaf summaries are fed in ascending order; full groups of
    :data:`FANOUT` nodes are merged into the next level as soon as they are
    complete.  Memory use is bounded by ``FANOUT * levels`` summaries --
    the channel's node set is never held in memory.
    """

    def __init__(self, emit):
        """``emit(level, node_index, summary)`` persists one node row."""
        self._emit = emit
        self._pending = {}  # level -> ungrouped child summaries
        self._count = {}    # level -> nodes emitted at that level

    def _append(self, summary, level):
        node_index = self._count.get(level, 0)
        self._emit(level, node_index, summary)
        self._count[level] = node_index + 1
        pending = self._pending.setdefault(level + 1, [])
        pending.append(summary)
        if len(pending) == FANOUT:
            parent = merge_summaries(pending)
            self._pending[level + 1] = []
            self._append(parent, level + 1)

    def feed_leaf(self, summary):
        """Feed one leaf summary (level 0) covering <= LEAF_SIZE samples."""
        self._append(summary, 0)

    def finish(self):
        """Flush partial groups; return the top level of this channel."""
        level = 1
        while True:
            pending = self._pending.get(level, [])
            emitted = self._count.get(level, 0)
            if not pending:
                if emitted == 0:
                    break
                level += 1
                continue
            if emitted == 0:
                # These pending nodes *are* the top of the tree (fewer than
                # FANOUT nodes at the level below); no extra parent level.
                break
            parent = merge_summaries(pending)
            self._pending[level] = []
            self._append(parent, level)
            level += 1
        top = 0
        for known_level, count in self._count.items():
            if count and known_level > top:
                top = known_level
        return top


class NodeBatcher:
    """Batches node rows into executemany inserts (bounded memory)."""

    def __init__(self, connection, source_seq, channel_index, batch=20000):
        self._conn = connection
        self._seq = source_seq
        self._ch = channel_index
        self._batch = batch
        self._rows = []

    def emit(self, level, node_index, summary):
        start, stop, max_value, max_index, min_value = summary
        self._rows.append((self._seq, self._ch, level, node_index,
                           start, stop, max_value, max_index, min_value))
        if len(self._rows) >= self._batch:
            self.flush()

    def flush(self):
        if self._rows:
            self._conn.executemany(NODE_INSERT, self._rows)
            self._rows = []


class ArchiveReader:
    """Uniform query-side storage access layer with logical counters.

    Every persistent summary record or point value the query engine obtains
    passes through this class and is counted exactly when handed out, no
    matter which cache (SQLite page cache, OS page cache) served the bytes.
    Constant catalog lookups that only locate a source/channel are not
    counted as node reads.
    """

    def __init__(self, archive_dir):
        self.archive_dir = Path(archive_dir)
        catalog = self.archive_dir / CATALOG_NAME
        if not catalog.is_file():
            raise ExcursionError(
                "archive catalog not found: %s" % catalog)
        uri = "file:%s?mode=ro" % urllib.parse.quote(str(catalog))
        try:
            self.conn = sqlite3.connect(uri, uri=True)
            rows = self.conn.execute("SELECT key, value FROM meta").fetchall()
        except sqlite3.Error as error:
            raise ExcursionError(
                "archive catalog is corrupt: %s" % error) from None
        meta = dict(rows)
        version = meta.get("archive_version")
        if version != str(ARCHIVE_VERSION):
            raise ExcursionError(
                "unsupported archive version: %r" % (version,))
        try:
            self.fanout = int(meta["fanout"])
            self.leaf_size = int(meta["leaf_size"])
        except (KeyError, ValueError):
            raise ExcursionError(
                "archive catalog is corrupt: bad fanout/leaf_size") from None
        if not (1 <= self.fanout <= 1 << 20) or self.leaf_size != LEAF_SIZE:
            raise ExcursionError(
                "archive catalog is corrupt: fanout/leaf_size out of range")
        self.node_reads = 0
        self.leaf_reads = 0
        self.value_reads = 0
        self._value_files = {}

    def close(self):
        for handle, _ in self._value_files.values():
            try:
                handle.close()
            except OSError:
                pass
        self._value_files.clear()
        self.conn.close()

    # -- constant catalog lookups (never counted as node reads) -------------

    def find_source(self, source_id):
        try:
            row = self.conn.execute(
                "SELECT source_seq, capture_id, sample_rate, points, "
                "raw_sha256, manifest_sha256 FROM sources "
                "WHERE source_id = ?", (source_id,)).fetchone()
        except sqlite3.Error as error:
            raise ExcursionError(
                "archive catalog is corrupt: %s" % error) from None
        if row is None:
            return None
        return {"source_seq": row[0], "capture_id": row[1],
                "sample_rate": row[2], "points": row[3],
                "raw_sha256": row[4], "manifest_sha256": row[5]}

    def find_channel(self, source_seq, name):
        try:
            row = self.conn.execute(
                "SELECT channel_index, top_level, values_file FROM channels "
                "WHERE source_seq = ? AND name = ?",
                (source_seq, name)).fetchone()
        except sqlite3.Error as error:
            raise ExcursionError(
                "archive catalog is corrupt: %s" % error) from None
        if row is None:
            return None
        return {"channel_index": row[0], "top_level": row[1],
                "values_file": row[2]}

    # -- counted summary reads ------------------------------------------------

    def top_nodes(self, source_seq, channel_index, top_level):
        rows = self._node_rows(source_seq, channel_index, top_level,
                               None, None)
        return rows

    def child_nodes(self, source_seq, channel_index, level, parent_index):
        low = parent_index * self.fanout
        high = low + self.fanout - 1
        return self._node_rows(source_seq, channel_index, level, low, high)

    def _node_rows(self, source_seq, channel_index, level, low, high):
        if low is None:
            statement = (
                "SELECT node_index, start, stop, max_value, max_index, "
                "min_value FROM nodes WHERE source_seq = ? "
                "AND channel_index = ? AND level = ? ORDER BY node_index")
            params = (source_seq, channel_index, level)
        else:
            statement = (
                "SELECT node_index, start, stop, max_value, max_index, "
                "min_value FROM nodes WHERE source_seq = ? "
                "AND channel_index = ? AND level = ? "
                "AND node_index BETWEEN ? AND ? ORDER BY node_index")
            params = (source_seq, channel_index, level, low, high)
        try:
            rows = self.conn.execute(statement, params).fetchall()
        except sqlite3.Error as error:
            raise ExcursionError(
                "archive catalog is corrupt: %s" % error) from None
        self.node_reads += len(rows)
        return rows

    # -- counted point-value reads --------------------------------------------

    def leaf_values(self, source_seq, channel_index, values_file, points,
                    start, count):
        """Return ``count`` calibrated values starting at absolute ``start``.

        Counts as one leaf visit and ``count`` value reads.
        """
        key = (source_seq, channel_index)
        entry = self._value_files.get(key)
        if entry is None:
            path = self.archive_dir / values_file
            try:
                handle = open(path, "rb")
                size = path.stat().st_size
            except OSError as error:
                raise ExcursionError(
                    "archive snapshot is missing: %s" % error) from None
            expected = points * _VALUE_WIDTH
            if size != expected:
                handle.close()
                raise ExcursionError(
                    "archive snapshot %s is corrupt: size %d, expected %d"
                    % (values_file, size, expected))
            entry = (handle, points)
            self._value_files[key] = entry
        handle = entry[0]
        try:
            handle.seek(start * _VALUE_WIDTH)
            data = handle.read(count * _VALUE_WIDTH)
        except OSError as error:
            raise ExcursionError(
                "archive snapshot read failed: %s" % error) from None
        if len(data) != count * _VALUE_WIDTH:
            raise ExcursionError(
                "archive snapshot ended early at sample %d" % start)
        self.leaf_reads += 1
        self.value_reads += count
        return struct.unpack(_VALUE_STRUCT_FMT % count, data)
