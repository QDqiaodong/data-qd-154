"""Threshold-excursion query engine over the persistent interval index.

The traversal never loads a whole channel or the whole index: it walks the
summary tree from the top level, and for every node whose own stored range
is fully inside the query range it decides from the persistent summary
alone -- an all-hit node contributes its interval without touching point
values, an all-miss node is pruned.  Only boundary-clipped nodes and
undecidable leaf segments read point values, and leaves read exactly the
intersecting sub-range (at most LEAF_SIZE values per visited leaf).

Decisions per node range [s, e) fully inside the query, threshold ``t``:

* ``ge``: all-hit iff ``min_value >= t``; all-miss iff ``max_value < t``
* ``lt``: all-hit iff ``max_value < t``; all-miss iff ``min_value >= t``

A partially intersecting node is never answered from its whole-range peak;
it is descended (all-miss pruning still applies, since a miss over the
superset proves a miss over the intersection).  Adjacent hits across
leaves/nodes merge into maximal runs; each run reports its maximum value
and the earliest absolute index reaching it (also for ``lt`` runs).
"""


def _fail_unless(condition, message):
    if not condition:
        from .errors import ExcursionError
        raise ExcursionError(message)


def _scan_leaf(values, base, op, threshold):
    """Yield maximal hit runs inside one decoded leaf intersection."""
    count = len(values)
    index = 0
    if op == "ge":
        while index < count:
            value = values[index]
            if value >= threshold:
                end = index + 1
                peak, peak_index = value, index
                while end < count and values[end] >= threshold:
                    if values[end] > peak:
                        peak, peak_index = values[end], end
                    end += 1
                yield (base + index, base + end, peak, base + peak_index)
                index = end
            else:
                index += 1
    else:
        while index < count:
            value = values[index]
            if value < threshold:
                end = index + 1
                peak, peak_index = value, index
                while end < count and values[end] < threshold:
                    if values[end] > peak:
                        peak, peak_index = values[end], end
                    end += 1
                yield (base + index, base + end, peak, base + peak_index)
                index = end
            else:
                index += 1


class _Traversal:
    """Lazy ascending generator of hit intervals for one query."""

    def __init__(self, reader, source_seq, channel_index, values_file,
                 points, top_level, qstart, qstop, op, threshold):
        self.reader = reader
        self.source_seq = source_seq
        self.channel_index = channel_index
        self.values_file = values_file
        self.points = points
        self.top_level = top_level
        self.qstart = qstart
        self.qstop = qstop
        self.op = op
        self.threshold = threshold

    def intervals(self):
        nodes = self.reader.top_nodes(self.source_seq, self.channel_index,
                                      self.top_level)
        for node_index, start, stop, peak, peak_index, floor in nodes:
            yield from self._visit(self.top_level, node_index, start, stop,
                                   peak, peak_index, floor)

    def _visit(self, level, node_index, start, stop, peak, peak_index,
               floor):
        inter_start = max(start, self.qstart)
        inter_stop = min(stop, self.qstop)
        if inter_start >= inter_stop:
            return
        if self.op == "ge":
            all_miss = peak < self.threshold
            all_hit = floor >= self.threshold
        else:
            all_miss = floor >= self.threshold
            all_hit = peak < self.threshold
        if all_miss:
            return
        if start >= self.qstart and stop <= self.qstop and all_hit:
            # Fully inside the query and uniformly hitting: the persistent
            # summary answers the whole interval; no point values are read.
            yield (start, stop, peak, peak_index)
            return
        if level == 0:
            values = self.reader.leaf_values(
                self.source_seq, self.channel_index, self.values_file,
                self.points, inter_start, inter_stop - inter_start)
            yield from _scan_leaf(values, inter_start, self.op,
                                  self.threshold)
            return
        children = self.reader.child_nodes(self.source_seq,
                                           self.channel_index, level - 1,
                                           node_index)
        for child_index, cstart, cstop, cpeak, cpeak_index, cfloor in children:
            yield from self._visit(level - 1, child_index, cstart, cstop,
                                   cpeak, cpeak_index, cfloor)


def _merge_runs(intervals, limit):
    """Merge ascending adjacent hit intervals into maximal runs.

    Returns ``(runs, has_more)`` with at most ``limit`` *complete* runs: a
    run is only counted once a gap (or the range end) proves it maximal, so
    the limit-th run is never cut in the middle and the following run is
    never merged into it.
    """
    completed = []
    current = None
    has_more = False
    for interval in intervals:
        if current is not None and interval[0] == current[1]:
            if interval[2] > current[2]:
                current = (current[0], interval[1], interval[2], interval[3])
            else:
                current = (current[0], interval[1], current[2], current[3])
            continue
        if current is not None:
            completed.append(current)
            current = None
        if len(completed) == limit:
            # The next run provably exists; its extent is irrelevant.
            has_more = True
            break
        current = interval
    else:
        if current is not None:
            completed.append(current)
    return completed, has_more


def run_query(reader, source, channel, qstart, qstop, op, threshold, limit):
    """Execute one query; return the JSON-ready result payload."""
    points = source["points"]
    _fail_unless(0 <= qstart, "start must be >= 0")
    _fail_unless(qstart < qstop, "start must be < stop")
    _fail_unless(qstop <= points,
                 "stop must be <= source points (%d)" % points)
    _fail_unless(1 <= limit <= 10000, "limit must be an integer 1..10000")

    traversal = _Traversal(
        reader, source["source_seq"], channel["channel_index"],
        channel["values_file"], points, channel["top_level"],
        qstart, qstop, op, threshold)
    runs, has_more = _merge_runs(traversal.intervals(), limit)
    return {
        "source_id": None,  # filled by the caller
        "capture_id": source["capture_id"],
        "channel": None,  # filled by the caller
        "sample_rate": source["sample_rate"],
        "op": op,
        "threshold": threshold,
        "start": qstart,
        "stop": qstop,
        "limit": limit,
        "raw_sha256": source["raw_sha256"],
        "manifest_sha256": source["manifest_sha256"],
        "runs": [
            {"start": start, "stop": stop,
             "peak_value": peak, "peak_index": peak_index}
            for start, stop, peak, peak_index in runs
        ],
        "has_more": has_more,
        "node_reads": reader.node_reads,
        "leaf_reads": reader.leaf_reads,
        "value_reads": reader.value_reads,
    }
