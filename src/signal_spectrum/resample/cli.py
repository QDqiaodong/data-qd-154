"""``signal-resample``: exact-grid multi-channel waveform resampling.

One published, fully verified waveform package is resampled onto the global
grid ``t = m / target_rate`` with a fixed radius-16 Blackman-windowed sinc
kernel and published as an immutable derived dataset (``samples.f64le`` plus
``lineage.json``).  Exit codes: 0 on success, 2 on any invalid input, source
verification failure, limit violation or I/O failure.

The destination is only created when every output point has been written and
its digest re-checked; an interrupted run removes its staging directory and
leaves the input bytes untouched.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

from .engine import (
    SAMPLE_FILE,
    prepare_source,
    stream_output,
    validate_target_rate,
)
from .errors import ResampleError
from .lineage import finish, prepare_staging

EXIT_FAILURE = 2
EXIT_SUCCESS = 0


def _strict_int(text):
    try:
        return int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "invalid integer: %r" % text) from None


def build_parser():
    parser = argparse.ArgumentParser(
        prog="signal-resample",
        description="Resample a published multi-channel waveform dataset "
                    "onto an exact rational target time grid.")
    parser.add_argument("--input", required=True, type=Path,
                        help="published waveform package (manifest.json + "
                             "raw.bin)")
    parser.add_argument("--target-rate", required=True, type=_strict_int,
                        help="target sample rate in whole Hz (1..10^9)")
    parser.add_argument("--dest", required=True, type=Path,
                        help="new derived dataset directory (must not exist)")
    return parser


def run(input_dir, target_rate, destination):
    validate_target_rate(target_rate)
    plan = prepare_source(input_dir, target_rate)
    staging = prepare_staging(destination, input_dir)
    try:
        output_points, output_bytes, output_sha = stream_output(
            plan, staging / SAMPLE_FILE)
        lineage = finish(plan, staging, destination, output_points,
                         output_bytes, output_sha)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "schema_version": lineage["schema_version"],
        "kind": lineage["kind"],
        "source_capture_id": lineage["source_capture_id"],
        "source_sample_rate": lineage["source_sample_rate"],
        "target_sample_rate": lineage["target_sample_rate"],
        "ratio_p": lineage["ratio_p"],
        "ratio_q": lineage["ratio_q"],
        "first_output_index": lineage["first_output_index"],
        "output_points": lineage["output_points"],
        "output_channels": lineage["output_channels"],
        "output_bytes": lineage["output_bytes"],
        "output_sha256": lineage["output_sha256"],
    }


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        summary = run(args.input, args.target_rate, args.dest)
    except (ResampleError, OSError, ValueError, TypeError, KeyError,
            OverflowError, MemoryError) as error:
        print("signal-resample failed: %s" % error, file=sys.stderr)
        return EXIT_FAILURE
    print(json.dumps(summary, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return EXIT_SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
