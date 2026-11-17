"""signal-excursions: build / query command line.

``build`` turns a SOURCES.json list of published waveform packages into one
immutable, self-contained excursion archive; ``query`` locates maximal
contiguous out-of-limit sample runs inside a half-open absolute range using
only the archive (never the source packages, never the network).

Exit codes: 0 on success, 2 on any invalid input, source-verification
failure, corrupt/unsupported archive or I/O failure.  argparse usage errors
also exit 2, matching the contract.
"""
import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

from .archive import ArchiveReader
from .builder import build_archive
from .errors import ExcursionError
from .query import run_query

EXIT_FAILURE = 2
MAX_LIMIT = 10000


def _finite_float(text):
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "invalid finite number: %r" % text) from None
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(
            "threshold must be finite, got %r" % text)
    return value


def _strict_int(text):
    try:
        return int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "invalid integer: %r" % text) from None


def build_parser():
    parser = argparse.ArgumentParser(
        prog="signal-excursions",
        description="Offline threshold-excursion archive over published "
                    "waveform packages.")
    sub = parser.add_subparsers(dest="command", required=True)

    build_cmd = sub.add_parser(
        "build", help="build an immutable excursion archive from packages")
    build_cmd.add_argument("--sources", required=True, type=Path,
                           help="SOURCES.json listing source_id/package pairs")
    build_cmd.add_argument("--archive", required=True, type=Path,
                           help="new archive directory (must not exist)")

    query_cmd = sub.add_parser(
        "query", help="locate maximal out-of-limit sample runs")
    query_cmd.add_argument("--archive", required=True, type=Path)
    query_cmd.add_argument("--source", required=True,
                           help="source_id from the SOURCES file")
    query_cmd.add_argument("--channel", required=True,
                           help="channel name inside that source")
    query_cmd.add_argument("--start", required=True, type=_strict_int,
                           help="absolute start sample index (inclusive)")
    query_cmd.add_argument("--stop", required=True, type=_strict_int,
                           help="absolute stop sample index (exclusive)")
    query_cmd.add_argument("--op", required=True, choices=("ge", "lt"),
                           help="hit predicate: value >= t (ge) or value < t (lt)")
    query_cmd.add_argument("--threshold", required=True, type=_finite_float,
                           help="finite threshold t")
    query_cmd.add_argument("--limit", required=True, type=_strict_int,
                           help="maximal runs to return, 1..%d" % MAX_LIMIT)
    return parser


def _run_build(args):
    summary = build_archive(args.sources, args.archive)
    print(json.dumps(summary, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return 0


def _run_query(args):
    if not (1 <= args.limit <= MAX_LIMIT):
        raise ExcursionError(
            "limit must be an integer 1..%d" % MAX_LIMIT)
    if args.start < 0:
        raise ExcursionError("start must be >= 0")
    if args.start >= args.stop:
        raise ExcursionError("start must be < stop")
    reader = ArchiveReader(args.archive)
    try:
        source = reader.find_source(args.source)
        if source is None:
            raise ExcursionError("unknown source_id: %s" % args.source)
        channel = reader.find_channel(source["source_seq"], args.channel)
        if channel is None:
            raise ExcursionError(
                "unknown channel %r for source %s"
                % (args.channel, args.source))
        result = run_query(
            reader, source, channel, args.start, args.stop, args.op,
            args.threshold, args.limit)
    finally:
        reader.close()
    result["source_id"] = args.source
    result["channel"] = args.channel
    print(json.dumps(result, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            return _run_build(args)
        return _run_query(args)
    except (ExcursionError, OSError, ValueError, TypeError, KeyError,
            OverflowError, sqlite3.Error) as error:
        print("signal-excursions %s failed: %s" % (args.command, error),
              file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
