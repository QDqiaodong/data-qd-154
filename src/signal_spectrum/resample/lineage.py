"""Deterministic ``lineage.json`` construction and atomic publication.

The derived dataset directory contains exactly ``samples.f64le`` and
``lineage.json``.  Both are written into ``DIR.staging`` next to ``DIR`` (same
parent, so the final rename cannot cross filesystems), fsynced, their digests
re-checked, and only then is the staging directory renamed to ``DIR`` in one
atomic ``os.replace``.  A failure removes the staging directory, so a
half-written conversion can never be mistaken for a complete derived dataset.

``lineage.json`` is UTF-8, key-sorted, indented by two spaces and ends with a
newline; it contains no temporary paths and no wall-clock time, so identical
input and arguments always yield identical bytes.
"""
import hashlib
import json
import os
import shutil
from fractions import Fraction
from pathlib import Path

from .engine import LINEAGE_FILE, SAMPLE_FILE, STAGING_SUFFIX
from .errors import ResampleError

SCHEMA_VERSION = 1
KIND = "derived-waveform"


def _fail(message):
    raise ResampleError(message)


def _injected_failure(stage):
    """Finite fault-injection hook used only by tests and the demo."""
    if os.environ.get("SIGSPECTRUM_RS_FAIL") == stage:
        raise ResampleError("injected failure at stage %s" % stage)


def _rational(value):
    value = Fraction(value)
    return {"num": value.numerator, "den": value.denominator}


def build_lineage(plan, output_points, output_bytes, output_sha256):
    """Assemble the lineage object from the verified plan and output digests."""
    grid = plan.grid
    manifest = plan.manifest
    first = grid.first_index
    last = grid.last_index
    kernel = plan.kernel.describe()
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "source_capture_id": manifest["capture_id"],
        "source_manifest_sha256": plan.manifest_sha256,
        "source_raw_sha256": manifest["raw_sha256"],
        "source_points": manifest["points"],
        "source_sample_rate": manifest["sample_rate"],
        "target_sample_rate": grid.target_rate,
        "ratio_p": grid.p,
        "ratio_q": grid.q,
        "ratio_gcd": grid.gcd,
        "kernel": kernel,
        "first_output_index": first,
        "last_output_index": last,
        "output_points": output_points,
        "output_channels": [channel["name"]
                            for channel in manifest["channels"]],
        "output_bytes": output_bytes,
        "output_sha256": output_sha256,
        "sample_layout": {
            "encoding": "little-endian IEEE-754 binary64",
            "order": "point-major",
            "channels": "source order, unchanged",
            "file": SAMPLE_FILE,
        },
        "calibration": {
            "state": "applied",
            "note": ("output values are source raw values already calibrated "
                     "with each channel's scale/offset; consumers must not "
                     "apply that transform again"),
        },
        "source_coordinate_range": {
            "first": _rational(Fraction(first * grid.q, grid.p)),
            "last": _rational(Fraction(last * grid.q, grid.p)),
            "step": {"num": grid.q, "den": grid.p},
            "definition": "source index coordinate x_m = m*ratio_q/ratio_p",
        },
        "time_mapping": {
            "definition": "t_m = m / target_sample_rate seconds (global m)",
            "first_m": first,
            "step": {"num": 1, "den": grid.target_rate},
        },
        "support": {
            "radius": kernel["radius"],
            "rule": ("output point m is kept only when the complete kernel "
                     "window [x_m-R, x_m+R] lies inside source indices "
                     "[0, source_points-1]"),
        },
    }


def serialize_lineage(lineage):
    text = json.dumps(lineage, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, indent=2)
    return (text + "\n").encode("utf-8")


def _fsync_dir(path):
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _write_synced(path, payload):
    try:
        with open(path, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise ResampleError("cannot write %s: %s"
                            % (path.name, error)) from None


def prepare_staging(destination, input_dir):
    """Check the destination and create an empty staging directory.

    Returns the staging path.  Rejects existing/symlinked destinations,
    destinations overlapping the input directory, and a leftover staging
    directory from an earlier failed run.
    """
    output_dir = Path(destination)
    if output_dir.is_symlink():
        _fail("destination must not be a symbolic link: %s" % output_dir)
    if output_dir.exists():
        _fail("destination already exists; refusing to overwrite: %s"
              % output_dir)
    try:
        target = output_dir.resolve()
        source = Path(input_dir).resolve()
    except OSError as error:
        raise ResampleError("cannot resolve paths: %s" % error) from None
    if (target == source or source in target.parents
            or target in source.parents):
        _fail("destination must not be the input dataset or one of its "
              "ancestors/descendants: %s" % output_dir)
    staging = output_dir.with_name(output_dir.name + STAGING_SUFFIX)
    if staging.exists() or staging.is_symlink():
        _fail("staging directory already exists from a previous failed run; "
              "remove it first: %s" % staging)
    try:
        staging.mkdir(parents=True)
        output_dir.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        shutil.rmtree(staging, ignore_errors=True)
        raise ResampleError("cannot create staging directory: %s"
                            % error) from None
    return staging


def finish(plan, staging, destination, output_points, output_bytes,
           output_sha256):
    """Write lineage, fsync staging and atomically publish ``destination``."""
    output_dir = Path(destination)
    staging = Path(staging)
    lineage = build_lineage(plan, output_points, output_bytes, output_sha256)
    published = False
    try:
        _write_synced(staging / LINEAGE_FILE, serialize_lineage(lineage))
        _injected_failure("after_lineage")
        # Re-read the finished payload and confirm the digest recorded in the
        # lineage; only a fully verified pair may become the published target.
        sample_path = staging / SAMPLE_FILE
        try:
            with open(sample_path, "rb") as stream:
                actual = hashlib.sha256()
                while True:
                    piece = stream.read(1 << 20)
                    if not piece:
                        break
                    actual.update(piece)
        except OSError as error:
            raise ResampleError("cannot re-read %s: %s"
                                % (SAMPLE_FILE, error)) from None
        if actual.hexdigest() != output_sha256:
            _fail("%s digest changed after it was written" % SAMPLE_FILE)
        names = sorted(entry.name for entry in staging.iterdir())
        if names != sorted((SAMPLE_FILE, LINEAGE_FILE)):
            _fail("staging holds unexpected files: %s" % ",".join(names))
        _fsync_dir(staging)
        os.replace(staging, output_dir)
        _fsync_dir(output_dir.parent)
        published = True
    finally:
        if not published:
            shutil.rmtree(staging, ignore_errors=True)
    return lineage
