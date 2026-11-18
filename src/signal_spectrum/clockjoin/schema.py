"""Strict reader for the ``signal-clock-join`` job file.

Top-level object with exactly ``reference``, ``sources``, ``anchors`` and
``bundles``.  Everything is validated before any package byte is trusted:
duplicate JSON keys, unknown fields, booleans where numbers are required and
``NaN``/``Infinity`` are all rejected.  Paths, ``source_id`` values and every
other identifier are kept verbatim -- nothing is silently trimmed or
rewritten.
"""
import json
from fractions import Fraction
from pathlib import Path

from ..waveform import protocol as wp
from .errors import ClockJoinError

JOB_MAX_BYTES = 8 * 1024 * 1024
MAX_SOURCES = 32
MIN_SOURCES = 1
MAX_ANCHORS = 512
MAX_BUNDLES = 128
MIN_EVENTS = 1
MAX_EVENTS = 64
MAX_RELATIONS = 256
NUM_ABS_LIMIT = 10 ** 18
DEN_LIMIT = 10 ** 9


def _fail(message):
    raise ClockJoinError(message)


def _require_exact(obj, keys, label):
    if not isinstance(obj, dict):
        _fail("%s must be a JSON object" % label)
    if set(obj) != set(keys):
        _fail("%s must contain exactly %s"
              % (label, ",".join(sorted(keys))))


def _ident(value, label):
    try:
        return wp.strict_name(value, label)
    except wp.ProtocolError as error:
        raise ClockJoinError(str(error)) from None


def _int(value, minimum, maximum, label):
    if type(value) is not int or type(value) is bool:
        _fail("%s must be an integer" % label)
    if not minimum <= value <= maximum:
        _fail("%s outside allowed range %d..%d" % (label, minimum, maximum))
    return value


def _rational(value, label):
    """Parse ``{num, den}`` into an exact positive-denominator Fraction."""
    _require_exact(value, ("num", "den"), label)
    num = value["num"]
    den = value["den"]
    if type(num) is not int or type(num) is bool:
        _fail("%s.num must be an integer" % label)
    if abs(num) > NUM_ABS_LIMIT:
        _fail("%s.num must have absolute value <= %d" % (label,
                                                         NUM_ABS_LIMIT))
    if type(den) is not int or type(den) is bool:
        _fail("%s.den must be an integer" % label)
    if not 1 <= den <= DEN_LIMIT:
        _fail("%s.den must be in 1..%d" % (label, DEN_LIMIT))
    # Fraction normalises the sign; comparison is by rational value, so an
    # unreduced input is accepted but judged equal by value.
    return Fraction(num, den)


def _endpoint(value, label):
    _require_exact(value, ("source_id", "index"), label)
    source_id = _ident(value["source_id"], label + ".source_id")
    index = _int(value["index"], 0, 10 ** 15, label + ".index")
    return source_id, index


def _parse_bytes(raw):
    if len(raw) > JOB_MAX_BYTES:
        _fail("job file exceeds %d bytes" % JOB_MAX_BYTES)

    def unique(pairs):
        seen = set()
        for key, value in pairs:
            if key in seen:
                _fail("duplicate JSON key: " + str(key))
            seen.add(key)
        return dict(pairs)

    def reject_constant(value):
        _fail("non-finite JSON constant is not allowed: " + value)

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                          parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ClockJoinError("invalid job JSON: " + str(error)) from None


class Anchor:
    __slots__ = ("anchor_id", "left_source", "left_index", "right_source",
                 "right_index", "lower", "upper")

    def __init__(self, anchor_id, left, right, lower, upper):
        self.anchor_id = anchor_id
        (self.left_source, self.left_index) = left
        (self.right_source, self.right_index) = right
        self.lower = lower
        self.upper = upper


class Relation:
    __slots__ = ("relation_id", "left_event", "right_event", "lower", "upper")

    def __init__(self, relation_id, left_event, right_event, lower, upper):
        self.relation_id = relation_id
        self.left_event = left_event
        self.right_event = right_event
        self.lower = lower
        self.upper = upper


class Event:
    __slots__ = ("event_id", "source_id", "channel", "index")

    def __init__(self, event_id, source_id, channel, index):
        self.event_id = event_id
        self.source_id = source_id
        self.channel = channel
        self.index = index


class Bundle:
    __slots__ = ("bundle_id", "events", "relations")

    def __init__(self, bundle_id, events, relations):
        self.bundle_id = bundle_id
        self.events = events
        self.relations = relations


class Job:
    __slots__ = ("reference", "sources", "anchors", "bundles", "base_dir")

    def __init__(self, reference, sources, anchors, bundles, base_dir):
        self.reference = reference
        self.sources = sources  # list of (source_id, package_path)
        self.anchors = anchors
        self.bundles = bundles
        self.base_dir = base_dir


def _parse_sources(entries):
    if type(entries) is not list or not (MIN_SOURCES <= len(entries)
                                         <= MAX_SOURCES):
        _fail("sources must be an array of %d..%d entries"
              % (MIN_SOURCES, MAX_SOURCES))
    seen = set()
    result = []
    for index, entry in enumerate(entries):
        label = "sources[%d]" % index
        _require_exact(entry, ("source_id", "package"), label)
        source_id = _ident(entry["source_id"], label + ".source_id")
        if source_id in seen:
            _fail("duplicate source_id: " + source_id)
        seen.add(source_id)
        package = entry["package"]
        if not isinstance(package, str) or not package:
            _fail("%s.package must be a non-empty path string" % label)
        if "\x00" in package:
            _fail("%s.package contains a NUL character" % label)
        result.append((source_id, package))
    return result


def _parse_anchors(entries):
    if type(entries) is not list or len(entries) > MAX_ANCHORS:
        _fail("anchors must be an array of at most %d entries" % MAX_ANCHORS)
    seen = set()
    result = []
    for index, entry in enumerate(entries):
        label = "anchors[%d]" % index
        _require_exact(entry,
                       ("anchor_id", "left", "right", "lower_ns", "upper_ns"),
                       label)
        anchor_id = _ident(entry["anchor_id"], label + ".anchor_id")
        if anchor_id in seen:
            _fail("duplicate anchor_id: " + anchor_id)
        seen.add(anchor_id)
        left = _endpoint(entry["left"], label + ".left")
        right = _endpoint(entry["right"], label + ".right")
        lower = _rational(entry["lower_ns"], label + ".lower_ns")
        upper = _rational(entry["upper_ns"], label + ".upper_ns")
        if lower > upper:
            _fail("%s.lower_ns must not exceed upper_ns" % label)
        result.append(Anchor(anchor_id, left, right, lower, upper))
    return result


def _parse_bundles(entries):
    if type(entries) is not list or not (1 <= len(entries) <= MAX_BUNDLES):
        _fail("bundles must be an array of 1..%d entries" % MAX_BUNDLES)
    bundle_ids = set()
    result = []
    for index, entry in enumerate(entries):
        label = "bundles[%d]" % index
        _require_exact(entry, ("bundle_id", "events", "relations"), label)
        bundle_id = _ident(entry["bundle_id"], label + ".bundle_id")
        if bundle_id in bundle_ids:
            _fail("duplicate bundle_id: " + bundle_id)
        bundle_ids.add(bundle_id)

        events_raw = entry["events"]
        if type(events_raw) is not list or not (
                MIN_EVENTS <= len(events_raw) <= MAX_EVENTS):
            _fail("%s.events must be an array of %d..%d entries"
                  % (label, MIN_EVENTS, MAX_EVENTS))
        events = []
        event_ids = set()
        for eidx, ev in enumerate(events_raw):
            elabel = "%s.events[%d]" % (label, eidx)
            _require_exact(ev, ("event_id", "source_id", "channel", "index"),
                           elabel)
            event_id = _ident(ev["event_id"], elabel + ".event_id")
            if event_id in event_ids:
                _fail("%s.event_id must be unique within its bundle"
                      % elabel)
            event_ids.add(event_id)
            source_id = _ident(ev["source_id"], elabel + ".source_id")
            channel = _ident(ev["channel"], elabel + ".channel")
            point = _int(ev["index"], 0, 10 ** 15, elabel + ".index")
            events.append(Event(event_id, source_id, channel, point))

        rel_raw = entry["relations"]
        if type(rel_raw) is not list or len(rel_raw) > MAX_RELATIONS:
            _fail("%s.relations must be an array of at most %d entries"
                  % (label, MAX_RELATIONS))
        relations = []
        relation_ids = set()
        for ridx, rel in enumerate(rel_raw):
            rlabel = "%s.relations[%d]" % (label, ridx)
            _require_exact(rel, ("relation_id", "left", "right", "lower_ns",
                                 "upper_ns"), rlabel)
            relation_id = _ident(rel["relation_id"], rlabel + ".relation_id")
            if relation_id in relation_ids:
                _fail("%s.relation_id must be unique within its bundle"
                      % rlabel)
            relation_ids.add(relation_id)
            left_event = _ident(rel["left"], rlabel + ".left")
            right_event = _ident(rel["right"], rlabel + ".right")
            lower = _rational(rel["lower_ns"], rlabel + ".lower_ns")
            upper = _rational(rel["upper_ns"], rlabel + ".upper_ns")
            if lower > upper:
                _fail("%s.lower_ns must not exceed upper_ns" % rlabel)
            relations.append(Relation(relation_id, left_event, right_event,
                                      lower, upper))
        result.append(Bundle(bundle_id, events, relations))
    return result


def load_job(job_path):
    """Read, validate and structurally resolve one job file."""
    job_path = Path(job_path)
    try:
        with open(job_path, "rb") as stream:
            raw = stream.read(JOB_MAX_BYTES + 1)
    except OSError as error:
        raise ClockJoinError("cannot read job file: " + str(error)) from None
    obj = _parse_bytes(raw)
    _require_exact(obj, ("reference", "sources", "anchors", "bundles"),
                   "job")
    sources = _parse_sources(obj["sources"])
    reference = _ident(obj["reference"], "reference")
    source_ids = [source_id for source_id, _ in sources]
    if reference not in set(source_ids):
        _fail("reference must name one of the source_id values")
    anchors = _parse_anchors(obj["anchors"])
    bundles = _parse_bundles(obj["bundles"])
    base_dir = job_path.resolve().parent
    resolved = []
    for source_id, package in sources:
        path = Path(package)
        if not path.is_absolute():
            path = base_dir / path
        resolved.append((source_id, path))
    # Structural reference checks that do not depend on package contents.
    known = set(source_ids)
    for anchor in anchors:
        for side, sid in (("left", anchor.left_source),
                          ("right", anchor.right_source)):
            if sid not in known:
                _fail("anchor %s %s references unknown source_id: %s"
                      % (anchor.anchor_id, side, sid))
    for bundle in bundles:
        by_id = {}
        for event in bundle.events:
            if event.source_id not in known:
                _fail("bundle %s event %s references unknown source_id: %s"
                      % (bundle.bundle_id, event.event_id, event.source_id))
            by_id[event.event_id] = event
        for relation in bundle.relations:
            for side, eid in (("left", relation.left_event),
                              ("right", relation.right_event)):
                if eid not in by_id:
                    _fail("bundle %s relation %s %s references unknown "
                          "event_id: %s"
                          % (bundle.bundle_id, relation.relation_id, side, eid))
    return Job(reference, resolved, anchors, bundles, base_dir)
