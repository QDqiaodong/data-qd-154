"""signal-waveform-fetch command line.

One complete raw capture from an HTTP measurement device becomes one
traceable, content-addressed package directory.  The command never touches the
Welch workbench (no SQLite, no requests, no seal, no PSD) and never produces
partial success: exit 0 means a fully verified directory was published,
exit 2 means a bounded failure with the staging directory left identifiable.
"""
import argparse
import json
import sys
from pathlib import Path

from .collector import build_config, run_acquisition
from .protocol import ANALYSIS_MAX, ANALYSIS_MIN, ProtocolError
from .store import Staging, is_published

EXIT_FAILURE = 2


def build_parser():
    parser = argparse.ArgumentParser(
        prog="signal-waveform-fetch",
        description="Fetch one complete raw waveform capture over HTTP into "
                    "a traceable, fully verified package directory.")
    parser.add_argument("--url", required=True,
                        help="base URL of the HTTP measurement device")
    parser.add_argument("--output", required=True, type=Path,
                        help="directory that will hold the published package")
    parser.add_argument("--resume", action="store_true",
                        help="resume the staging directory derived from --output")
    parser.add_argument("--channel", type=int, default=None,
                        help="channel index (0-based) for optional analysis")
    parser.add_argument("--analysis-count", type=int, default=None,
                        help="number of leading values exported for the legacy "
                             "single-segment DFT (%d..%d)"
                             % (ANALYSIS_MIN, ANALYSIS_MAX))
    parser.add_argument("--channel-mode", choices=("lease", "etag"),
                        default=None,
                        help="force a consistency channel instead of negotiating")
    return parser


def _validate_analysis_args(args):
    if (args.channel is None) != (args.analysis_count is None):
        raise ProtocolError(
            "--channel and --analysis-count must be given together")
    if args.channel is None:
        return None
    if args.channel < 0:
        raise ProtocolError("--channel must be a non-negative 0-based index")
    count = args.analysis_count
    if not (ANALYSIS_MIN <= count <= ANALYSIS_MAX):
        raise ProtocolError(
            "--analysis-count must be %d..%d" % (ANALYSIS_MIN, ANALYSIS_MAX))
    return args.channel, count


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        analysis = _validate_analysis_args(args)
        cfg = build_config(args.url, force_channel=args.channel_mode,
                           analysis=analysis)
        staging = Staging(args.output)
        if is_published(staging.output_dir):
            raise ProtocolError(
                "output directory already published; refusing to overwrite: %s"
                % staging.output_dir)
        manifest = run_acquisition(cfg, staging, resume=args.resume)
    except (ProtocolError, OSError, ValueError, KeyError, OverflowError) as error:
        print("waveform fetch failed: " + str(error), file=sys.stderr)
        return EXIT_FAILURE
    print(json.dumps({"published": str(staging.output_dir),
                      "capture_id": manifest["capture_id"],
                      "etag": manifest["etag"],
                      "consistency_channel": manifest["consistency_channel"]},
                     sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
