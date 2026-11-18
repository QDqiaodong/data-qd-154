"""Build the two published artefacts and publish them atomically.

``report.json`` records every verified source (sorted by ``source_id``) and
every bundle (sorted by ``bundle_id``) with its status, minimal offset vector
or negative-cycle evidence.  ``events.ndjson`` holds only the events of
feasible bundles, one compact JSON object per line, sorted by
``(bundle_id, event_id)``.

Both files are written into ``DIR.staging`` and only after everything has
been computed and verified is the whole directory renamed to ``DIR``.  An
existing output directory is never overwritten and a failed run leaves no
half-product that could be mistaken for success.
"""
import json
import os
import shutil
from fractions import Fraction
from pathlib import Path

from .errors import ClockJoinError
from .evaluate import cycle_evidence

REPORT_NAME = "report.json"
EVENTS_NAME = "events.ndjson"
STAGING_SUFFIX = ".staging"
SCHEMA_VERSION = 1


def _fail(message):
    raise ClockJoinError(message)


def _fsync_dir(path):
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _event_time(offset, index, sample_rate):
    return Fraction(offset) + Fraction(10 ** 9 * index, sample_rate)


def build_documents(job, infos, ordered_sources, results, values):
    """Return ``(report_object, [event_line_objects])`` fully in memory.

    The two documents are small by contract (<=32 sources, <=128 bundles and
    <=8192 events), so assembling them before publication cannot approach the
    RSS budget; the large raw payloads were streamed earlier.  ``values`` maps
    ``(source_id, channel_index, index)`` to the calibrated binary64 value
    captured while streaming.
    """
    report_sources = []
    for source_id in ordered_sources:
        info = infos[source_id]
        report_sources.append({
            "source_id": source_id,
            "capture_id": info.manifest["capture_id"],
            "sample_rate": info.manifest["sample_rate"],
            "points": info.manifest["points"],
            "raw_sha256": info.manifest["raw_sha256"],
            "manifest_sha256": info.manifest_sha256,
        })

    bundles_by_id = {bundle.bundle_id: bundle for bundle in job.bundles}
    report_bundles = []
    events = []
    for bundle_id in sorted(bundles_by_id):
        bundle = bundles_by_id[bundle_id]
        result = results[bundle_id]
        if result.status == "impossible":
            offsets = None
            cycle = cycle_evidence(result.cycle)
        else:
            offsets = [
                {"source_id": source_id,
                 "offset_ns": _rational(result.offsets[
                     infos[source_id].node_index])}
                for source_id in ordered_sources
            ]
            cycle = None
            for event in sorted(bundle.events,
                                key=lambda ev: ev.event_id):
                info = infos[event.source_id]
                offset = result.offsets[info.node_index]
                channel_index = info.channel_index[event.channel]
                events.append({
                    "bundle_id": bundle_id,
                    "event_id": event.event_id,
                    "source_id": event.source_id,
                    "channel": event.channel,
                    "index": event.index,
                    "time_ns": _rational(_event_time(
                        offset, event.index, info.sample_rate)),
                    "value": values[
                        (event.source_id, channel_index, event.index)],
                })
        report_bundles.append({
            "bundle_id": bundle_id,
            "status": result.status,
            "offsets": offsets,
            "cycle": cycle,
        })

    report = {
        "schema_version": SCHEMA_VERSION,
        "reference": job.reference,
        "sources": report_sources,
        "bundles": report_bundles,
    }
    events.sort(key=lambda record: (record["bundle_id"], record["event_id"]))
    return report, events


def _rational(value):
    frac = Fraction(value)
    return {"num": frac.numerator, "den": frac.denominator}


def _serialize_report(report):
    return (json.dumps(report, sort_keys=True, ensure_ascii=False,
                       allow_nan=False, indent=2) + "\n").encode("utf-8")


def _serialize_events(events):
    lines = [json.dumps(record, sort_keys=True, ensure_ascii=False,
                        allow_nan=False, separators=(",", ":"))
             for record in events]
    if not lines:
        return b""
    return ("\n".join(lines) + "\n").encode("utf-8")


def publish(output_dir, report, events, forbidden_roots):
    """Publish both artefacts atomically into a brand-new ``output_dir``."""
    output_dir = Path(output_dir)
    if output_dir.is_symlink():
        _fail("output path must not be a symbolic link: %s" % output_dir)
    if output_dir.exists():
        _fail("output directory already exists; refusing to overwrite: %s"
              % output_dir)
    target = output_dir.resolve()
    for root in forbidden_roots:
        root = Path(root).resolve()
        if target == root or root in target.parents or target in root.parents:
            _fail("output directory must not be an input package directory "
                  "or one of its ancestors/descendants: %s" % output_dir)

    staging = output_dir.with_name(output_dir.name + STAGING_SUFFIX)
    if staging.exists() or staging.is_symlink():
        _fail("staging directory already exists from a previous failed run; "
              "remove it first: %s" % staging)

    report_bytes = _serialize_report(report)
    events_bytes = _serialize_events(events)
    published = False
    try:
        staging.mkdir(parents=True)
        _write_file(staging / REPORT_NAME, report_bytes)
        _write_file(staging / EVENTS_NAME, events_bytes)
        _fsync_dir(staging)
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, output_dir)
        _fsync_dir(output_dir.parent)
        published = True
    finally:
        if not published:
            shutil.rmtree(staging, ignore_errors=True)


def _write_file(path, payload):
    try:
        with open(path, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise ClockJoinError("cannot write %s: %s" % (path.name, error)) \
            from None
