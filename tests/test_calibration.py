"""Tests for ``signal-calibration``.

The suite covers, from the contract:

* the acceptance scenario: reference nodes 0/1/2/3 with up means 0,4,2,6 and
  down means 3,4,6,8 on unequal-length steps.  The up fit must be 0,3,3,6, an
  unknown-direction reading of [3,3] inverts to [0,0] and [1,2], and a reading
  of [3,4] inverts to the single merged [0,7/3];
* a tolerance that is exactly met (qualified) and each of the two maxima
  slightly exceeded (not qualified), independently;
* curves outside the domain are not extrapolated, and a real gap between two
  inverses is preserved;
* a step spanning a decoding-block boundary is split exactly by absolute
  sample index;
* explicitly directed single-point inversions;
* a bad raw SHA and a bad manifest fingerprint are rejected, and an existing
  certificate keeps its exact bytes on every failure;
* a real-format package with non-unit per-channel scale/offset is verified
  against an independent naive decoder;
* reordering the STEPS arrays does not change the certificate bytes;
* output-path protection (certificate may not be an input).
"""
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from signal_spectrum.calibration.curves import (
    classify,
    inverse_intervals,
    isotonic,
    merge_intervals,
)
from signal_spectrum.calibration.exact import mean_from_scaled, scaled_int

MODULE = ["-m", "signal_spectrum.calibration.cli"]
WIDTH = {"int16": 2, "uint16": 2, "float32": 4}
CODE = {"int16": "h", "uint16": "H", "float32": "f"}


def canonical_meta_sha(meta):
    return hashlib.sha256(json.dumps(
        meta, sort_keys=True, ensure_ascii=False, allow_nan=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def write_package(pkg_dir, capture_id, sample_rate, points, dtype,
                  byte_order, channels, raw_fn):
    """Write a published-style package (manifest.json + raw.bin)."""
    pkg_dir = Path(pkg_dir)
    pkg_dir.mkdir(parents=True, exist_ok=True)
    prefix = "<" if byte_order == "little" else ">"
    packer = struct.Struct(prefix + CODE[dtype])
    frame = bytearray()
    for point in range(points):
        for channel in range(len(channels)):
            frame += packer.pack(raw_fn(point, channel))
    raw = bytes(frame)
    meta = {
        "capture_id": capture_id,
        "sample_rate": sample_rate,
        "points": points,
        "dtype": dtype,
        "byte_order": byte_order,
        "channels": [{"name": name, "scale": scale, "offset": offset}
                     for name, scale, offset in channels],
        "raw_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    manifest = {
        "schema_version": 1,
        "source_url": "http://127.0.0.1:9/device",
        "capabilities": {"max_range_bytes": 4096, "lease": False},
        "consistency_channel": "etag",
        "etag": '"' + meta["sha256"][:32] + '"',
        "capture_id": capture_id,
        "config": {"sample_rate": sample_rate, "points": points,
                   "dtype": dtype, "byte_order": byte_order},
        "channels": meta["channels"],
        "raw_bytes": meta["raw_bytes"],
        "raw_sha256": meta["sha256"],
        "meta_sha256": canonical_meta_sha(meta),
        "analysis": None,
    }
    (pkg_dir / "raw.bin").write_bytes(raw)
    (pkg_dir / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2))
    return meta, manifest


def naive_decode_channel(pkg_dir, channel_name):
    """Independently decode one channel with plain struct unpacking."""
    pkg_dir = Path(pkg_dir)
    manifest = json.loads((pkg_dir / "manifest.json").read_text())
    names = [spec["name"] for spec in manifest["channels"]]
    index = names.index(channel_name)
    dtype = manifest["config"]["dtype"]
    width = WIDTH[dtype]
    prefix = "<" if manifest["config"]["byte_order"] == "little" else ">"
    unpack = struct.Struct(prefix + CODE[dtype]).unpack
    raw = (pkg_dir / "raw.bin").read_bytes()
    frame = width * len(manifest["channels"])
    spec = manifest["channels"][index]
    values = []
    for offset in range(0, len(raw), frame):
        (raw_value,) = unpack(raw[offset + index * width:
                                  offset + (index + 1) * width])
        values.append(raw_value * spec["scale"] + spec["offset"])
    return values


def step(start, stop, reference, direction):
    return {"start": start, "stop": stop, "reference": reference,
            "direction": direction}


def reading(reading_id, channel, direction, low, high):
    return {"id": reading_id, "channel": channel, "direction": direction,
            "low": low, "high": high}


def acceptance_steps():
    """Nodes 0..3 on unequal-length up/down segments."""
    up_lengths = [1, 2, 1, 2]
    down_lengths = [3, 1, 2, 2]
    segments = ([("up", ref, length) for ref, length in enumerate(up_lengths)]
                + [("down", ref, length)
                   for ref, length in enumerate(down_lengths)])
    means = {"up": [0, 4, 2, 6], "down": [3, 4, 6, 8]}
    raw_values = []
    steps = []
    start = 0
    for direction, reference, length in segments:
        steps.append(step(start, start + length, reference, direction))
        raw_values.extend([means[direction][reference]] * length)
        start += length
    return steps, raw_values


def q(value):
    """Parse a ``{num, den}`` object into a Fraction."""
    return Fraction(value["num"], value["den"])


def fraction_token(value):
    """Render an exactly-terminating Fraction as a JSON numeric literal."""
    num, den = value.numerator, value.denominator
    twos = fives = 0
    probe = den
    while probe % 2 == 0:
        probe //= 2
        twos += 1
    while probe % 5 == 0:
        probe //= 5
        fives += 1
    assert probe == 1, "only terminating tolerances are used in these tests"
    places = max(twos, fives)
    scaled = num * 2 ** (places - twos) * 5 ** (places - fives)
    sign = "-" if scaled < 0 else ""
    digits = str(abs(scaled)).rjust(places + 1, "0")
    if places == 0:
        return sign + digits
    return sign + digits[:-places] + "." + digits[-places:]


def exact_json(value):
    """Serialize a structure, keeping Fraction values as exact JSON numbers."""
    if isinstance(value, Fraction):
        return fraction_token(value)
    if isinstance(value, bool) or value is None:
        return json.dumps(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ",".join(exact_json(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(key) + ":" + exact_json(item)
                              for key, item in value.items()) + "}"
    raise TypeError("unsupported trial value: %r" % (value,))


class CalibrationCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        self.env = env

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args, expect=0):
        proc = subprocess.run(
            [sys.executable, *MODULE, *[str(a) for a in args]],
            capture_output=True, text=True, env=self.env)
        self.assertEqual(proc.returncode, expect,
                         "args=%r stdout=%r stderr=%r"
                         % (args, proc.stdout, proc.stderr))
        return proc

    def write_package(self, name, capture_id, sample_rate, points, dtype,
                      byte_order, channels, raw_fn):
        pkg = self.root / name
        write_package(pkg, capture_id, sample_rate, points, dtype,
                      byte_order, channels, raw_fn)
        return pkg

    def write_trial(self, trial, name="STEPS.json"):
        path = self.root / name
        path.write_text(exact_json(trial))
        return path

    def calibrate(self, package, trial_path, cert_name="cert.json",
                  expect=0):
        cert = self.root / cert_name
        proc = self.run_cli("--package", package, "--steps", trial_path,
                            "--certificate", cert, expect=expect)
        if expect != 0:
            return proc, cert
        return json.loads(cert.read_text()), cert

    def channel(self, certificate, name):
        return next(entry for entry in certificate["channels"]
                    if entry["channel"] == name)

    def reading_map(self, certificate):
        return {entry["id"]: entry for entry in certificate["readings"]}


class AcceptanceScenario(CalibrationCase):
    def _run(self):
        steps, raw_values = acceptance_steps()
        pkg = self.write_package(
            "pkg", "cap-accept", 100, len(raw_values), "float32", "little",
            [("ch0", 1.0, 0.0)], lambda p, c: float(raw_values[p]))
        trial = {
            "steps": steps,
            "readings": [
                reading("at3", "ch0", "unknown", 3, 3),
                reading("band34", "ch0", "unknown", 3, 4),
            ],
            "residual_tolerance": 1,
            "hysteresis_tolerance": 3,
        }
        certificate, _ = self.calibrate(pkg, self.write_trial(trial))
        return certificate

    def test_up_fit_and_direction_curves(self):
        certificate = self._run()
        channel = self.channel(certificate, "ch0")
        up = channel["up"]
        self.assertEqual([q(node["reference"]) for node in up],
                         [Fraction(0), Fraction(1), Fraction(2),
                          Fraction(3)])
        self.assertEqual([q(node["measured_mean"]) for node in up],
                         [Fraction(0), Fraction(4), Fraction(2),
                          Fraction(6)])
        # Pooling 4 and 2 -> 3,3 is the exact isotonic fit.
        self.assertEqual([q(node["fitted"]) for node in up],
                         [Fraction(0), Fraction(3), Fraction(3),
                          Fraction(6)])
        down = channel["down"]
        self.assertEqual([q(node["fitted"]) for node in down],
                         [Fraction(3), Fraction(4), Fraction(6),
                          Fraction(8)])
        # Largest absolute up/down node gap: |3-3|,|3-4|,|3-6|,|6-8| -> 3.
        self.assertEqual(q(channel["hysteresis_max"]), Fraction(3))
        # Largest absolute residual: |4-3| = |2-3| = 1.
        self.assertEqual(q(channel["residual_max"]), Fraction(1))
        self.assertTrue(channel["qualified"])

    def test_unknown_direction_point_reading(self):
        certificate = self._run()
        result = self.reading_map(certificate)["at3"]
        intervals = [(q(span["low"]), q(span["high"]))
                     for span in result["intervals"]]
        self.assertEqual(intervals, [(Fraction(0), Fraction(0)),
                                     (Fraction(1), Fraction(2))])
        self.assertEqual(result["classification"], "ambiguous")

    def test_unknown_direction_band_merges_adjacent(self):
        certificate = self._run()
        result = self.reading_map(certificate)["band34"]
        intervals = [(q(span["low"]), q(span["high"]))
                     for span in result["intervals"]]
        # Up curve gives [0,1]; down curve gives [1,7/3]; they touch at 1.
        self.assertEqual(intervals, [(Fraction(0), Fraction(7, 3))])
        self.assertEqual(result["classification"], "ambiguous")


class ToleranceBoundary(CalibrationCase):
    """Exactly-at-tolerance qualifies; each maximum just over fails."""

    def _certificate(self, residual_tolerance, hysteresis_tolerance):
        steps = [step(0, 2, 0, "up"), step(2, 4, 1, "up"),
                 step(4, 6, 2, "up"),
                 step(6, 8, 0, "down"), step(8, 10, 1, "down"),
                 step(10, 12, 2, "down")]
        # up means 0,2,3 (non-decreasing); down means 1,4,4 (non-decreasing);
        # the largest node gap is |2-4| = 2.
        pkg = self.write_package(
            "pkg", "cap-tol", 10, 12, "float32", "little",
            [("main", 1.0, 0.0)],
            lambda p, c: [0.0, 0.0, 2.0, 2.0, 3.0, 3.0,
                          1.0, 1.0, 4.0, 4.0, 4.0, 4.0][p])
        trial = {"steps": steps, "readings": [],
                 "residual_tolerance": residual_tolerance,
                 "hysteresis_tolerance": hysteresis_tolerance}
        certificate, _ = self.calibrate(pkg, self.write_trial(trial))
        return self.channel(certificate, "main")

    def test_exactly_at_tolerance_qualifies(self):
        channel = self._certificate(0, 2)
        self.assertEqual(q(channel["hysteresis_max"]), Fraction(2))
        self.assertEqual(q(channel["residual_max"]), Fraction(0))
        self.assertTrue(channel["qualified"])

    def test_hysteresis_just_over_tolerance_fails(self):
        channel = self._certificate(0, Fraction(199999, 100000))
        self.assertEqual(q(channel["hysteresis_max"]), Fraction(2))
        self.assertFalse(channel["qualified"])

    def test_residual_exceeded_independently(self):
        # Up nodes 0,1,2 with means 0,4,1 -> isotonic fit 0,5/2,5/2, so the
        # residual is 3/2 at both pooled nodes while the down curve is flat.
        steps = [step(0, 1, 0, "up"), step(1, 2, 1, "up"),
                 step(2, 3, 2, "up"), step(3, 4, 0, "down"),
                 step(4, 5, 1, "down"), step(5, 6, 2, "down")]
        pkg = self.write_package(
            "pkg", "cap-res3", 10, 6, "float32", "little",
            [("main", 1.0, 0.0)],
            lambda p, c: [0.0, 4.0, 1.0, 0.0, 0.0, 0.0][p])
        trial = {"steps": steps, "readings": [],
                 "residual_tolerance": Fraction(1, 1),
                 "hysteresis_tolerance": 2}
        certificate, _ = self.calibrate(pkg, self.write_trial(trial))
        channel = self.channel(certificate, "main")
        self.assertEqual(q(channel["residual_max"]), Fraction(3, 2))
        self.assertEqual(q(channel["hysteresis_max"]), Fraction(5, 2))
        # Residual fails, hysteresis also fails here (5/2 > 2).
        self.assertFalse(channel["qualified"])

        trial["hysteresis_tolerance"] = Fraction(5, 2)
        certificate, _ = self.calibrate(pkg, self.write_trial(trial, "b.json"),
                                        "cert-b.json")
        channel = self.channel(certificate, "main")
        self.assertEqual(q(channel["hysteresis_max"]), Fraction(5, 2))
        self.assertFalse(channel["qualified"])  # residual still 3/2 > 1

        trial["residual_tolerance"] = Fraction(3, 2)
        certificate, _ = self.calibrate(pkg, self.write_trial(trial, "c.json"),
                                        "cert-c.json")
        channel = self.channel(certificate, "main")
        self.assertTrue(channel["qualified"])  # both exactly at tolerance


class DomainAndGaps(CalibrationCase):
    """Both direction curves are non-decreasing; a gap only comes from the
    unknown-direction union of two curves whose inverses are separated."""

    def _certificate(self, readings):
        steps = [step(0, 2, 0, "up"), step(2, 3, 1, "up"),
                 step(3, 5, 2, "up"),
                 step(5, 6, 0, "down"), step(6, 8, 1, "down"),
                 step(8, 10, 2, "down")]
        # up means 0,5,10 on nodes 0,1,2 (already non-decreasing);
        # down means 0,1,2 on the same nodes.
        raw = [0.0, 0.0, 5.0, 10.0, 10.0,
               0.0, 1.0, 1.0, 2.0, 2.0]
        pkg = self.write_package(
            "pkg", "cap-gap", 10, 10, "float32", "little",
            [("main", 1.0, 0.0)], lambda p, c: raw[p])
        trial = {"steps": steps, "readings": readings,
                 "residual_tolerance": 10, "hysteresis_tolerance": 10}
        certificate, _ = self.calibrate(pkg, self.write_trial(trial))
        return certificate

    def test_outside_domain_not_extrapolated(self):
        certificate = self._certificate(
            [reading("low", "main", "up", -5, -1),
             reading("high", "main", "up", 11, 12),
             reading("inside", "main", "up", 0, 0)])
        results = self.reading_map(certificate)
        self.assertEqual(results["low"]["intervals"], [])
        self.assertEqual(results["low"]["classification"], "outside")
        self.assertEqual(results["high"]["intervals"], [])
        self.assertEqual(results["high"]["classification"], "outside")
        self.assertEqual(results["inside"]["classification"], "unique")

    def test_gap_preserved_between_curves(self):
        # Up curve 0,5,10 gives x in [1/5,2/5] for y in [1,2]; down curve
        # 0,1,2 gives x in [1,2].  The union must keep the (2/5,1) gap.
        certificate = self._certificate(
            [reading("band", "main", "unknown", 1, 2)])
        result = self.reading_map(certificate)["band"]
        intervals = [(q(span["low"]), q(span["high"]))
                     for span in result["intervals"]]
        self.assertEqual(intervals,
                         [(Fraction(1, 5), Fraction(2, 5)),
                          (Fraction(1), Fraction(2))])
        self.assertEqual(result["classification"], "ambiguous")

    def test_plateau_preserves_both_ends(self):
        steps = [step(0, 1, 0, "up"), step(1, 2, 1, "up"),
                 step(2, 3, 2, "up"),
                 step(3, 4, 0, "down"), step(4, 5, 1, "down"),
                 step(5, 6, 2, "down")]
        raw = [0.0, 2.0, 2.0, 0.0, 2.0, 2.0]
        pkg = self.write_package(
            "pkg", "cap-plateau", 10, 6, "float32", "little",
            [("main", 1.0, 0.0)], lambda p, c: raw[p])
        trial = {"steps": steps,
                 "readings": [reading("top", "main", "up", 2, 2)],
                 "residual_tolerance": 0, "hysteresis_tolerance": 0}
        certificate, _ = self.calibrate(pkg, self.write_trial(trial))
        intervals = [(q(span["low"]), q(span["high"])) for span in
                     self.reading_map(certificate)["top"]["intervals"]]
        self.assertEqual(intervals, [(Fraction(1), Fraction(2))])
        self.assertEqual(self.reading_map(certificate)["top"]
                         ["classification"], "ambiguous")

    def test_zero_length_point_is_unique(self):
        certificate = self._certificate(
            [reading("one", "main", "up", 2, 2)])
        result = self.reading_map(certificate)["one"]
        self.assertEqual([(q(span["low"]), q(span["high"]))
                          for span in result["intervals"]],
                         [(Fraction(2, 5), Fraction(2, 5))])
        self.assertEqual(result["classification"], "unique")

    def test_directed_single_point_inversions(self):
        certificate = self._certificate(
            [reading("up5", "main", "up", 5, 5),
             reading("down1", "main", "down", 1, 1)])
        results = self.reading_map(certificate)
        self.assertEqual([(q(s["low"]), q(s["high"]))
                          for s in results["up5"]["intervals"]],
                         [(Fraction(1), Fraction(1))])
        self.assertEqual([(q(s["low"]), q(s["high"]))
                          for s in results["down1"]["intervals"]],
                         [(Fraction(1), Fraction(1))])


class BlockBoundary(CalibrationCase):
    def test_step_spanning_decode_block_is_exact(self):
        # One 1 MiB read block holds 262144 float32 frames.  A single up
        # segment straddles that boundary with different values on either
        # side, so a per-block mean would corrupt its exact segment mean.
        boundary = 262144
        total = 264444
        raw = [1.0] * total
        for index in range(boundary, total):
            raw[index] = 3.0
        pkg = self.write_package(
            "pkg", "cap-block", 10, total, "float32", "little",
            [("main", 1.0, 0.0)], lambda p, c: raw[p])
        steps = [step(0, 100000, 0, "up"),
                 step(100000, 200000, 0, "down"),
                 step(200000, 264144, 1, "up"),
                 step(264144, 264244, 1, "down"),
                 step(264244, 264344, 2, "up"),
                 step(264344, 264444, 2, "down")]
        trial = {"steps": steps, "readings": [],
                 "residual_tolerance": 10, "hysteresis_tolerance": 10}
        certificate, _ = self.calibrate(pkg, self.write_trial(trial))
        channel = self.channel(certificate, "main")
        # Segment [200000, 264144) has 62144 ones up to the block boundary
        # and 2000 threes after it; its exact mean straddles the block.
        self.assertEqual(q(channel["up"][1]["measured_mean"]),
                         Fraction(62144 + 6000, 64144))
        self.assertEqual(q(channel["down"][1]["measured_mean"]),
                         Fraction(3))


class PackageVerification(CalibrationCase):
    def _package(self):
        steps, raw_values = acceptance_steps()
        pkg = self.write_package(
            "pkg", "cap-verify", 100, len(raw_values), "float32", "little",
            [("main", 1.0, 0.0)], lambda p, c: float(raw_values[p]))
        trial = {"steps": steps, "readings": [
            reading("r", "main", "unknown", 3, 3)],
            "residual_tolerance": 1, "hysteresis_tolerance": 3}
        return pkg, self.write_trial(trial)

    def test_bad_raw_sha_rejected_and_certificate_preserved(self):
        pkg, trial = self._package()
        cert = self.root / "cert.json"
        cert.write_bytes(b"previous certificate bytes")
        manifest = json.loads((pkg / "manifest.json").read_text())
        manifest["raw_sha256"] = "0" * 64
        (pkg / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2))
        self.run_cli("--package", pkg, "--steps", trial, "--certificate",
                     cert, expect=2)
        self.assertEqual(cert.read_bytes(), b"previous certificate bytes")

    def test_bad_manifest_fingerprint_rejected(self):
        pkg, trial = self._package()
        manifest = json.loads((pkg / "manifest.json").read_text())
        manifest["meta_sha256"] = "f" * 64
        (pkg / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2))
        self.run_cli("--package", pkg, "--steps", trial, "--certificate",
                     self.root / "cert.json", expect=2)
        self.assertFalse((self.root / "cert.json").exists())

    def test_failed_readings_keep_certificate(self):
        pkg, trial = self._package()
        cert = self.root / "cert.json"
        cert.write_bytes(b"keep me")
        payload = json.loads(trial.read_text())
        payload["readings"][0]["channel"] = "missing"
        trial.write_text(json.dumps(payload))
        self.run_cli("--package", pkg, "--steps", trial, "--certificate",
                     cert, expect=2)
        self.assertEqual(cert.read_bytes(), b"keep me")

    def test_certificate_may_not_be_an_input(self):
        pkg, trial = self._package()
        self.run_cli("--package", pkg, "--steps", trial, "--certificate",
                     trial, expect=2)
        self.assertTrue(trial.exists())

    def test_nonfinite_payload_rejected(self):
        pkg, trial = self._package()
        path = pkg / "raw.bin"
        raw = bytearray(path.read_bytes())
        raw[0:4] = struct.pack("<f", float("nan"))
        path.write_bytes(bytes(raw))
        manifest = json.loads((pkg / "manifest.json").read_text())
        manifest["raw_sha256"] = hashlib.sha256(bytes(raw)).hexdigest()
        manifest["meta_sha256"] = canonical_meta_sha({
            "capture_id": manifest["capture_id"],
            "sample_rate": manifest["config"]["sample_rate"],
            "points": manifest["config"]["points"],
            "dtype": manifest["config"]["dtype"],
            "byte_order": manifest["config"]["byte_order"],
            "channels": manifest["channels"],
            "raw_bytes": manifest["raw_bytes"],
            "sha256": manifest["raw_sha256"],
        })
        (pkg / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2))
        cert = self.root / "cert.json"
        self.run_cli("--package", pkg, "--steps", trial, "--certificate",
                     cert, expect=2)
        self.assertFalse(cert.exists())

    def test_symlink_manifest_rejected(self):
        pkg, trial = self._package()
        manifest = pkg / "manifest.json"
        moved = self.root / "manifest-real.json"
        shutil.copyfile(manifest, moved)
        manifest.unlink()
        manifest.symlink_to(moved)
        self.run_cli("--package", pkg, "--steps", trial, "--certificate",
                     self.root / "cert.json", expect=2)
        self.assertFalse((self.root / "cert.json").exists())

    def test_certificate_may_not_be_inside_package(self):
        pkg, trial = self._package()
        target = pkg / "manifest.json"
        before = target.read_bytes()
        self.run_cli("--package", pkg, "--steps", trial, "--certificate",
                     target, expect=2)
        self.assertEqual(target.read_bytes(), before)


class NonUnitCalibration(CalibrationCase):
    def test_scaled_and_offset_channels_match_naive_decode(self):
        points = 12
        specs = [("main", 0.5, -1.25), ("aux", 2.5, 0.125)]
        pkg = self.write_package(
            "pkg", "cap-scale", 10, points, "int16", "big", specs,
            lambda p, c: (p * 7 + c * 3) % 40)

        def mean(values):
            total = sum(scaled_int(v) for v in values)
            return mean_from_scaled(total, len(values))

        main_values = naive_decode_channel(pkg, "main")
        aux_values = naive_decode_channel(pkg, "aux")
        # Steps: 3 refs, one up + one down each, unequal lengths.
        layout = [("main", 0, "up", 0, 2), ("main", 0, "down", 2, 3),
                  ("main", 1, "up", 3, 5), ("main", 1, "down", 5, 7),
                  ("main", 2, "up", 7, 10), ("main", 2, "down", 10, 12)]
        steps = [step(a, b, ref, direction)
                 for _, ref, direction, a, b in layout]
        trial = {"steps": steps, "readings": [],
                 "residual_tolerance": 100, "hysteresis_tolerance": 100}
        certificate, _ = self.calibrate(pkg, self.write_trial(trial))
        main = self.channel(certificate, "main")
        expected_up = [mean(main_values[0:2]), mean(main_values[3:5]),
                       mean(main_values[7:10])]
        expected_down = [mean(main_values[2:3]), mean(main_values[5:7]),
                         mean(main_values[10:12])]
        self.assertEqual([q(node["measured_mean"]) for node in main["up"]],
                         expected_up)
        self.assertEqual([q(node["measured_mean"])
                          for node in main["down"]], expected_down)
        aux = self.channel(certificate, "aux")
        self.assertEqual([q(node["measured_mean"]) for node in aux["up"]],
                         [mean(aux_values[0:2]), mean(aux_values[3:5]),
                          mean(aux_values[7:10])])
        # Non-unit scale/offset must produce non-integral exact means.
        self.assertIn(aux["up"][0]["measured_mean"]["den"], (2, 4, 8))

    def test_manifest_and_raw_digests_are_of_original_bytes(self):
        steps, raw_values = acceptance_steps()
        pkg = self.write_package(
            "pkg", "cap-digest", 100, len(raw_values), "float32", "little",
            [("main", 1.0, 0.0)], lambda p, c: float(raw_values[p]))
        trial = {"steps": steps, "readings": [],
                 "residual_tolerance": 1, "hysteresis_tolerance": 3}
        trial_path = self.write_trial(trial)
        certificate, _ = self.calibrate(pkg, trial_path)
        self.assertEqual(
            certificate["manifest_sha256"],
            hashlib.sha256((pkg / "manifest.json").read_bytes()).hexdigest())
        self.assertEqual(
            certificate["raw_sha256"],
            hashlib.sha256((pkg / "raw.bin").read_bytes()).hexdigest())


class CertificateShape(CalibrationCase):
    def _run(self):
        steps, raw_values = acceptance_steps()
        # Two channels whose manifest order is deliberately non-alphabetical.
        pkg = self.write_package(
            "pkg", "cap-shape", 100, len(raw_values), "int16", "little",
            [("zeta", 2, 3), ("alpha", 1, 0)],
            lambda p, c: raw_values[p] - c)
        trial = {"steps": steps, "readings": [],
                 "residual_tolerance": 10, "hysteresis_tolerance": 10}
        certificate, _ = self.calibrate(pkg, self.write_trial(trial))
        return certificate

    def test_top_level_fields_exact(self):
        certificate = self._run()
        self.assertEqual(set(certificate),
                         {"manifest_sha256", "raw_sha256", "channels",
                          "readings"})

    def test_channel_order_follows_manifest(self):
        certificate = self._run()
        self.assertEqual([entry["channel"] for entry in certificate["channels"]],
                         ["zeta", "alpha"])

    def test_channel_entry_fields_exact(self):
        certificate = self._run()
        for entry in certificate["channels"]:
            self.assertEqual(set(entry), {"channel", "qualified",
                                          "residual_max", "hysteresis_max",
                                          "up", "down"})
            for node in entry["up"] + entry["down"]:
                self.assertEqual(set(node), {"reference", "measured_mean",
                                             "fitted"})
                for value in node.values():
                    self.assertEqual(set(value), {"num", "den"})
                    self.assertGreater(value["den"], 0)

    def test_reading_entry_fields_exact(self):
        steps, raw_values = acceptance_steps()
        pkg = self.write_package(
            "pkg", "cap-shape2", 100, len(raw_values), "float32", "little",
            [("main", 1.0, 0.0)], lambda p, c: float(raw_values[p]))
        trial = {"steps": steps,
                 "readings": [reading("r", "main", "unknown", 3, 4)],
                 "residual_tolerance": 1, "hysteresis_tolerance": 3}
        certificate, _ = self.calibrate(pkg, self.write_trial(trial))
        entry = certificate["readings"][0]
        self.assertEqual(set(entry), {"id", "channel", "classification",
                                      "intervals"})
        for interval in entry["intervals"]:
            self.assertEqual(set(interval), {"low", "high"})
            self.assertEqual(set(interval["low"]), {"num", "den"})


class DescendingSegments(CalibrationCase):
    def test_down_ramp_laid_out_in_descending_order(self):
        # Physically the reference walks 3,2,1,0 on the down ramp, so the
        # descending segments visit references in descending order.  The fit
        # must still pair each mean with *its own* reference.
        steps = [step(0, 1, 0, "up"), step(1, 2, 1, "up"),
                 step(2, 3, 2, "up"), step(3, 4, 3, "up"),
                 step(4, 5, 3, "down"), step(5, 6, 2, "down"),
                 step(6, 7, 1, "down"), step(7, 8, 0, "down")]
        raw = [0.0, 1.0, 2.0, 3.0, 3.0, 2.0, 1.0, 0.0]
        pkg = self.write_package(
            "pkg", "cap-desc", 10, 8, "float32", "little",
            [("main", 1.0, 0.0)], lambda p, c: raw[p])
        trial = {"steps": steps, "readings": [],
                 "residual_tolerance": 0, "hysteresis_tolerance": 0}
        certificate, _ = self.calibrate(pkg, self.write_trial(trial))
        channel = self.channel(certificate, "main")
        self.assertEqual([q(n["reference"]) for n in channel["down"]],
                         [Fraction(0), Fraction(1), Fraction(2),
                          Fraction(3)])
        self.assertEqual([q(n["measured_mean"]) for n in channel["down"]],
                         [Fraction(0), Fraction(1), Fraction(2),
                          Fraction(3)])
        self.assertEqual(q(channel["residual_max"]), Fraction(0))
        self.assertTrue(channel["qualified"])


class UnqualifiedStillPublishes(CalibrationCase):
    def test_unqualified_channel_still_writes_certificate(self):
        steps, raw_values = acceptance_steps()
        pkg = self.write_package(
            "pkg", "cap-unq", 100, len(raw_values), "float32", "little",
            [("main", 1.0, 0.0)], lambda p, c: float(raw_values[p]))
        trial = {"steps": steps,
                 "readings": [reading("r", "main", "unknown", 3, 3)],
                 "residual_tolerance": 0, "hysteresis_tolerance": 0}
        certificate, cert = self.calibrate(pkg, self.write_trial(trial))
        self.assertTrue(cert.exists())
        channel = self.channel(certificate, "main")
        self.assertFalse(channel["qualified"])
        # Diagnostics are still present for the unqualified channel.
        self.assertEqual(self.reading_map(certificate)["r"]["classification"],
                         "ambiguous")
        self.assertEqual(len(channel["up"]), 4)


class ReorderInvariance(CalibrationCase):
    def test_steps_and_readings_reorder_keep_bytes(self):
        steps, raw_values = acceptance_steps()
        pkg = self.write_package(
            "pkg", "cap-reorder", 100, len(raw_values), "float32", "little",
            [("main", 1.0, 0.0)], lambda p, c: float(raw_values[p]))
        readings = [reading("b", "main", "unknown", 3, 4),
                    reading("a", "main", "unknown", 3, 3)]
        trial = {"steps": steps, "readings": readings,
                 "residual_tolerance": 1, "hysteresis_tolerance": 3}
        cert_a = self.root / "a.json"
        self.run_cli("--package", pkg, "--steps", self.write_trial(trial),
                     "--certificate", cert_a)
        trial_b = {"steps": list(reversed(steps)),
                   "readings": list(reversed(readings)),
                   "residual_tolerance": 1, "hysteresis_tolerance": 3}
        cert_b = self.root / "b.json"
        self.run_cli("--package", pkg,
                     "--steps", self.write_trial(trial_b, "reversed.json"),
                     "--certificate", cert_b)
        self.assertEqual(cert_a.read_bytes(), cert_b.read_bytes())


class TrialValidation(CalibrationCase):
    def _good(self):
        steps, raw_values = acceptance_steps()
        pkg = self.write_package(
            "pkg", "cap-invalid", 100, len(raw_values), "float32", "little",
            [("main", 1.0, 0.0)], lambda p, c: float(raw_values[p]))
        return pkg, {"steps": steps, "readings": [],
                     "residual_tolerance": 1, "hysteresis_tolerance": 3}

    def assert_rejected(self, trial, raw=None):
        pkg, _ = self._good()
        path = self.root / "bad.json"
        path.write_text(raw if raw is not None else exact_json(trial))
        self.run_cli("--package", pkg, "--steps", path, "--certificate",
                     self.root / "cert.json", expect=2)
        self.assertFalse((self.root / "cert.json").exists())

    def test_segment_not_partitioning(self):
        pkg, trial = self._good()
        trial["steps"][0]["start"] = 1
        self.assert_rejected(trial)

    def test_too_few_references(self):
        pkg, trial = self._good()
        trial["steps"] = [entry for entry in trial["steps"]
                          if entry["reference"] < 2]
        self.assert_rejected(trial)

    def test_missing_direction(self):
        pkg, trial = self._good()
        trial["steps"] = [entry for entry in trial["steps"]
                          if not (entry["reference"] == 0
                                  and entry["direction"] == "down")]
        self.assert_rejected(trial)

    def test_duplicate_direction(self):
        pkg, trial = self._good()
        trial["steps"].append(step(12, 13, 0, "up"))
        self.assert_rejected(trial)

    def test_negative_tolerance(self):
        pkg, trial = self._good()
        trial["residual_tolerance"] = -1
        self.assert_rejected(trial)

    def test_boolean_is_not_a_number(self):
        pkg, trial = self._good()
        trial["hysteresis_tolerance"] = True
        self.assert_rejected(trial)

    def test_duplicate_reading_id(self):
        pkg, trial = self._good()
        trial["readings"] = [reading("x", "main", "up", 0, 1),
                             reading("x", "main", "down", 0, 1)]
        self.assert_rejected(trial)

    def test_low_above_high(self):
        pkg, trial = self._good()
        trial["readings"] = [reading("x", "main", "up", 2, 1)]
        self.assert_rejected(trial)

    def test_reading_unknown_channel_rejected(self):
        pkg, trial = self._good()
        trial["readings"] = [reading("x", "main", "up", 1, 2)]
        trial["readings"][0]["channel"] = "missing"
        self.assert_rejected(trial)

    def test_mantissa_digit_limit(self):
        pkg, trial = self._good()
        raw = exact_json(trial).replace('"residual_tolerance":1',
                                        '"residual_tolerance":'
                                        + "1." + "0" * 31 + "1")
        self.assert_rejected(trial, raw=raw)

    def test_exponent_magnitude_limit(self):
        pkg, trial = self._good()
        raw = exact_json(trial).replace('"residual_tolerance":1',
                                        '"residual_tolerance":1e309')
        self.assert_rejected(trial, raw=raw)

    def test_fractional_index_rejected(self):
        pkg, trial = self._good()
        raw = exact_json(trial).replace('"start":0', '"start":0.5', 1)
        self.assert_rejected(trial, raw=raw)

    def test_empty_segment_rejected(self):
        pkg, trial = self._good()
        trial["steps"][0]["stop"] = trial["steps"][0]["start"]
        self.assert_rejected(trial)

    def test_exponent_notation_reference_accepted(self):
        pkg, trial = self._good()
        raw = exact_json(trial).replace('"reference":3', '"reference":3.0e0',
                                       1)
        pkg = self._good()[0]
        path = self.root / "exp.json"
        path.write_text(raw)
        certificate, _ = self.calibrate(pkg, path)
        self.assertEqual(q(certificate["channels"][0]["up"][-1]["reference"]),
                         Fraction(3))

    def test_readings_may_be_empty(self):
        pkg, trial = self._good()
        trial["readings"] = []
        certificate, _ = self.calibrate(pkg, self.write_trial(trial))
        self.assertEqual(certificate["readings"], [])

    def test_duplicate_json_key(self):
        pkg, trial = self._good()
        raw = json.dumps(trial)
        raw = raw.replace("\"readings\": []",
                          "\"readings\": [], \"readings\": []")
        self.assert_rejected(trial, raw=raw)

    def test_unknown_field(self):
        pkg, trial = self._good()
        trial["extra"] = 1
        self.assert_rejected(trial)

    def test_unknown_step_direction(self):
        pkg, trial = self._good()
        trial["steps"][0]["direction"] = "sideways"
        self.assert_rejected(trial)


class ChunkSplitting(unittest.TestCase):
    """A segment split over several decode blocks must sum exactly once."""

    def test_segments_tile_each_chunk(self):
        from signal_spectrum.calibration.evaluate import _chunk_segments

        class FakeStep:
            def __init__(self, start, stop):
                self.start = start
                self.stop = stop

        steps = [FakeStep(0, 10), FakeStep(10, 25), FakeStep(25, 30)]
        cursor = 0
        covered = {index: 0 for index in range(len(steps))}
        for chunk_start, chunk_stop in ((0, 4), (4, 12), (12, 30)):
            segments, cursor = _chunk_segments(steps, chunk_start,
                                               chunk_stop, cursor)
            for index, first, last in segments:
                covered[index] += last - first
        self.assertEqual(covered, {0: 10, 1: 15, 2: 5})
        self.assertEqual(cursor, len(steps))

    def test_empty_chunk_keeps_cursor(self):
        from signal_spectrum.calibration.evaluate import _chunk_segments

        class FakeStep:
            def __init__(self, start, stop):
                self.start = start
                self.stop = stop

        steps = [FakeStep(5, 10)]
        segments, cursor = _chunk_segments(steps, 0, 5, 0)
        self.assertEqual(segments, [])
        self.assertEqual(cursor, 0)


class Limits(CalibrationCase):
    def _package(self):
        steps, raw_values = acceptance_steps()
        pkg = self.write_package(
            "pkg", "cap-limits", 100, len(raw_values), "float32", "little",
            [("main", 1.0, 0.0)], lambda p, c: float(raw_values[p]))
        return pkg, steps

    def test_trial_file_over_two_mib_rejected(self):
        pkg, steps = self._package()
        trial = {"steps": steps, "readings": [],
                 "residual_tolerance": 1, "hysteresis_tolerance": 3}
        raw = exact_json(trial)
        raw = raw[:-1] + ',"pad":"' + "x" * (2 * 1024 * 1024) + '"}'
        path = self.root / "big.json"
        path.write_text(raw)
        self.run_cli("--package", pkg, "--steps", path, "--certificate",
                     self.root / "cert.json", expect=2)

    def test_too_many_steps_rejected(self):
        pkg, steps = self._package()
        trial = {"steps": [step(0, 1, 0, "up")] * 1025,
                 "readings": [], "residual_tolerance": 1,
                 "hysteresis_tolerance": 3}
        path = self.root / "many.json"
        path.write_text(exact_json(trial))
        self.run_cli("--package", pkg, "--steps", path, "--certificate",
                     self.root / "cert.json", expect=2)

    def test_too_many_readings_rejected(self):
        pkg, steps = self._package()
        trial = {"steps": steps,
                 "readings": [reading("r%d" % n, "main", "up", 0, 1)
                              for n in range(4097)],
                 "residual_tolerance": 1, "hysteresis_tolerance": 3}
        path = self.root / "readings.json"
        path.write_text(exact_json(trial))
        self.run_cli("--package", pkg, "--steps", path, "--certificate",
                     self.root / "cert.json", expect=2)

    def test_max_readings_accepted(self):
        pkg, steps = self._package()
        trial = {"steps": steps,
                 "readings": [reading("r%04d" % n, "main", "up", 0, 1)
                              for n in range(4096)],
                 "residual_tolerance": 1, "hysteresis_tolerance": 3}
        certificate, _ = self.calibrate(pkg, self.write_trial(trial))
        self.assertEqual(len(certificate["readings"]), 4096)
        self.assertEqual(certificate["readings"][0]["id"], "r0000")


class IndependentMath(unittest.TestCase):
    def test_isotonic_matches_brute_force(self):
        import itertools
        import random

        def subarray_means(values):
            candidates = set()
            for i in range(len(values)):
                total = Fraction(0)
                for j in range(i, len(values)):
                    total += values[j]
                    candidates.add(total / (j - i + 1))
            return sorted(candidates)

        random.seed(20261118)
        for _ in range(40):
            n = random.randint(1, 5)
            values = [Fraction(random.randint(-3, 3)) for _ in range(n)]
            # An optimal isotonic sequence is always a block-wise average of
            # contiguous runs, so every fitted value lies in this candidate
            # set.  Enumerate all non-decreasing assignments exactly.
            candidates = subarray_means(values)
            best = None
            for combo in itertools.product(candidates, repeat=n):
                if any(combo[i] > combo[i + 1] for i in range(n - 1)):
                    continue
                cost = sum((values[i] - combo[i]) ** 2 for i in range(n))
                if best is None or cost < best:
                    best = cost
            ours = isotonic(values)
            self.assertTrue(all(ours[i] <= ours[i + 1] for i in range(n - 1)))
            our_cost = sum((values[i] - ours[i]) ** 2 for i in range(n))
            self.assertEqual(our_cost, best)

    def test_isotonic_acceptance(self):
        self.assertEqual(isotonic([Fraction(0), Fraction(4), Fraction(2),
                                   Fraction(6)]),
                         [Fraction(0), Fraction(3), Fraction(3),
                          Fraction(6)])

    def test_merge_preserves_gaps(self):
        self.assertEqual(merge_intervals([(Fraction(0), Fraction(1)),
                                          (Fraction(2), Fraction(3))]),
                         [[Fraction(0), Fraction(1)],
                          [Fraction(2), Fraction(3)]])
        self.assertEqual(merge_intervals([(Fraction(0), Fraction(1)),
                                          (Fraction(1), Fraction(2))]),
                         [[Fraction(0), Fraction(2)]])

    def test_classification(self):
        self.assertEqual(classify([]), "outside")
        self.assertEqual(classify([(Fraction(1), Fraction(1))]), "unique")
        self.assertEqual(classify([(Fraction(1), Fraction(2))]),
                         "ambiguous")
        self.assertEqual(classify([(Fraction(1), Fraction(1)),
                                   (Fraction(2), Fraction(2))]),
                         "ambiguous")

    def test_inverse_does_not_extrapolate(self):
        nodes_x = [Fraction(0), Fraction(1), Fraction(2)]
        nodes_y = [Fraction(0), Fraction(1), Fraction(2)]
        self.assertEqual(inverse_intervals(nodes_x, nodes_y,
                                           Fraction(-1), Fraction(-1)), [])
        self.assertEqual(inverse_intervals(nodes_x, nodes_y,
                                           Fraction(3), Fraction(4)), [])


if __name__ == "__main__":
    unittest.main()
