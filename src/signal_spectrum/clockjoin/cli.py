"""``signal-clock-join``: join multi-instrument waveform packages onto one
consistent common time base using cross-instrument timing anchors.

Exit codes: 0 on success, 2 on any invalid job/JSON, reference, package
verification, base-constraint or I/O failure.  Bundle incompatibility is a
normal per-bundle result and never aborts publication.
"""
import argparse
import json
import sys
from pathlib import Path

from .errors import ClockJoinError
from .evaluate import (
    assign_nodes,
    build_base,
    classify_bundle,
    load_source_infos,
    scan_values,
    validate_references,
)
from .publish import build_documents, publish
from .schema import load_job

EXIT_FAILURE = 2
EXIT_SUCCESS = 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="signal-clock-join",
        description="Join published waveform packages into a traceable "
                    "multi-source event dataset on one common clock.")
    parser.add_argument("--source", required=True, type=Path,
                        help="job JSON describing sources, anchors and "
                             "candidate bundles")
    parser.add_argument("--output", required=True, type=Path,
                        help="new output directory for report.json and "
                             "events.ndjson (must not exist)")
    return parser


def run(job_path, output_dir):
    job = load_job(job_path)
    infos, total_raw = load_source_infos(job)
    validate_references(job, infos)
    ordered_sources = assign_nodes(infos)
    ref_idx = infos[job.reference].node_index
    node_count = len(infos)

    # Base anchor feasibility (and finite per-source bounds) is a hard input
    # error: an infeasible or unbounded base graph can never yield a joint
    # solution for any bundle.
    base = build_base(job.anchors, infos, node_count)

    results = {}
    for bundle in job.bundles:
        results[bundle.bundle_id] = classify_bundle(
            bundle, infos, base, node_count, ref_idx)

    # Capture only the calibrated sample values referenced by feasible
    # bundles, while every source is still streamed once end-to-end (which
    # also verifies its length, SHA256 and every calibrated value).
    requested = {}
    for bundle in job.bundles:
        if results[bundle.bundle_id].status == "impossible":
            continue
        for event in bundle.events:
            info = infos[event.source_id]
            channel_index = info.channel_index[event.channel]
            requested.setdefault(event.source_id, {}) \
                .setdefault(channel_index, set()).add(event.index)
    values = scan_values(infos, requested)

    report, events = build_documents(job, infos, ordered_sources, results,
                                     values)
    forbidden_roots = [info.package for info in infos.values()]
    publish(output_dir, report, events, forbidden_roots)

    status_counts = {}
    for bundle in job.bundles:
        status = results[bundle.bundle_id].status
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "schema_version": 1,
        "reference": job.reference,
        "source_count": len(infos),
        "total_raw_bytes": total_raw,
        "bundle_count": len(job.bundles),
        "status_counts": status_counts,
        "event_count": len(events),
    }


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        summary = run(args.source, args.output)
    except (ClockJoinError, OSError, ValueError, TypeError, KeyError,
            OverflowError, RecursionError) as error:
        print("signal-clock-join failed: %s" % error, file=sys.stderr)
        return EXIT_FAILURE
    print(json.dumps(summary, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return EXIT_SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
