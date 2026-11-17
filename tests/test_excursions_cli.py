"""End-to-end CLI tests for signal-excursions.

Covers the acceptance scenarios:

* build from real ``signal-waveform-fetch`` packages (both endiannesses,
  multi-channel, non-unit calibration), same ``capture_id`` under different
  ``source_id`` values, raw-SHA conflict rejection, and identical query
  results after the source packages are deleted;
* excursion semantics against an independent point-by-point naive
  expectation: runs spanning leaf segments and summary nodes, truncation at
  the query boundary, threshold equality (counted by ``ge``, excluded by
  ``lt``), earliest tied peak, K-run pagination with ``has_more``, negative
  thresholds, first/last-point hits, an all-equal channel, same channel
  name in multiple sources, and unknown source/channel errors;
* index acceptance on a 1,048,576-point channel proving summary-only
  pruning (bounded ``node_reads``/``value_reads``, zero value reads for
  fully decidable queries);
* build-time rejection of malformed SOURCES/manifests/packages and of
  corrupt or unsupported archives at query time;
* peak-RSS measurement of build and query on a raw corpus significantly
  larger than the 192 MiB cap (method documented in
  ``docs/excursions-contract.md``).

Every query runs in a fresh subprocess after the build subprocess finished,
so read-count acceptance can never be served by build-time in-memory state.
"""
import hashlib
import json
import os
import resource
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from signal_spectrum.waveform.collector import build_config, run_acquisition
from signal_spectrum.waveform.device import DeviceServer, build_capture
from signal_spectrum.waveform.store import Staging

MODULE = ["-m", "signal_spectrum.excursions.cli"]
WIDTH = {"int16": 2, "uint16": 2, "float32": 4}
CODE = {"int16": "h", "uint16": "H", "float32": "f"}
RSS_CAP_KIB = 192 * 1024  # 192 MiB, Linux ru_maxrss unit


def canonical_meta_sha(meta):
    return hashlib.sha256(json.dumps(
        meta, sort_keys=True, ensure_ascii=False, allow_nan=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def write_package(pkg_dir, capture_id, sample_rate, points, dtype,
                  byte_order, channels, raw_fn):
    """Write a published-style package (manifest.json + raw.bin) directly."""
    pkg_dir = Path(pkg_dir)
    pkg_dir.mkdir(parents=True)
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
        "etag": '"' + hashlib.sha256(raw).hexdigest()[:32] + '"',
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
    """Independent point-by-point decode straight from raw.bin."""
    pkg_dir = Path(pkg_dir)
    manifest = json.loads((pkg_dir / "manifest.json").read_text())
    channels = manifest["channels"]
    names = [spec["name"] for spec in channels]
    index = names.index(channel_name)
    dtype = manifest["config"]["dtype"]
    width = WIDTH[dtype]
    prefix = "<" if manifest["config"]["byte_order"] == "little" else ">"
    unpack = struct.Struct(prefix + CODE[dtype]).unpack
    raw = (pkg_dir / "raw.bin").read_bytes()
    frame = width * len(channels)
    spec = channels[index]
    values = []
    for offset in range(0, len(raw), frame):
        (raw_value,) = unpack(raw[offset + index * width:
                                  offset + (index + 1) * width])
        values.append(raw_value * spec["scale"] + spec["offset"])
    return values


def naive_runs(values, start, stop, op, threshold, limit):
    """Independent maximal-run scan used as the ground truth."""
    runs = []
    index = start
    while index < stop:
        value = values[index]
        hit = value >= threshold if op == "ge" else value < threshold
        if not hit:
            index += 1
            continue
        end = index + 1
        peak, peak_index = value, index
        while end < stop:
            current = values[end]
            if not (current >= threshold if op == "ge"
                    else current < threshold):
                break
            if current > peak:
                peak, peak_index = current, end
            end += 1
        runs.append({"start": index, "stop": end,
                     "peak_value": peak, "peak_index": peak_index})
        index = end
    has_more = len(runs) > limit
    return runs[:limit], has_more


class CliCase(unittest.TestCase):
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

    def build(self, sources_path, archive, expect=0):
        proc = self.run_cli("build", "--sources", sources_path,
                            "--archive", archive, expect=expect)
        return json.loads(proc.stdout) if expect == 0 else proc

    def query(self, archive, source, channel, start, stop, op, threshold,
              limit, expect=0):
        proc = self.run_cli("query", "--archive", archive,
                            "--source", source, "--channel", channel,
                            "--start", start, "--stop", stop, "--op", op,
                            "--threshold", threshold, "--limit", limit,
                            expect=expect)
        return json.loads(proc.stdout) if expect == 0 else proc

    def write_sources(self, entries, name="SOURCES.json"):
        path = self.root / name
        path.write_text(json.dumps({"sources": entries}))
        return path

    def assert_query_matches_naive(self, archive, source, channel, values,
                                   start, stop, op, threshold, limit):
        result = self.query(archive, source, channel, start, stop, op,
                            threshold, limit)
        expected_runs, expected_more = naive_runs(
            values, start, stop, op, threshold, limit)
        self.assertEqual(result["runs"], expected_runs,
                         "query=%r" % ((source, channel, start, stop, op,
                                        threshold, limit),))
        self.assertEqual(result["has_more"], expected_more)
        self.assertEqual(result["start"], start)
        self.assertEqual(result["stop"], stop)
        self.assertEqual(result["op"], op)
        self.assertEqual(result["threshold"], float(threshold))
        self.assertLessEqual(result["value_reads"],
                             256 * result["leaf_reads"])
        return result


class RealPackageBuilds(CliCase):
    """Build from packages really fetched over loopback HTTP."""

    def setUp(self):
        super().setUp()
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.stop()
        super().tearDown()

    def fetch(self, capture, name):
        server = DeviceServer(capture, max_range_bytes=101)
        server.start()
        self.servers.append(server)
        out = self.root / name
        run_acquisition(build_config(server.url()), Staging(out))
        return out

    def test_real_packages_mixed_layouts_and_source_evidence(self):
        cap_a = build_capture(
            "cap-shared", 2000, 48, "int16", "little",
            [("main", 0.001, 0.0), ("b", 0.002, 1.5)],
            lambda p, c: ((p * 7 + c * 13) % 200) - 100)
        cap_b = build_capture(
            "cap-shared", 500, 64, "float32", "big",
            [("main", 0.25, -3.0)],
            lambda p, c: ((p * 11) % 97) - 48.5)
        cap_c = build_capture(
            "cap-c", 1000, 100, "uint16", "big",
            [("u1", 0.5, 2.0), ("u2", 1.0, -1.0)],
            lambda p, c: (p * 37 + c * 101) % 5000)
        pkg_a = self.fetch(cap_a, "pkg_a")
        pkg_b = self.fetch(cap_b, "pkg_b")
        pkg_c = self.fetch(cap_c, "pkg_c")

        before = {}
        for pkg in (pkg_a, pkg_b, pkg_c):
            before[pkg] = {
                name: hashlib.sha256((pkg / name).read_bytes()).hexdigest()
                for name in ("manifest.json", "raw.bin")
            }

        sources = self.write_sources([
            {"source_id": "alpha", "package": "pkg_a"},
            {"source_id": "beta", "package": "pkg_b"},
            {"source_id": "gamma", "package": "pkg_c"},
        ])
        archive = self.root / "archive"
        summary = self.build(sources, archive)
        self.assertEqual(summary["source_count"], 3)
        # Same capture_id under different source_ids: both archived.
        by_id = {s["source_id"]: s for s in summary["sources"]}
        self.assertEqual(by_id["alpha"]["capture_id"], "cap-shared")
        self.assertEqual(by_id["beta"]["capture_id"], "cap-shared")
        self.assertNotEqual(by_id["alpha"]["raw_sha256"],
                            by_id["beta"]["raw_sha256"])

        values = {
            ("alpha", "main"): naive_decode_channel(pkg_a, "main"),
            ("alpha", "b"): naive_decode_channel(pkg_a, "b"),
            ("beta", "main"): naive_decode_channel(pkg_b, "main"),
            ("gamma", "u1"): naive_decode_channel(pkg_c, "u1"),
            ("gamma", "u2"): naive_decode_channel(pkg_c, "u2"),
        }
        points = {("alpha", "main"): 48, ("alpha", "b"): 48,
                  ("beta", "main"): 64, ("gamma", "u1"): 100,
                  ("gamma", "u2"): 100}

        def check(source, channel, start, stop, op, threshold, limit=50):
            result = self.assert_query_matches_naive(
                archive, source, channel, values[(source, channel)],
                start, stop, op, threshold, limit)
            meta = {"alpha": cap_a, "beta": cap_b,
                    "gamma": cap_c}[source].meta
            self.assertEqual(result["capture_id"], meta["capture_id"])
            self.assertEqual(result["sample_rate"], meta["sample_rate"])
            self.assertEqual(result["raw_sha256"], meta["sha256"])
            return result

        # Non-unit calibration, both endiannesses, multi-channel interleave.
        check("alpha", "main", 0, 48, "ge", 0.05)
        check("alpha", "main", 3, 40, "lt", -0.02)
        check("alpha", "b", 0, 48, "ge", 1.6)
        check("beta", "main", 0, 64, "ge", 5.0)
        check("beta", "main", 10, 60, "lt", 0.0)
        check("gamma", "u1", 0, 100, "ge", 2000.0)
        check("gamma", "u2", 5, 99, "lt", 100.0)

        # The build must not modify the source packages.
        for pkg, digests in before.items():
            for name, digest in digests.items():
                self.assertEqual(
                    hashlib.sha256((pkg / name).read_bytes()).hexdigest(),
                    digest)

        # The archive is self-contained: remove every source package and
        # all query results stay identical.
        saved = {}
        for key, vals in values.items():
            saved[key] = self.query(archive, key[0], key[1], 0,
                                    points[key], "ge", -1.0, 100)
        for pkg in (pkg_a, pkg_b, pkg_c):
            shutil.rmtree(pkg)
        for key, previous in saved.items():
            again = self.query(archive, key[0], key[1], 0, points[key],
                               "ge", -1.0, 100)
            self.assertEqual(again["runs"], previous["runs"])
            self.assertEqual(again["raw_sha256"], previous["raw_sha256"])
            self.assertEqual(again["manifest_sha256"],
                             previous["manifest_sha256"])

    def test_raw_sha_conflict_refuses_publication(self):
        cap = build_capture("cap-x", 1000, 32, "int16", "little",
                            [("main", 0.5, 0.0)],
                            lambda p, c: p % 50)
        pkg = self.fetch(cap, "pkg_tampered")
        raw_path = pkg / "raw.bin"
        data = bytearray(raw_path.read_bytes())
        data[10] ^= 0xFF
        raw_path.write_bytes(bytes(data))
        sources = self.write_sources(
            [{"source_id": "s", "package": "pkg_tampered"}])
        archive = self.root / "archive_sha"
        self.build(sources, archive, expect=2)
        self.assertFalse(archive.exists())
        self.assertFalse((self.root / "archive_sha.staging").exists())

    def test_manifest_meta_sha_inconsistency_rejected(self):
        cap = build_capture("cap-y", 1000, 32, "int16", "little",
                            [("main", 0.5, 0.0)],
                            lambda p, c: p % 50)
        pkg = self.fetch(cap, "pkg_meta")
        manifest_path = pkg / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["config"]["sample_rate"] = 999  # not re-fingerprinted
        manifest_path.write_text(json.dumps(manifest, sort_keys=True,
                                            indent=2))
        sources = self.write_sources(
            [{"source_id": "s", "package": "pkg_meta"}])
        self.build(sources, self.root / "archive_meta", expect=2)
        self.assertFalse((self.root / "archive_meta").exists())


class Semantics(CliCase):
    """Semantic acceptance against an independent point-by-point scan."""

    POINTS = 20000

    def setUp(self):
        super().setUp()
        spikes = {}
        for p in range(0, 5):
            spikes[p] = 12        # calibrated -97.0, first-point region
        for p in range(1000, 1100):
            spikes[p] = 400       # 0.0
        spikes[7000] = 404        # 1.0
        spikes[7001] = 396        # -1.0
        spikes[12000] = 400       # 0.0 single point
        for p in range(16000, 16500):
            spikes[p] = 440       # 10.0, tied peak, earliest at 16000
        for p in range(16500, 17000):
            spikes[p] = 400       # 0.0
        for p in range(16800, 16810):
            spikes[p] = 440       # 10.0 again, later tie
        for p in range(19995, 20000):
            spikes[p] = 480       # 20.0, last-point region

        def raw_main(p, c):
            if c == 0:
                return spikes.get(p, 0)
            return 7              # aux: 7*2.0+1.0 = 15.0 everywhere

        self.pkg1 = self.root / "pkg1"
        write_package(self.pkg1, "cap-sem", 4000, self.POINTS, "int16",
                      "little", [("main", 0.25, -100.0), ("aux", 2.0, 1.0)],
                      raw_main)
        self.pkg2 = self.root / "pkg2"
        # Same channel name "main", completely different content.
        write_package(self.pkg2, "cap-sem", 4000, self.POINTS, "int16",
                      "little", [("main", 0.25, -100.0)],
                      lambda p, c: 40)  # 40*0.25-100 = -90.0 everywhere
        self.sources = self.write_sources([
            {"source_id": "s1", "package": "pkg1"},
            {"source_id": "s2", "package": "pkg2"},
        ])
        self.archive = self.root / "archive"
        self.build(self.sources, self.archive)
        self.main = naive_decode_channel(self.pkg1, "main")
        self.aux = naive_decode_channel(self.pkg1, "aux")
        self.main2 = naive_decode_channel(self.pkg2, "main")

    def q(self, source="s1", channel="main", start=0, stop=POINTS, op="ge",
          threshold=0.0, limit=50):
        values = {"main": self.main, "aux": self.aux}[channel]
        if source == "s2":
            values = self.main2
        return self.assert_query_matches_naive(
            self.archive, source, channel, values, start, stop, op,
            threshold, limit)

    def test_run_spanning_leaves_nodes_and_query_boundary(self):
        # [16000,17000) crosses leaves 62..66 and the level-1 node boundary
        # at 16384; the query left boundary cuts into the run.
        result = self.q(start=16500, stop=18000, threshold=0.0)
        self.assertEqual(result["runs"], [
            {"start": 16500, "stop": 17000, "peak_value": 10.0,
             "peak_index": 16800},
        ])
        # Untruncated: the same run from its true start, earliest tied peak.
        result = self.q(start=15000, stop=18000, threshold=0.0)
        self.assertEqual(result["runs"], [
            {"start": 16000, "stop": 17000, "peak_value": 10.0,
             "peak_index": 16000},
        ])
        # [1000,1100) crosses leaves 3 and 4.
        result = self.q(start=900, stop=1200, threshold=-1.0)
        self.assertEqual(result["runs"], [
            {"start": 1000, "stop": 1100, "peak_value": 0.0,
             "peak_index": 1000},
        ])

    def test_threshold_equality_ge_counts_lt_excludes(self):
        ge = self.q(threshold=10.0)
        self.assertEqual(ge["runs"], [
            {"start": 16000, "stop": 16500, "peak_value": 10.0,
             "peak_index": 16000},
            {"start": 16800, "stop": 16810, "peak_value": 10.0,
             "peak_index": 16800},
            {"start": 19995, "stop": 20000, "peak_value": 20.0,
             "peak_index": 19995},
        ])
        lt = self.q(op="lt", threshold=10.0)
        # The ==10.0 plateaus are excluded: they split the baseline run.
        self.assertEqual(lt["runs"], [
            {"start": 0, "stop": 16000, "peak_value": 1.0,
             "peak_index": 7000},
            {"start": 16500, "stop": 16800, "peak_value": 0.0,
             "peak_index": 16500},
            {"start": 16810, "stop": 19995, "peak_value": 0.0,
             "peak_index": 16810},
        ])

    def test_earliest_tied_peak(self):
        result = self.q(start=0, stop=self.POINTS, threshold=10.0)
        self.assertEqual(result["runs"][0]["peak_index"], 16000)

    def test_pagination_around_k(self):
        # Five runs total for ge 0.0.
        all_five = self.q(threshold=0.0, limit=5)
        self.assertEqual(len(all_five["runs"]), 5)
        self.assertFalse(all_five["has_more"])
        more = self.q(threshold=0.0, limit=7)
        self.assertEqual(len(more["runs"]), 5)
        self.assertFalse(more["has_more"])
        cut = self.q(threshold=0.0, limit=3)
        self.assertEqual(cut["runs"], all_five["runs"][:3])
        self.assertTrue(cut["has_more"])
        one = self.q(threshold=0.0, limit=1)
        self.assertEqual(one["runs"], all_five["runs"][:1])
        self.assertTrue(one["has_more"])
        # The third run must be complete (not cut mid-run) and the fourth
        # must not be merged into it.
        self.assertEqual(cut["runs"][2],
                         {"start": 12000, "stop": 12001,
                          "peak_value": 0.0, "peak_index": 12000})

    def test_negative_threshold(self):
        result = self.q(threshold=-98.0)
        self.assertIn({"start": 0, "stop": 5, "peak_value": -97.0,
                       "peak_index": 0}, result["runs"])
        result = self.q(op="lt", threshold=-98.0)
        runs = result["runs"]
        self.assertTrue(all(run["peak_value"] < -98.0 for run in runs))
        self.assertEqual(runs[0],
                         {"start": 5, "stop": 1000, "peak_value": -100.0,
                          "peak_index": 5})

    def test_first_and_last_point_hits(self):
        result = self.q(start=0, stop=1, threshold=-97.0)
        self.assertEqual(result["runs"], [
            {"start": 0, "stop": 1, "peak_value": -97.0, "peak_index": 0}])
        result = self.q(start=self.POINTS - 1, stop=self.POINTS,
                        threshold=0.0)
        self.assertEqual(result["runs"], [
            {"start": 19999, "stop": 20000, "peak_value": 20.0,
             "peak_index": 19999}])

    def test_all_equal_channel_uses_summaries_only(self):
        result = self.q(channel="aux", threshold=15.0)
        self.assertEqual(result["runs"], [
            {"start": 0, "stop": self.POINTS, "peak_value": 15.0,
             "peak_index": 0}])
        self.assertEqual(result["value_reads"], 0)
        result = self.q(channel="aux", op="lt", threshold=15.0)
        self.assertEqual(result["runs"], [])
        self.assertFalse(result["has_more"])
        self.assertEqual(result["value_reads"], 0)
        result = self.q(channel="aux", threshold=15.5)
        self.assertEqual(result["runs"], [])
        self.assertEqual(result["value_reads"], 0)

    def test_same_channel_name_in_multiple_sources(self):
        one = self.q(source="s1", channel="main", threshold=0.0)
        two = self.q(source="s2", channel="main", threshold=0.0)
        self.assertEqual(len(one["runs"]), 5)
        self.assertEqual(two["runs"], [])
        two = self.q(source="s2", channel="main", threshold=-90.0)
        self.assertEqual(two["runs"], [
            {"start": 0, "stop": self.POINTS, "peak_value": -90.0,
             "peak_index": 0}])
        self.assertEqual(two["capture_id"], "cap-sem")

    def test_range_cutting_into_run_middle(self):
        result = self.q(start=16050, stop=16060, threshold=0.0)
        self.assertEqual(result["runs"], [
            {"start": 16050, "stop": 16060, "peak_value": 10.0,
             "peak_index": 16050}])
        result = self.q(start=16999, stop=17000, threshold=0.0)
        self.assertEqual(result["runs"], [
            {"start": 16999, "stop": 17000, "peak_value": 0.0,
             "peak_index": 16999}])

    def test_unknown_source_and_channel_rejected(self):
        self.query(self.archive, "nosuch", "main", 0, 10, "ge", 0.0, 5,
                   expect=2)
        self.query(self.archive, "s1", "nosuch", 0, 10, "ge", 0.0, 5,
                   expect=2)

    def test_invalid_query_arguments_rejected(self):
        base = ["query", "--archive", self.archive, "--source", "s1",
                "--channel", "main"]
        self.run_cli(*base, "--start", -1, "--stop", 10, "--op", "ge",
                     "--threshold", 0, "--limit", 5, expect=2)
        self.run_cli(*base, "--start", 10, "--stop", 10, "--op", "ge",
                     "--threshold", 0, "--limit", 5, expect=2)
        self.run_cli(*base, "--start", 0, "--stop", self.POINTS + 1,
                     "--op", "ge", "--threshold", 0, "--limit", 5, expect=2)
        self.run_cli(*base, "--start", 0, "--stop", 10, "--op", "ge",
                     "--threshold", 0, "--limit", 0, expect=2)
        self.run_cli(*base, "--start", 0, "--stop", 10, "--op", "ge",
                     "--threshold", 0, "--limit", 10001, expect=2)
        self.run_cli(*base, "--start", 0, "--stop", 10, "--op", "ge",
                     "--threshold", "nan", "--limit", 5, expect=2)
        self.run_cli(*base, "--start", 0, "--stop", 10, "--op", "ge",
                     "--threshold", "inf", "--limit", 5, expect=2)
        self.run_cli(*base, "--start", 0, "--stop", 10, "--op", "gt",
                     "--threshold", 0, "--limit", 5, expect=2)
        self.run_cli(*base, "--start", "x", "--stop", 10, "--op", "ge",
                     "--threshold", 0, "--limit", 5, expect=2)
        self.run_cli(*base, "--start", 0, "--stop", 10, "--op", "ge",
                     "--threshold", 0, "--limit", "2.5", expect=2)


class IndexAcceptance(CliCase):
    """1,048,576-point channel: summary pruning must dominate the counts."""

    POINTS = 1048576

    def setUp(self):
        super().setUp()
        import array as _array
        arr = _array.array("h", bytes(2 * self.POINTS))
        for index in range(500000, 500010):
            arr[index] = 10
        raw = arr.tobytes()
        self.pkg = self.root / "pkg1m"
        self.pkg.mkdir()
        (self.pkg / "raw.bin").write_bytes(raw)
        meta = {"capture_id": "cap-1m", "sample_rate": 1000,
                "points": self.POINTS, "dtype": "int16",
                "byte_order": "little",
                "channels": [{"name": "ch0", "scale": 1.0, "offset": 0.0}],
                "raw_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest()}
        manifest = {
            "schema_version": 1, "source_url": "http://127.0.0.1:9/d",
            "capabilities": {"max_range_bytes": 4096, "lease": False},
            "consistency_channel": "etag", "etag": '"0123abcd"',
            "capture_id": meta["capture_id"],
            "config": {"sample_rate": 1000, "points": self.POINTS,
                       "dtype": "int16", "byte_order": "little"},
            "channels": meta["channels"], "raw_bytes": meta["raw_bytes"],
            "raw_sha256": meta["sha256"],
            "meta_sha256": canonical_meta_sha(meta), "analysis": None,
        }
        (self.pkg / "manifest.json").write_text(json.dumps(manifest))
        sources = self.write_sources(
            [{"source_id": "big", "package": "pkg1m"}])
        self.archive = self.root / "archive1m"
        self.build(sources, self.archive)

    def test_sparse_excursion_reads_one_leaf(self):
        result = self.query(self.archive, "big", "ch0", 0, self.POINTS,
                            "ge", 5, 100)
        self.assertEqual(result["runs"], [
            {"start": 500000, "stop": 500010, "peak_value": 10.0,
             "peak_index": 500000}])
        self.assertFalse(result["has_more"])
        self.assertLessEqual(result["value_reads"], 1024)
        self.assertLessEqual(result["node_reads"], 512)
        self.assertGreater(result["value_reads"], 0)

    def test_full_hit_needs_no_point_values(self):
        result = self.query(self.archive, "big", "ch0", 0, self.POINTS,
                            "ge", 0, 100)
        self.assertEqual(result["runs"], [
            {"start": 0, "stop": self.POINTS, "peak_value": 10.0,
             "peak_index": 500000}])
        self.assertEqual(result["value_reads"], 0)

    def test_full_miss_needs_no_point_values(self):
        result = self.query(self.archive, "big", "ch0", 0, self.POINTS,
                            "lt", 0, 100)
        self.assertEqual(result["runs"], [])
        self.assertFalse(result["has_more"])
        self.assertEqual(result["value_reads"], 0)


class BuildValidation(CliCase):
    def make_package(self, name="pkg", points=64, channels=None,
                     raw_fn=None, dtype="int16", byte_order="little"):
        channels = channels or [("main", 0.5, -1.0)]
        raw_fn = raw_fn or (lambda p, c: (p * 3 + c) % 40)
        pkg = self.root / name
        write_package(pkg, "cap-" + name, 1000, points, dtype, byte_order,
                      channels, raw_fn)
        return pkg

    def build_one(self, pkg_name="pkg", archive_name="archive", expect=0,
                  sources_entries=None):
        entries = sources_entries or [
            {"source_id": "s", "package": pkg_name}]
        sources = self.write_sources(entries)
        return self.build(sources, self.root / archive_name, expect=expect)

    def test_sources_file_violations(self):
        self.make_package()
        bad = [
            {},                                        # missing key
            {"sources": [], "extra": 1},               # extra key
            {"sources": []},                           # too few
            {"sources": [{"source_id": "s", "package": "pkg",
                          "x": 1}]},                   # extra field
            {"sources": [{"source_id": "s"}]},         # missing field
            {"sources": [{"source_id": "", "package": "pkg"}]},
            {"sources": [{"source_id": True, "package": "pkg"}]},
            {"sources": [{"source_id": 7, "package": "pkg"}]},
            {"sources": [{"source_id": "s", "package": ""}]},
            {"sources": [{"source_id": "s", "package": "pkg"},
                         {"source_id": "s", "package": "pkg"}]},  # dup id
            {"sources": [{"source_id": "s", "package": "missing"}]},
            {"sources": [{"source_id": "s%d" % i, "package": "pkg"}
                         for i in range(33)]},         # too many
        ]
        for index, payload in enumerate(bad):
            path = self.root / ("bad%d.json" % index)
            path.write_text(json.dumps(payload))
            self.build(path, self.root / ("arch_bad%d" % index), expect=2)
        # Duplicate JSON key in SOURCES.
        dup = self.root / "dup.json"
        dup.write_text('{"sources": [{"source_id": "s", "package": "pkg",'
                       ' "package": "pkg"}]}')
        self.build(dup, self.root / "arch_dup", expect=2)
        # SOURCES file missing entirely.
        self.build(self.root / "absent.json", self.root / "arch_absent",
                   expect=2)

    def test_manifest_violations(self):
        self.make_package()
        manifest_path = self.root / "pkg" / "manifest.json"
        good = json.loads(manifest_path.read_text())

        def corrupt(mutate, name):
            pkg = self.root / name
            shutil.copytree(self.root / "pkg", pkg)
            target = pkg / "manifest.json"
            manifest = json.loads(target.read_text())
            mutate(manifest)
            target.write_text(json.dumps(manifest))
            sources = self.write_sources(
                [{"source_id": "s", "package": name}], name="src_" + name)
            self.build(sources, self.root / ("arch_" + name), expect=2)

        corrupt(lambda m: m.__setitem__("schema_version", 2), "ver2")
        corrupt(lambda m: m.__setitem__("unexpected", 1), "extra")
        corrupt(lambda m: m.pop("raw_sha256"), "missing")
        corrupt(lambda m: m.__setitem__("raw_bytes", m["raw_bytes"] + 2),
                "rawbytes")
        corrupt(lambda m: m["config"].__setitem__("dtype", "int32"),
                "dtype")
        corrupt(lambda m: m["config"].__setitem__("byte_order", "middle"),
                "order")
        corrupt(lambda m: m["channels"][0].__setitem__("scale", 1e999),
                "nonfinite")
        corrupt(lambda m: m.__setitem__("raw_sha256", "0" * 64), "badsha")
        corrupt(lambda m: m["channels"].append(
            {"name": "main", "scale": 1.0, "offset": 0.0}), "dupchan")

        # Duplicate key and oversized manifest.
        pkg = self.root / "pkgdup"
        shutil.copytree(self.root / "pkg", pkg)
        (pkg / "manifest.json").write_text(
            '{"schema_version": 1, "schema_version": 1}')
        sources = self.write_sources([{"source_id": "s", "package": "pkgdup"}],
                                     name="src_dup")
        self.build(sources, self.root / "arch_mdup", expect=2)

        pkg = self.root / "pkgbig"
        shutil.copytree(self.root / "pkg", pkg)
        big = json.loads((pkg / "manifest.json").read_text())
        big["capture_id"] = "x" * 70000
        (pkg / "manifest.json").write_text(json.dumps(big))
        sources = self.write_sources([{"source_id": "s", "package": "pkgbig"}],
                                     name="src_big")
        self.build(sources, self.root / "arch_mbig", expect=2)

    def test_symlink_and_truncation_rejected(self):
        self.make_package()
        pkg = self.root / "pkg"
        # raw.bin as symlink.
        raw_target = self.root / "raw_elsewhere.bin"
        shutil.copy(pkg / "raw.bin", raw_target)
        os.remove(pkg / "raw.bin")
        os.symlink(raw_target, pkg / "raw.bin")
        self.build_one(expect=2)
        # manifest.json as symlink.
        os.remove(pkg / "raw.bin")
        shutil.copy(raw_target, pkg / "raw.bin")
        manifest_target = self.root / "manifest_elsewhere.json"
        shutil.copy(pkg / "manifest.json", manifest_target)
        os.remove(pkg / "manifest.json")
        os.symlink(manifest_target, pkg / "manifest.json")
        self.build_one(expect=2)
        # Truncated raw.bin.
        os.remove(pkg / "manifest.json")
        shutil.copy(manifest_target, pkg / "manifest.json")
        raw = (pkg / "raw.bin").read_bytes()
        (pkg / "raw.bin").write_bytes(raw[:-2])
        self.build_one(expect=2)
        self.assertFalse((self.root / "archive").exists())

    def test_samples_ndjson_is_not_a_fact_source(self):
        # A package whose samples.ndjson flatly contradicts raw.bin must
        # still archive from raw.bin alone.
        self.make_package()
        pkg = self.root / "pkg"
        (pkg / "samples.ndjson").write_text(
            "\n".join(json.dumps({"index": i, "values": [999999.0]})
                      for i in range(64)) + "\n")
        archive = self.root / "archive"
        self.build_one(archive_name="archive")
        values = naive_decode_channel(pkg, "main")
        result = self.assert_query_matches_naive(
            archive, "s", "main", values, 0, 64, "ge", 0.0, 100)
        self.assertTrue(all(run["peak_value"] < 100.0
                            for run in result["runs"]))

    def test_existing_archive_and_staging_refused(self):
        self.make_package()
        archive = self.root / "archive"
        self.build_one()
        self.assertTrue(archive.is_dir())
        self.build_one(expect=2)  # never overwrites
        staging = self.root / "second.staging"
        staging.mkdir()
        sources = self.write_sources([{"source_id": "s", "package": "pkg"}],
                                     name="src2")
        self.build(sources, self.root / "second", expect=2)

    def test_per_package_and_aggregate_raw_limits(self):
        # Per-package 256 MiB limit: declared raw_bytes just over the cap.
        pkg = self.root / "pkgtoobig"
        pkg.mkdir()
        points = (256 * 1024 * 1024) // 4 + 1
        meta = {"capture_id": "cap-huge", "sample_rate": 1,
                "points": points, "dtype": "float32", "byte_order": "little",
                "channels": [{"name": "m", "scale": 1.0, "offset": 0.0}],
                "raw_bytes": points * 4, "sha256": "0" * 64}
        manifest = {
            "schema_version": 1, "source_url": "http://x",
            "capabilities": {"max_range_bytes": 1, "lease": False},
            "consistency_channel": "etag", "etag": '"ab"',
            "capture_id": "cap-huge",
            "config": {"sample_rate": 1, "points": points,
                       "dtype": "float32", "byte_order": "little"},
            "channels": meta["channels"], "raw_bytes": meta["raw_bytes"],
            "raw_sha256": meta["sha256"],
            "meta_sha256": canonical_meta_sha(meta), "analysis": None,
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))
        (pkg / "raw.bin").write_bytes(b"")
        self.build_one(pkg_name="pkgtoobig", archive_name="arch_huge",
                       expect=2)

        # Aggregate 1 GiB limit: five max-size packages declared.
        entries = []
        for index in range(5):
            name = "pkgagg%d" % index
            pkg = self.root / name
            pkg.mkdir()
            points = (256 * 1024 * 1024) // 4
            meta["points"] = points
            meta["raw_bytes"] = points * 4
            meta["capture_id"] = "cap-agg%d" % index
            manifest["capture_id"] = meta["capture_id"]
            manifest["config"]["points"] = points
            manifest["raw_bytes"] = meta["raw_bytes"]
            manifest["raw_sha256"] = meta["sha256"]
            manifest["meta_sha256"] = canonical_meta_sha(meta)
            (pkg / "manifest.json").write_text(json.dumps(manifest))
            (pkg / "raw.bin").write_bytes(b"")
            entries.append({"source_id": "s%d" % index, "package": name})
        self.build_one(archive_name="arch_agg", expect=2,
                       sources_entries=entries)


class ArchiveCorruption(CliCase):
    def setUp(self):
        super().setUp()
        self.pkg = self.root / "pkg"
        write_package(self.pkg, "cap-c", 1000, 300, "int16", "little",
                      [("main", 1.0, 0.0)], lambda p, c: p % 25)
        self.sources = self.write_sources(
            [{"source_id": "s", "package": "pkg"}])
        self.archive = self.root / "archive"
        self.build(self.sources, self.archive)

    def query_ok(self):
        return self.query(self.archive, "s", "main", 0, 300, "ge", 10, 10)

    def test_missing_and_corrupt_catalog(self):
        self.assertTrue(self.query_ok()["runs"])
        os.remove(self.archive / "catalog.db")
        self.query(self.archive, "s", "main", 0, 300, "ge", 10, 10,
                   expect=2)
        self.query(self.root / "no_such_archive", "s", "main", 0, 300,
                   "ge", 10, 10, expect=2)
        (self.archive / "catalog.db").write_bytes(b"not a sqlite database")
        self.query(self.archive, "s", "main", 0, 300, "ge", 10, 10,
                   expect=2)

    def test_unsupported_archive_version(self):
        import sqlite3
        conn = sqlite3.connect(str(self.archive / "catalog.db"))
        conn.execute("UPDATE meta SET value='2' WHERE key='archive_version'")
        conn.commit()
        conn.close()
        self.query(self.archive, "s", "main", 0, 300, "ge", 10, 10,
                   expect=2)

    def test_missing_and_truncated_snapshot(self):
        values_file = None
        for path in (self.archive / "values").iterdir():
            values_file = path
        os.remove(values_file)
        self.query(self.archive, "s", "main", 0, 300, "ge", 10, 10,
                   expect=2)
        values_file.write_bytes(b"\x00" * 16)
        self.query(self.archive, "s", "main", 0, 300, "ge", 10, 10,
                   expect=2)


class ResourceBound(CliCase):
    """Peak RSS of build and query on a corpus far above the 192 MiB cap.

    Method: the corpus is a single 216 MiB raw package (54,000,000 float32
    samples, larger than the 192 MiB RSS budget); build and query run as
    separate subprocesses and their high-water marks are read from
    ``resource.getrusage(RUSAGE_CHILDREN).ru_maxrss`` (KiB on Linux), a
    running maximum over reaped children, sampled before/after each run.
    """

    POINTS = 54_000_000  # 216 MiB of float32 raw, 432 MiB snapshot

    def test_build_and_query_stay_under_rss_cap(self):
        import array as _array
        pkg = self.root / "pkgbig"
        pkg.mkdir()
        raw_path = pkg / "raw.bin"
        # 54M float32 zeros with a small excursion far into the payload.
        chunk = b"\x00" * (4 * 1_000_000)
        with open(raw_path, "wb") as stream:
            for _ in range(self.POINTS // 1_000_000):
                stream.write(chunk)
        arr = _array.array("f", bytes(4 * 100))
        for index in range(100):
            arr[index] = 5.0
        with open(raw_path, "r+b") as stream:
            stream.seek(30_000_000 * 4)
            stream.write(arr.tobytes())
        raw_bytes = self.POINTS * 4
        sha = hashlib.sha256()
        with open(raw_path, "rb") as stream:
            while True:
                block = stream.read(1 << 20)
                if not block:
                    break
                sha.update(block)
        meta = {"capture_id": "cap-rss", "sample_rate": 1000,
                "points": self.POINTS, "dtype": "float32",
                "byte_order": "little",
                "channels": [{"name": "ch", "scale": 1.0, "offset": 0.0}],
                "raw_bytes": raw_bytes, "sha256": sha.hexdigest()}
        manifest = {
            "schema_version": 1, "source_url": "http://127.0.0.1:9/d",
            "capabilities": {"max_range_bytes": 4096, "lease": False},
            "consistency_channel": "etag", "etag": '"rss"',
            "capture_id": "cap-rss",
            "config": {"sample_rate": 1000, "points": self.POINTS,
                       "dtype": "float32", "byte_order": "little"},
            "channels": meta["channels"], "raw_bytes": raw_bytes,
            "raw_sha256": meta["sha256"],
            "meta_sha256": canonical_meta_sha(meta), "analysis": None,
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))
        sources = self.write_sources(
            [{"source_id": "big", "package": "pkgbig"}])
        archive = self.root / "archivebig"

        before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        self.build(sources, archive)
        after_build = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        result = self.query(archive, "big", "ch", 0, self.POINTS, "ge", 1.0,
                            10)
        after_query = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        print("RSS KiB: before=%d after_build=%d after_query=%d (cap %d)"
              % (before, after_build, after_query, RSS_CAP_KIB))
        self.assertEqual(result["runs"], [
            {"start": 30_000_000, "stop": 30_000_100, "peak_value": 5.0,
             "peak_index": 30_000_000}])
        self.assertLessEqual(result["value_reads"], 256 * 4)
        self.assertLessEqual(after_build, RSS_CAP_KIB)
        self.assertLessEqual(after_query, RSS_CAP_KIB)


if __name__ == "__main__":
    unittest.main()
