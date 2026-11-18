"""Load verified packages, validate references and classify every bundle.

Everything in this module is exact: offsets, deltas, weights, totals and
event times are :class:`fractions.Fraction` values.  No floating point
tolerance participates in any feasibility decision; the only binary64 values
produced are the calibrated event ``value`` fields, which follow the
baseline ``raw*scale + offset`` semantics verbatim.

Memory discipline: each source ``raw.bin`` is streamed exactly once through
:func:`iter_source_chunks`; only the requested event values (at most
``128*64`` distinct samples) and one chunk worth of calibration buffers are
held at a time.  A whole package or channel is never loaded.
"""
from fractions import Fraction

from . import clock
from .errors import ClockJoinError
from ..excursions.manifest import validate_manifest
from ..excursions.package import (
    calibrate_channel,
    check_raw_path,
    iter_source_chunks,
    read_manifest,
)

MAX_RAW_TOTAL_BYTES = 1024 ** 3  # 1 GiB across all source packages
NS_PER_SECOND = 10 ** 9


def _fail(message):
    raise ClockJoinError(message)


class SourceInfo:
    __slots__ = ("source_id", "package", "manifest", "manifest_sha256",
                 "raw_path", "channels", "channel_index", "node_index")

    def __init__(self, source_id, package, manifest, manifest_sha256,
                 raw_path):
        self.source_id = source_id
        self.package = package
        self.manifest = manifest
        self.manifest_sha256 = manifest_sha256
        self.raw_path = raw_path
        self.channels = manifest["channels"]
        self.channel_index = {spec["name"]: index
                             for index, spec in enumerate(self.channels)}
        self.node_index = -1

    @property
    def sample_rate(self):
        return self.manifest["sample_rate"]

    @property
    def points(self):
        return self.manifest["points"]


def load_source_infos(job):
    """Verify every manifest before any raw byte is trusted; enforce limits."""
    infos = {}
    total_raw = 0
    for source_id, package in job.sources:
        manifest_obj, manifest_sha = read_manifest(package)
        manifest = _validate(manifest_obj)
        raw_path = check_raw_path(package)
        # Aggregate budget is summed over source entries, exactly like the
        # excursion archive builder: two source_ids may share a package, but
        # each still counts its declared raw_bytes toward the 1 GiB cap.
        total_raw += manifest["raw_bytes"]
        if total_raw > MAX_RAW_TOTAL_BYTES:
            _fail("aggregate raw_bytes of all sources exceeds 1 GiB")
        infos[source_id] = SourceInfo(source_id, package, manifest,
                                      manifest_sha, raw_path)
    return infos, total_raw


def _validate(manifest_obj):
    return validate_manifest(manifest_obj)


def validate_references(job, infos):
    """Check every anchor endpoint and every event against real packages.

    Runs for *all* bundles, including ones that will turn out impossible, so a
    bad reference is never hidden behind an infeasible bundle.
    """
    for anchor in job.anchors:
        _check_endpoint(anchor.anchor_id, "anchor", anchor.left_source,
                        anchor.left_index, infos)
        _check_endpoint(anchor.anchor_id, "anchor", anchor.right_source,
                        anchor.right_index, infos)
    for bundle in job.bundles:
        for event in bundle.events:
            info = infos.get(event.source_id)
            if info is None:
                _fail("bundle %s event %s references unknown source_id: %s"
                      % (bundle.bundle_id, event.event_id, event.source_id))
            if event.channel not in info.channel_index:
                _fail("bundle %s event %s references unknown channel %r of "
                      "source %s"
                      % (bundle.bundle_id, event.event_id, event.channel,
                         event.source_id))
            if event.index >= info.points:
                _fail("bundle %s event %s index %d outside source %s range "
                      "0..%d"
                      % (bundle.bundle_id, event.event_id, event.index,
                         event.source_id, info.points - 1))


def _check_endpoint(anchor_id, kind, source_id, index, infos):
    info = infos.get(source_id)
    if info is None:
        _fail("%s %s references unknown source_id: %s"
              % (kind, anchor_id, source_id))
    if index >= info.points:
        _fail("%s %s endpoint index %d outside source %s range 0..%d"
              % (kind, anchor_id, index, source_id, info.points - 1))


def scan_values(infos, requested):
    """Stream every source once: validate every calibrated value is finite
    and capture the requested ``(channel_index, index)`` event values.

    ``requested`` maps ``source_id -> {channel_index: {index: None}}``.  The
    returned dict maps ``(source_id, channel_index, index) -> float``.
    """
    collected = {}
    for source_id in sorted(infos):
        info = infos[source_id]
        wanted = requested.get(source_id, {})
        # Validate against declared ranges up front (already checked, but a
        # second guard keeps this function safe in isolation).
        for channel_index, indices in wanted.items():
            for index in indices:
                if index >= info.points:
                    _fail("requested index %d outside source %s"
                          % (index, source_id))
        for base, frames, arr in iter_source_chunks(info.manifest,
                                                    info.raw_path):
            for channel_index, spec in enumerate(info.channels):
                calibrated = calibrate_channel(
                    arr, channel_index, len(info.channels), spec,
                    info.manifest["dtype"], base)
                channel_wanted = wanted.get(channel_index)
                if not channel_wanted:
                    continue
                for index in channel_wanted:
                    if base <= index < base + len(calibrated):
                        # Store the calibrated value as a finite binary64,
                        # matching the existing snapshot/decoder semantics.
                        collected[(source_id, channel_index, index)] = \
                            float(calibrated[index - base])
        for channel_index, indices in wanted.items():
            for index in indices:
                if (source_id, channel_index, index) not in collected:
                    _fail("event sample %d of source %s was not decoded"
                          % (index, source_id))
    return collected


# -- graph assembly ---------------------------------------------------------

def assign_nodes(infos):
    """Assign one node per source_id in sorted order (array-order invariant)."""
    ordered = sorted(infos)
    for index, source_id in enumerate(ordered):
        infos[source_id].node_index = index
    return ordered


def _delta(info_left, index_left, info_right, index_right):
    return (Fraction(NS_PER_SECOND * index_right, info_right.sample_rate)
            - Fraction(NS_PER_SECOND * index_left, info_left.sample_rate))


def base_edges(anchors, infos):
    edges = []
    for anchor in anchors:
        left = infos[anchor.left_source]
        right = infos[anchor.right_source]
        delta = _delta(left, anchor.left_index, right, anchor.right_index)
        edges.extend(clock.constraint_edges(
            clock.ANCHOR, anchor.anchor_id,
            anchor.left_source, left.node_index,
            anchor.right_source, right.node_index,
            delta, anchor.lower, anchor.upper))
    return edges


def bundle_edges(bundle, infos):
    by_event = {event.event_id: event for event in bundle.events}
    edges = []
    for relation in bundle.relations:
        left_event = by_event[relation.left_event]
        right_event = by_event[relation.right_event]
        left = infos[left_event.source_id]
        right = infos[right_event.source_id]
        delta = _delta(left, left_event.index, right, right_event.index)
        edges.extend(clock.constraint_edges(
            clock.RELATION, relation.relation_id,
            left_event.source_id, left.node_index,
            right_event.source_id, right.node_index,
            delta, relation.lower, relation.upper))
    return edges


# -- bundle classification --------------------------------------------------

class BundleResult:
    __slots__ = ("status", "offsets", "cycle")

    def __init__(self, status, offsets, cycle):
        self.status = status
        self.offsets = offsets
        self.cycle = cycle


def classify_bundle(bundle, infos, base, node_count, ref_idx):
    """Return a :class:`BundleResult` for one bundle.

    Feasibility is decided on the joint graph of *all* base anchors plus this
    bundle's relations, with one shared offset vector -- never relation by
    relation.  ``guaranteed`` additionally requires that the base region alone
    already satisfies every relation.
    """
    by_event = {event.event_id: event for event in bundle.events}
    guaranteed = True
    for relation in bundle.relations:
        left_event = by_event[relation.left_event]
        right_event = by_event[relation.right_event]
        left = infos[left_event.source_id]
        right = infos[right_event.source_id]
        delta = _delta(left, left_event.index, right, right_event.index)
        if not clock.relation_guaranteed(
                base.dist, base.scale, left.node_index, right.node_index,
                delta, relation.lower, relation.upper):
            guaranteed = False
            break

    relations = bundle_edges(bundle, infos)
    joint = list(base.edges) + relations
    cycle = clock.find_negative_cycle(node_count, joint)
    if cycle is not None:
        return BundleResult("impossible", None, cycle)

    offsets = clock.shortest_to_reference(node_count, joint, ref_idx)
    status = "guaranteed" if guaranteed else "conditional"
    return BundleResult(status, offsets, None)


class BaseGraph:
    __slots__ = ("edges", "dist", "scale")

    def __init__(self, edges, dist, scale):
        self.edges = edges
        self.dist = dist
        self.scale = scale


def build_base(anchors, infos, node_count):
    edges = base_edges(anchors, infos)
    dist, scale = clock.floyd_base(node_count, edges)
    return BaseGraph(edges, dist, scale)


def rational(value):
    """Render an exact rational as a reduced ``{num, den}`` with den > 0."""
    frac = Fraction(value)
    return {"num": frac.numerator, "den": frac.denominator}


def edge_evidence(edge):
    return {
        "kind": edge.kind,
        "id": edge.id,
        "side": edge.side,
        "from": edge.from_id,
        "to": edge.to_id,
        "weight": rational(edge.weight),
    }


def cycle_evidence(cycle):
    total = sum((edge.weight for edge in cycle), Fraction(0))
    return {
        "edges": [edge_evidence(edge) for edge in cycle],
        "total_weight": rational(total),
    }
