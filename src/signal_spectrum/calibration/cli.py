"""``signal-calibration``: derive up/down calibration curves from one
reference-step trial and report which reference values a future reading may
correspond to.

Exit codes: 0 on a valid trial (even when a channel is not qualified), 2 on
any invalid input, package-verification or I/O failure.  The certificate path
may not point at any input, and a failure never touches an existing
certificate.
"""
import argparse
import json
import sys
from pathlib import Path

from .errors import CalibrationError
from .evaluate import (
    accumulate_means,
    build_certificate,
    load_package,
    validate_readings,
)
from .publish import check_destination, publish, serialize
from .schema import load_trial

EXIT_FAILURE = 2
EXIT_SUCCESS = 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="signal-calibration",
        description="Fit increasing/decreasing calibration curves from a "
                    "reference-step trial over one published waveform "
                    "package and invert future readings exactly.")
    parser.add_argument("--package", required=True, type=Path,
                        help="published waveform package directory")
    parser.add_argument("--steps", required=True, type=Path,
                        help="reference-step trial JSON")
    parser.add_argument("--certificate", required=True, type=Path,
                        help="certificate JSON path to write")
    return parser


def run(package_path, steps_path, cert_path):
    package = load_package(package_path)
    trial = load_trial(steps_path, package.points)
    validate_readings(trial, package)
    check_destination(cert_path, [steps_path], package_path)

    references = sorted({step.reference for step in trial.steps})
    step_means = accumulate_means(package, trial.steps)
    certificate = build_certificate(package, trial, step_means, references)
    publish(cert_path, serialize(certificate))
    return certificate


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        certificate = run(args.package, args.steps, args.certificate)
    except (CalibrationError, OSError, ValueError, TypeError, KeyError,
            OverflowError, RecursionError) as error:
        print("signal-calibration failed: %s" % error, file=sys.stderr)
        return EXIT_FAILURE
    summary = {
        "schema_version": 1,
        "channels": len(certificate["channels"]),
        "qualified": sum(1 for entry in certificate["channels"]
                         if entry["qualified"]),
        "readings": len(certificate["readings"]),
    }
    print(json.dumps(summary, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return EXIT_SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
