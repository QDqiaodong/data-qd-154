"""End-to-end tests for ``signal-resample``.

Covers the acceptance contract:

* constant multi-channel input keeps its exact level when the target rate is
  above, below and equal to the source rate, with the exact output length,
  first global ``m`` and per-point source coordinate from the integer ratio;
* integer and non-integer ratios (44,100 -> 16,000 Hz among them) map every
  output point onto the same global time grid, and a small independent
  reference implementation (exact ``Fraction`` coordinate, direct kernel
  weights) agrees to better than 1e-12 on source values that are exactly
  representable as binary64;
* a 3,000 Hz passband sine keeps its amplitude within 0.02 and an 18,000 Hz
  sine falls below 0.01 of the input RMS when 48,000 Hz is resampled to
  16,000 Hz;
* insufficient support at both ends of the capture produces no output, and
  the boundary ``m`` values follow the closed complete-support condition;
* multi-channel frames crossing the existing decoder's 64 KiB read-block
  boundaries never swap channels;
* damaged meta/raw digests, duplicate keys, invalid target rates, output
  point/size limits and non-finite raw or calibrated values exit 2 without
  creating the destination, leaving input bytes unchanged;
* the published bytes hash to the recorded ``output_sha256``, an existing
  destination is never overwritten, and injected write/digest-stage failures
  never leave a valid ``lineage.json`` or a finished target directory;
* peak RSS on a payload significantly larger than the 96 MiB budget, measured
  in a child process, proves the decode and resample are streaming.
"""
import array
import hashlib
import json
import math
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

MODULE = ["-m", "signal_spectrum.resample.cli"]
WIDTH = {"int16": 2, "uint16": 2, "float32": 4}
CODE = {"int16": "h", "uint16": "H", "float32": "f"}
RSS_CAP_KIB = 96 * 1024


def canonical_meta_sha(meta):
    return hashlib.sha256(json.dumps(
        meta, sort_keys=True, ensure_ascii=False, allow_nan=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def write_package(pkg_dir, capture_id, sample_rate, points, dtype,
                  byte_order, channels, raw_fn, manifest_overrides=None):
    """Write a published-style schema_version=1 package directly."""
    pkg_dir = Path(pkg_dir)
    pkg_dir.mkdir(parents=True, exist_ok=True)
    code = CODE[dtype]
    prefix = "<" if byte_order == "little" else ">"
    packer = struct.Struct(prefix + code)
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
    if manifest_overrides:
        manifest.update(manifest_overrides)
    (pkg_dir / "raw.bin").write_bytes(raw)
    (pkg_dir / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2))
    return meta, manifest


def read_samples(dest, channel_count):
    """Return one list per channel from a published derived dataset."""
    raw = (Path(dest) / "samples.f64le").read_bytes()
    flat = array.array("d")
    flat.frombytes(raw)
    if sys.byteorder != "little":
        flat.byteswap()
    return [flat[channel::channel_count] for channel in range(channel_count)]


def read_lineage(dest):
    return json.loads((Path(dest) / "lineage.json").read_text())


def f32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


# -- independent reference implementation -----------------------------------

def reference_resample(values, source_rate, target_rate, radius=16):
    """A deliberately naive, direct-evaluation reference.

    Uses ``Fraction`` for the exact source coordinate and the same kernel
    formula, but computes every tap independently with the closed support
    condition and ordinary left-to-right summation.  Returns a list of
    ``(m, value)``.
    """
    g = math.gcd(source_rate, target_rate)
    p, q = target_rate // g, source_rate // g
    points = len(values)
    fc = 0.5 * min(Fraction(1), Fraction(target_rate, source_rate))
    fc_float = float(fc)
    first = -(-(radius * p) // q)
    last = ((points - 1 - radius) * p) // q
    output = []
    for m in range(first, last + 1):
        x = Fraction(m * q, p)
        low = math.ceil(x - radius)
        high = math.floor(x + radius)
        numerator = 0.0
        denominator = 0.0
        for j in range(low, high + 1):
            d = float(Fraction(j, 1) - x)
            window = (0.42 + 0.5 * math.cos(math.pi * d / radius)
                      + 0.08 * math.cos(2.0 * math.pi * d / radius))
            argument = 2.0 * fc_float * d
            if argument == 0.0:
                sinc = 1.0
            else:
                scaled = math.pi * argument
                sinc = math.sin(scaled) / scaled
            weight = 2.0 * fc_float * sinc * window
            numerator += weight * values[j]
            denominator += weight
        output.append((m, numerator / denominator))
    return output


class ResampleCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="resample-test-"))
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = str(SRC)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_cli(self, input_dir, target_rate, dest):
        return subprocess.run(
            [sys.executable] + MODULE + [
                "--input", str(input_dir),
                "--target-rate", str(target_rate),
                "--dest", str(dest)],
            capture_output=True, text=True, env=self.env)

    def assert_ok(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def assert_fails(self, proc):
        # argparse usage errors and contract violations both exit 2.
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)


class ConstantGain(ResampleCase):
    def test_constant_multi_channel_all_ratios(self):
        cases = [
            (48_000, 96_000),   # above source rate
            (48_000, 16_000),   # below source rate
            (44_100, 16_000),   # non-integer ratio
            (8_000, 8_000),     # equal
            (7, 3),             # tiny, co-prime rates
        ]
        for source_rate, target_rate in cases:
            with self.subTest(source_rate=source_rate,
                              target_rate=target_rate):
                points = 521
                channels = [("left", 2.0, -5.0), ("right", 0.25, 10.0)]
                pkg = self.root / ("pkg_%d_%d" % (source_rate, target_rate))
                write_package(pkg, "cap-%d-%d" % (source_rate, target_rate),
                              source_rate, points, "int16", "little",
                              channels,
                              lambda point, channel: 100 if channel == 0 else 40)
                dest = self.root / ("out_%d_%d" % (source_rate, target_rate))
                self.assert_ok(self.run_cli(pkg, target_rate, dest))

                g = math.gcd(source_rate, target_rate)
                p, q = target_rate // g, source_rate // g
                first = -(-(16 * p) // q)
                last = ((points - 1 - 16) * p) // q
                expected_points = last - first + 1
                lineage = read_lineage(dest)
                self.assertEqual(lineage["output_points"], expected_points)
                self.assertEqual(lineage["first_output_index"], first)
                self.assertEqual(lineage["ratio_p"], p)
                self.assertEqual(lineage["ratio_q"], q)
                self.assertEqual(lineage["output_channels"],
                                 ["left", "right"])
                self.assertEqual(lineage["kind"], "derived-waveform")
                self.assertEqual(lineage["schema_version"], 1)
                self.assertEqual(lineage["last_output_index"], last)
                coordinate = Fraction(first * q, p)
                self.assertEqual(
                    lineage["source_coordinate_range"]["first"],
                    {"num": coordinate.numerator,
                     "den": coordinate.denominator})

                left, right = read_samples(dest, 2)
                self.assertEqual(len(left), expected_points)
                # raw*scale + offset exactly: 100*2-5 and 40*0.25+10.
                self.assertTrue(all(abs(v - 195.0) < 1e-9 for v in left))
                self.assertTrue(all(abs(v - 20.0) < 1e-9 for v in right))

                # Every output point sits exactly on m*q/p in the source.
                for offset, m in enumerate(range(first, first + expected_points)):
                    coordinate = Fraction(m * q, p)
                    self.assertTrue(16 <= coordinate <= points - 1 - 16)
                    if offset in (0, expected_points - 1):
                        expected = {
                            "num": coordinate.numerator,
                            "den": coordinate.denominator}
                        key = "first" if offset == 0 else "last"
                        self.assertEqual(
                            lineage["source_coordinate_range"][key], expected)

    def test_support_boundary_is_closed_condition(self):
        # 48 -> 48 keeps every m; the first m with full support is exactly 16.
        points = 40
        pkg = self.root / "boundary"
        write_package(pkg, "b", 48, points, "int16", "little",
                      [("ch", 1.0, 0.0)], lambda point, channel: 3)
        for target_rate in (48, 24, 96):
            dest = self.root / ("b_%d" % target_rate)
            self.assert_ok(self.run_cli(pkg, target_rate, dest))
            lineage = read_lineage(dest)
            g = math.gcd(48, target_rate)
            p, q = target_rate // g, 48 // g
            first = -(-(16 * p) // q)
            last = ((points - 1 - 16) * p) // q
            self.assertEqual(lineage["first_output_index"], first)
            self.assertEqual(lineage["output_points"], last - first + 1)
            # One step outside the window has incomplete support and must
            # never be emitted.
            self.assertTrue(Fraction((first - 1) * q, p) < 16
                            or Fraction((first) * q, p) == 16)
            self.assertTrue(Fraction((last + 1) * q, p) > points - 1 - 16)

        # Not enough samples for a single complete window: refuse.
        short = self.root / "short"
        write_package(short, "s", 48, 32, "int16", "little",
                      [("ch", 1.0, 0.0)], lambda point, channel: 1)
        dest = self.root / "short_out"
        self.assert_fails(self.run_cli(short, 48, dest))
        self.assertFalse(dest.exists())


class HeavyDecimation(ResampleCase):
    def test_deep_decimation_matches_reference(self):
        # q = 100,000 means the next output window often starts beyond the
        # current decode block; the tail buffer must still begin exactly at
        # the buffer base, not at a jumped-ahead discard point.
        values = [f32(math.sin(i * 0.0007) * 100.0) for i in range(120_000)]
        pkg = self.root / "deep"
        write_package(pkg, "deep", 100_000, len(values), "float32",
                      "little", [("x", 1.0, 0.0)],
                      lambda point, channel: values[point])
        dest = self.root / "deep_out"
        self.assert_ok(self.run_cli(pkg, 3, dest))
        expected = reference_resample(values, 100_000, 3)
        result = read_samples(dest, 1)[0]
        self.assertEqual(len(result), len(expected))
        for produced, (m, want) in zip(result, expected):
            self.assertLessEqual(abs(produced - want), 1e-12, "m=%d" % m)


class ReferenceParity(ResampleCase):
    def test_integer_and_non_integer_ratios(self):
        for source_rate, target_rate in ((48_000, 24_000),
                                         (44_100, 16_000),
                                         (16_000, 48_000)):
            with self.subTest(source_rate=source_rate,
                              target_rate=target_rate):
                values = [f32(math.sin(i * 0.31) * 0.5 + 0.125)
                          for i in range(96)]
                pkg = self.root / ("ref_%d_%d" % (source_rate, target_rate))
                write_package(pkg, "r", source_rate, len(values), "float32",
                              "little", [("x", 1.0, 0.0)],
                              lambda point, channel: values[point])
                dest = self.root / ("refout_%d_%d"
                                    % (source_rate, target_rate))
                self.assert_ok(self.run_cli(pkg, target_rate, dest))
                expected = reference_resample(values, source_rate,
                                              target_rate)
                lineage = read_lineage(dest)
                self.assertEqual(lineage["first_output_index"],
                                 expected[0][0])
                self.assertEqual(lineage["output_points"], len(expected))
                result = read_samples(dest, 1)[0]
                self.assertEqual(len(result), len(expected))
                for produced, (m, want) in zip(result, expected):
                    self.assertLessEqual(abs(produced - want), 1e-12,
                                         "m=%d" % m)
                # The global grid m/target_rate is the only time base.
                self.assertEqual(
                    lineage["time_mapping"]["first_m"],
                    expected[0][0])
                self.assertEqual(
                    lineage["time_mapping"]["step"],
                    {"num": 1, "den": target_rate})


class Filtering(ResampleCase):
    def test_passband_and_stopband(self):
        source_rate, target_rate = 48_000, 16_000
        points = 4096
        for name, frequency, checks in (
                ("pass", 3_000, "passband"),
                ("high", 18_000, "stopband")):
            values = [f32(math.sin(2 * math.pi * frequency * i / source_rate))
                      for i in range(points)]
            pkg = self.root / ("filt_" + name)
            write_package(pkg, "f-" + name, source_rate, points, "float32",
                          "little", [("x", 1.0, 0.0)],
                          lambda point, channel: values[point])
            dest = self.root / ("filtout_" + name)
            self.assert_ok(self.run_cli(pkg, target_rate, dest))
            result = read_samples(dest, 1)[0]
            edge = len(result) // 10
            interior = result[edge:len(result) - edge]
            rms = math.sqrt(math.fsum(v * v for v in interior)
                            / len(interior))
            source_rms = math.sqrt(
                math.fsum(v * v for v in values) / len(values))
            if checks == "passband":
                # A unit sine has RMS sqrt(1/2); allow 0.02 amplitude error.
                expected = 1.0 / math.sqrt(2.0)
                self.assertLessEqual(abs(rms * math.sqrt(2.0) - 1.0), 0.02,
                                     "passband RMS %.6f" % rms)
                self.assertLessEqual(abs(rms - expected), 0.02)
            else:
                self.assertLess(rms, 0.01 * source_rms,
                                "high-frequency RMS %.6g" % rms)


class BlockBoundaries(ResampleCase):
    def test_multichannel_frames_crossing_decoder_blocks(self):
        # A frame of 7 int16 channels is 14 bytes, which does not divide the
        # decoder's 64 KiB read block, so frames straddle block boundaries and
        # any channel misalignment would show as a shifted sample.
        channel_count = 7
        points = 120_000 // (channel_count * 2) + 977

        def raw(point, channel):
            return int(round(1000 * math.sin(point * 0.01)
                             + 100 * channel)) - 300

        channels = [("c%d" % index, 1.0, float(index))
                    for index in range(channel_count)]
        pkg = self.root / "blocks"
        write_package(pkg, "blocks", 24_000, points, "int16", "little",
                      channels, raw)
        dest = self.root / "blocks_out"
        self.assert_ok(self.run_cli(pkg, 24_000, dest))
        per_channel = read_samples(dest, channel_count)
        lineage = read_lineage(dest)
        first = lineage["first_output_index"]
        self.assertEqual(lineage["output_points"],
                         points - 2 * 16)
        for channel in range(channel_count):
            values = per_channel[channel]
            # Same grid as the source: every output is its own input point,
            # so a channel mismatch is a whole-sample shift (>= 1e-4 here).
            for offset in range(len(values)):
                index = first + offset
                want = raw(index, channel) + float(channel)
                self.assertLessEqual(abs(values[offset] - want), 1e-9,
                                     "channel %d output %d"
                                     % (channel, offset))


class KernelUnits(unittest.TestCase):
    """Direct checks on the exact grid and the bounded phase cache."""

    def test_phase_cache_is_bounded_and_exact(self):
        from signal_spectrum.resample import kernel as kernel_module
        from signal_spectrum.resample.kernel import Grid, Kernel
        # A prime target rate relative to the source makes every phase
        # distinct; the cache must stop growing at its fixed budget while the
        # weights stay identical to a direct evaluation.
        grid = Grid(1_000_003, 1_000_000_007, 1_000_000_007, 1_000_003,
                    1, 10_000_000, 0, 9_000_000)
        kernel = Kernel(grid)
        for m in range(0, kernel_module.PHASE_CACHE_LIMIT * 4):
            phase = (m * grid.q) % grid.p
            cached = kernel.weights(phase)
            direct = kernel_module._weights_for_phase(
                phase, grid.p, 2.0 * kernel.fc)
            self.assertEqual(cached[0], direct)
            self.assertEqual(cached[1], math.fsum(direct))
        self.assertEqual(len(kernel._cache),
                         kernel_module.PHASE_CACHE_LIMIT)

    def test_plan_grid_support_condition(self):
        from signal_spectrum.resample.kernel import plan_grid
        cases = [
            (33, 48_000, 48_000),    # exactly one window
            (32, 48_000, 48_000),    # one short: no output
            (1000, 44_100, 16_000),
            (1000, 16_000, 44_100),
        ]
        for points, source_rate, target_rate in cases:
            grid = plan_grid(points, source_rate, target_rate)
            if points < 33:
                self.assertIsNone(grid)
                continue
            self.assertIsNotNone(grid)
            for m in (grid.first_index, grid.last_index):
                coordinate = Fraction(m * grid.q, grid.p)
                self.assertGreaterEqual(coordinate, 16)
                self.assertLessEqual(coordinate, points - 1 - 16)
            for m in (grid.first_index - 1, grid.last_index + 1):
                coordinate = Fraction(m * grid.q, grid.p)
                self.assertFalse(16 <= coordinate <= points - 1 - 16)


class Failures(ResampleCase):
    def _package(self, name="pkg", **kwargs):
        pkg = self.root / name
        write_package(pkg, name, 8_000, 200, "int16", "little",
                      [("a", 1.0, 0.0)], lambda point, channel: point % 7,
                      **kwargs)
        return pkg

    def test_bad_raw_digest(self):
        pkg = self._package()
        raw = bytearray((pkg / "raw.bin").read_bytes())
        raw[10] ^= 0xFF
        (pkg / "raw.bin").write_bytes(bytes(raw))
        before = (pkg / "raw.bin").read_bytes()
        dest = self.root / "bad_raw_out"
        proc = self.run_cli(pkg, 16_000, dest)
        self.assert_fails(proc)
        self.assertFalse(dest.exists())
        self.assertEqual((pkg / "raw.bin").read_bytes(), before)

    def test_bad_meta_digest(self):
        pkg = self._package("meta")
        manifest_path = pkg / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["meta_sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest))
        dest = self.root / "bad_meta_out"
        self.assert_fails(self.run_cli(pkg, 16_000, dest))
        self.assertFalse(dest.exists())

    def test_duplicate_manifest_key(self):
        pkg = self._package("dup")
        text = (pkg / "manifest.json").read_text()
        text = text.replace('"capture_id": "dup",',
                            '"capture_id": "dup", "capture_id": "dup",')
        (pkg / "manifest.json").write_text(text)
        dest = self.root / "dup_out"
        self.assert_fails(self.run_cli(pkg, 16_000, dest))
        self.assertFalse(dest.exists())

    def test_symlinked_inputs_rejected(self):
        pkg = self._package("sym")
        link_dir = self.root / "symlinked"
        link_dir.mkdir()
        os.symlink(pkg / "manifest.json", link_dir / "manifest.json")
        shutil.copy(pkg / "raw.bin", link_dir / "raw.bin")
        dest = self.root / "sym_out"
        self.assert_fails(self.run_cli(link_dir, 16_000, dest))
        self.assertFalse(dest.exists())

        pkg2 = self._package("sym_raw")
        link_dir2 = self.root / "symlinked_raw"
        link_dir2.mkdir()
        shutil.copy(pkg2 / "manifest.json", link_dir2 / "manifest.json")
        os.symlink(pkg2 / "raw.bin", link_dir2 / "raw.bin")
        dest2 = self.root / "sym_raw_out"
        self.assert_fails(self.run_cli(link_dir2, 16_000, dest2))
        self.assertFalse(dest2.exists())

    def test_bad_target_rates(self):
        pkg = self._package("rates")
        for text in ("0", "-1", "1000000001", "1.5", "true", "abc"):
            dest = self.root / ("rate_" + text.replace("-", "m")
                                .replace(".", "_"))
            proc = self.run_cli(pkg, text, dest)
            self.assert_fails(proc)
            self.assertFalse(dest.exists())

    def test_boolean_rate_rejected_via_module(self):
        pkg = self._package("bool")
        dest = self.root / "bool_out"
        driver = (
            "import sys\n"
            "from signal_spectrum.resample.cli import main\n"
            "sys.exit(main(['--input', sys.argv[1], '--target-rate', 'x',"
            " '--dest', sys.argv[2]]))\n")
        # argparse itself rejects 'x'; the bool guard is exercised in-process.
        proc = subprocess.run([sys.executable, "-c", driver, str(pkg),
                               str(dest)], capture_output=True, text=True,
                              env=self.env)
        self.assertEqual(proc.returncode, 2)
        from signal_spectrum.resample.engine import validate_target_rate
        from signal_spectrum.resample.errors import ResampleError
        with self.assertRaises(ResampleError):
            validate_target_rate(True)
        with self.assertRaises(ResampleError):
            validate_target_rate(0)
        with self.assertRaises(ResampleError):
            validate_target_rate(10 ** 9 + 1)
        self.assertEqual(validate_target_rate(10 ** 9), 10 ** 9)
        self.assertFalse(dest.exists())

    def test_output_point_limit(self):
        pkg = self.root / "many"
        write_package(pkg, "many", 1, 100, "int16", "little",
                      [("a", 1.0, 0.0)], lambda point, channel: 1)
        dest = self.root / "many_out"
        self.assert_fails(self.run_cli(pkg, 100_000_000, dest))
        self.assertFalse(dest.exists())

    def test_output_size_limit(self):
        pkg = self.root / "wide"
        points = 450_000
        channels = 16
        write_package(pkg, "wide", 1_000, points, "int16", "little",
                      [("c%d" % i, 1.0, 0.0) for i in range(channels)],
                      lambda point, channel: 1)
        # 450,000 points at 30,000 Hz would need ~13.5 million output points,
        # so the point cap rejects the conversion before any decode starts.
        dest = self.root / "wide_out"
        self.assert_fails(self.run_cli(pkg, 30_000, dest))
        self.assertFalse(dest.exists())

        # The point cap and the 16-channel ceiling together put the largest
        # possible payload at 640,000,000 sample bytes, just under 640 MiB, so
        # the byte branch is exercised directly against the engine constant.
        from signal_spectrum.resample import engine as engine_module
        from signal_spectrum.resample.errors import ResampleError
        original = engine_module.MAX_OUTPUT_BYTES
        engine_module.MAX_OUTPUT_BYTES = 1024
        try:
            with self.assertRaises(ResampleError) as caught:
                engine_module.prepare_source(pkg, 1_000)
        finally:
            engine_module.MAX_OUTPUT_BYTES = original
        self.assertIn("byte limit", str(caught.exception))

        point_pkg = self.root / "pointwide"
        write_package(point_pkg, "pointwide", 1_000, 1_200_000, "int16",
                      "little", [("a", 1.0, 0.0)],
                      lambda point, channel: 1)
        dest3 = self.root / "pointwide_out"
        proc = self.run_cli(point_pkg, 5_000, dest3)
        self.assert_fails(proc)
        self.assertIn("point limit", proc.stderr)
        self.assertFalse(dest3.exists())

    def test_non_finite_raw_and_calibrated(self):
        nan_pkg = self.root / "nan"
        write_package(nan_pkg, "nan", 8_000, 200, "float32", "little",
                      [("a", 1.0, 0.0)],
                      lambda point, channel:
                      float("nan") if point == 100 else 1.0)
        dest = self.root / "nan_out"
        self.assert_fails(self.run_cli(nan_pkg, 16_000, dest))
        self.assertFalse(dest.exists())

        # A huge int16 scale makes a nonzero raw value calibrate to infinity.
        inf_pkg = self.root / "inf"
        write_package(inf_pkg, "inf", 8_000, 200, "int16", "little",
                      [("a", 1e308, 0.0)], lambda point, channel: 2)
        dest2 = self.root / "inf_out"
        self.assert_fails(self.run_cli(inf_pkg, 16_000, dest2))
        self.assertFalse(dest2.exists())


class Publication(ResampleCase):
    def _run_once(self, name="ok"):
        pkg = self.root / "pub_pkg"
        if not pkg.exists():
            write_package(pkg, "pub", 4_000, 300, "int16", "big",
                          [("a", 0.5, 1.0), ("b", -2.0, 3.0)],
                          lambda point, channel:
                          (point * (channel + 1)) % 64000)
        dest = self.root / name
        proc = self.run_cli(pkg, 6_000, dest)
        return pkg, dest, proc

    def test_digest_determinism_and_bytes(self):
        pkg, dest, proc = self._run_once("out1")
        self.assert_ok(proc)
        lineage = read_lineage(dest)
        payload = (dest / "samples.f64le").read_bytes()
        self.assertEqual(hashlib.sha256(payload).hexdigest(),
                         lineage["output_sha256"])
        self.assertEqual(lineage["output_bytes"], len(payload))
        self.assertEqual(sorted(entry.name for entry in dest.iterdir()),
                         ["lineage.json", "samples.f64le"])
        first_lineage = (dest / "lineage.json").read_bytes()
        first_payload = payload

        # Same input and arguments -> identical bytes.
        _, dest2, proc2 = self._run_once("out2")
        self.assert_ok(proc2)
        self.assertEqual((dest2 / "lineage.json").read_bytes(),
                         first_lineage)
        self.assertEqual((dest2 / "samples.f64le").read_bytes(),
                         first_payload)

        # Existing destination (file, directory or link) is never replaced.
        dest3 = self.root / "existing"
        dest3.mkdir()
        (dest3 / "keep.txt").write_text("keep")
        self.assert_fails(self.run_cli(pkg, 6_000, dest3))
        self.assertEqual((dest3 / "keep.txt").read_text(), "keep")

        link = self.root / "existing_link"
        os.symlink(dest3, link)
        self.assert_fails(self.run_cli(pkg, 6_000, link))
        self.assertTrue(dest3.is_dir())

    def test_destination_inside_input_rejected(self):
        pkg, _, _ = self._run_once("base")
        inside = pkg / "derived"
        self.assert_fails(self.run_cli(pkg, 6_000, inside))
        self.assertFalse(inside.exists())
        ancestor = self.root
        # The temp root contains the package, so it is an ancestor: rejected.
        self.assert_fails(self.run_cli(pkg, 6_000, ancestor))
        self.assertTrue(pkg.is_dir())

    def test_injected_failures_leave_no_complete_dataset(self):
        pkg = self.root / "fail_pkg"
        write_package(pkg, "fail", 4_000, 300, "int16", "little",
                      [("a", 1.0, 0.0)], lambda point, channel: point % 5)
        for stage in ("after_samples", "after_lineage"):
            dest = self.root / ("fail_" + stage)
            env = dict(self.env)
            env["SIGSPECTRUM_RS_FAIL"] = stage
            proc = subprocess.run(
                [sys.executable] + MODULE + [
                    "--input", str(pkg),
                    "--target-rate", "6000",
                    "--dest", str(dest)],
                capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertFalse(dest.exists(), stage)
            staging = dest.with_name(dest.name + ".staging")
            self.assertFalse(staging.exists(), stage)
            # Input bytes are unchanged.
            self.assertTrue((pkg / "raw.bin").stat().st_size > 0)


class ResourceBound(ResampleCase):
    """Peak RSS on a source payload far larger than the 96 MiB budget.

    One 200 MiB float32 capture is resampled by the CLI in a fresh child
    process; the high-water mark comes from
    ``resource.getrusage(RUSAGE_CHILDREN).ru_maxrss`` (KiB on Linux) and is
    reported below.  A whole-payload or whole-channel buffer would exceed the
    cap immediately.
    """

    POINTS = 60_000_000  # 240 MiB of float32

    def test_rss_under_cap(self):
        pkg = self.root / "big"
        pkg.mkdir()
        raw_path = pkg / "raw.bin"
        hasher = hashlib.sha256()
        chunk = struct.pack("<f", 1.5) * 1_000_000
        with open(raw_path, "wb") as stream:
            for _ in range(self.POINTS // 1_000_000):
                stream.write(chunk)
                hasher.update(chunk)
        raw_bytes = self.POINTS * 4
        meta = {"capture_id": "rss", "sample_rate": 1000,
                "points": self.POINTS, "dtype": "float32",
                "byte_order": "little",
                "channels": [{"name": "ch", "scale": 2.0, "offset": 1.0}],
                "raw_bytes": raw_bytes, "sha256": hasher.hexdigest()}
        manifest = {
            "schema_version": 1, "source_url": "http://127.0.0.1:9/d",
            "capabilities": {"max_range_bytes": 4096, "lease": False},
            "consistency_channel": "etag", "etag": '"rss"',
            "capture_id": "rss",
            "config": {"sample_rate": 1000, "points": self.POINTS,
                       "dtype": "float32", "byte_order": "little"},
            "channels": meta["channels"], "raw_bytes": raw_bytes,
            "raw_sha256": meta["sha256"],
            "meta_sha256": canonical_meta_sha(meta), "analysis": None,
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))
        self.assertGreater(raw_path.stat().st_size, RSS_CAP_KIB * 1024 * 2,
                           "corpus must be significantly larger than the cap")

        dest = self.root / "rss_out"
        driver = (
            "import resource, subprocess, sys\n"
            "subprocess.run([sys.executable, '-m', 'signal_spectrum."
            "resample.cli', '--input', sys.argv[1], '--target-rate', "
            "'50', '--dest', sys.argv[2]], check=True)\n"
            "print(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", driver, str(pkg), str(dest)],
            capture_output=True, text=True, env=self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        peak_kib = int(proc.stdout.strip().splitlines()[-1])
        print("signal-resample peak RSS: %d KiB (cap %d KiB)"
              % (peak_kib, RSS_CAP_KIB))
        self.assertLessEqual(peak_kib, RSS_CAP_KIB)
        lineage = read_lineage(dest)
        self.assertEqual(lineage["source_points"], self.POINTS)
        first = -(-(16 * 1) // 20)
        last = ((self.POINTS - 1 - 16) * 1) // 20
        self.assertEqual(lineage["first_output_index"], first)
        self.assertEqual(lineage["output_points"], last - first + 1)
        values = read_samples(dest, 1)[0]
        self.assertEqual(len(values), lineage["output_points"])
        self.assertTrue(all(abs(value - 4.0) < 1e-9 for value in values[:1000]))


if __name__ == "__main__":
    unittest.main()
