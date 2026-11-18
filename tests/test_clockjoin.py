"""End-to-end tests for ``signal-clock-join``.

Covers the contract acceptance scenarios:

* three sources A/B/C with base anchors bounding B-A and C-A in [0,10] ns:
  a bundle requiring B-A=0, C-B=0, C-A=1 is impossible even though each
  relation is individually satisfiable, and its evidence is a real closed
  negative cycle whose weights are rebuilt from the original input by an
  independent checker; a bundle requiring B-A=5, C-B=2 yields the
  conditional offsets A=0, B=5, C=7;
* guaranteed classification with no relations or with a relation the base
  anchors already imply; independence of bundles (an impossible bundle never
  contaminates a feasible one); adding a base anchor tightens the offsets;
* exact-rational non-integer (1/3 ns) boundaries, exact equality, and a
  1/3 ns contradiction (a floating-point solver would get these wrong);
* redundant and reversed anchors, jointly tightening relations, unbounded or
  contradictory base anchors (exit 2, no output), same capture_id under
  different source_id, and array-reorder byte invariance;
* independent small-scale enumeration of the feasible region to cross-check
  whole-bundle feasibility, the componentwise-minimal offset vector and the
  guaranteed/conditional split;
* real published packages (both endiannesses, multi-channel, non-unit
  calibration) with exact event time/value checks, plus rejection of a bad
  raw SHA, a bad meta fingerprint, a non-finite float32 value at an
  unreferenced position, and bad event references, while a previously
  published output keeps its exact bytes;
* a resource bound on a corpus whose total raw bytes significantly exceed the
  192 MiB RSS cap, with the measured peak RSS recorded.
"""
import hashlib
import itertools
import json
import os
import resource
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

MODULE = ["-m", "signal_spectrum.clockjoin.cli"]
WIDTH = {"int16": 2, "uint16": 2, "float32": 4}
CODE = {"int16": "h", "uint16": "H", "float32": "f"}
RSS_CAP_KIB = 192 * 1024


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


def q(num, den=1):
    return {"num": num, "den": den}


def event(event_id, source_id, channel, index):
    return {"event_id": event_id, "source_id": source_id,
            "channel": channel, "index": index}


def ep(source_id, index):
    return {"source_id": source_id, "index": index}


def anchor(anchor_id, left, right, lower, upper):
    return {"anchor_id": anchor_id, "left": left, "right": right,
            "lower_ns": lower, "upper_ns": upper}


def relation(relation_id, left, right, lower, upper):
    return {"relation_id": relation_id, "left": left, "right": right,
            "lower_ns": lower, "upper_ns": upper}


# -- independent reference implementation -----------------------------------

def reference_solve(nodes, constraints):
    """A deliberately simple, independent difference-constraint solver.

    ``constraints`` is a list of ``(u, v, lo, hi)`` Fractions meaning
    ``lo <= x_v - x_u <= hi``.  Returns ``(feasible, offsets)`` where offsets
    is the componentwise-minimal solution with node 0 pinned at zero, computed
    by Bellman-Ford relaxation to a fixed point (a different code path from the
    production Floyd-Warshall / shortest-path solver).
    """
    edges = []
    for u, v, lo, hi in constraints:
        edges.append((u, v, hi))      # x_v <= x_u + hi  -> u->v
        edges.append((v, u, -lo))     # x_u <= x_v - lo  -> v->u
    n = len(nodes)
    index = {name: i for i, name in enumerate(nodes)}
    edges = [(index[u], index[v], w) for u, v, w in edges]
    dist = [Fraction(0)] * n
    converged = False
    for _ in range(n + 1):
        changed = False
        for u, v, w in edges:
            if dist[u] + w < dist[v]:
                dist[v] = dist[u] + w
                changed = True
        if not changed:
            converged = True
            break
    if not converged:
        return False, None
    # y_s = -dist(s, ref) with ref node 0.
    offsets = {nodes[i]: -dist[i] for i in range(n)}
    offsets[nodes[0]] = Fraction(0)
    return True, offsets


def enumerate_region(nodes, constraints, lo_bound, hi_bound, step):
    """Enumerate a grid over the free variables (node 0 fixed at 0).

    For small integer problems with integral bounds the difference-constraint
    polytope has integral vertices, so grid enumeration at ``step`` finds the
    true feasibility, componentwise minimum, and whether the base region
    already implies every relation.
    """
    free = nodes[1:]
    grid = [Fraction(x) for x in
            range(int(lo_bound), int(hi_bound) + 1, int(step))]
    feasible = []
    for combo in itertools.product(grid, repeat=len(free)):
        assignment = {nodes[0]: Fraction(0)}
        assignment.update(dict(zip(free, combo)))
        ok = True
        for u, v, lo, hi in constraints:
            diff = assignment[v] - assignment[u]
            if diff < lo or diff > hi:
                ok = False
                break
        if ok:
            feasible.append(assignment)
    if not feasible:
        return None
    minima = {}
    for node in nodes:
        minima[node] = min(item[node] for item in feasible)
    return minima


def rebuild_cycle_edges(job, bundle_id, cycle, rates):
    """Independently rebuild every cycle edge weight from the raw job input.

    ``rates`` maps each source_id to its sample rate (read from the packages).
    Returns a list of ``(from, to, weight, kind, id, side)`` and asserts that
    each edge corresponds to a real constraint with the correct side and an
    exactly matching weight.
    """
    anchors = {a["anchor_id"]: a for a in job["anchors"]}
    bundle = next(b for b in job["bundles"] if b["bundle_id"] == bundle_id)
    relations = {r["relation_id"]: r for r in bundle["relations"]}
    events = {e["event_id"]: e for e in bundle["events"]}

    def delta(left_sid, left_idx, right_sid, right_idx):
        return (Fraction(10 ** 9 * right_idx, rates[right_sid])
                - Fraction(10 ** 9 * left_idx, rates[left_sid]))

    def side_weight(constraint, left_sid, left_idx, right_sid, right_idx,
                    side):
        d = delta(left_sid, left_idx, right_sid, right_idx)
        lower = Fraction(constraint["lower_ns"]["num"],
                         constraint["lower_ns"]["den"])
        upper = Fraction(constraint["upper_ns"]["num"],
                         constraint["upper_ns"]["den"])
        if side == "upper":
            return left_sid, right_sid, upper - d
        return right_sid, left_sid, d - lower

    rebuilt = []
    for edge in cycle["edges"]:
        if edge["kind"] == "anchor":
            constraint = anchors[edge["id"]]
            left = constraint["left"]
            right = constraint["right"]
            frm, to, weight = side_weight(
                constraint, left["source_id"], left["index"],
                right["source_id"], right["index"], edge["side"])
        else:
            constraint = relations[edge["id"]]
            l_ev = events[constraint["left"]]
            r_ev = events[constraint["right"]]
            frm, to, weight = side_weight(
                constraint, l_ev["source_id"], l_ev["index"],
                r_ev["source_id"], r_ev["index"], edge["side"])
        assert (edge["from"], edge["to"]) == (frm, to), (
            "edge endpoints disagree with the constraint")
        reported = Fraction(edge["weight"]["num"], edge["weight"]["den"])
        assert reported == weight, "edge weight disagrees with the constraint"
        rebuilt.append((frm, to, weight, edge["kind"], edge["id"],
                        edge["side"]))
    return rebuilt


class JoinCase(unittest.TestCase):
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

    def write_job(self, job, name="JOB.json"):
        path = self.root / name
        path.write_text(json.dumps(job, sort_keys=True, indent=2))
        return path

    def join(self, job_path, out_name="out", expect=0):
        proc = self.run_cli("--source", job_path, "--output",
                            self.root / out_name, expect=expect)
        if expect != 0:
            return proc
        out = self.root / out_name
        report = json.loads((out / "report.json").read_text())
        events = [json.loads(line) for line in
                  (out / "events.ndjson").read_text().splitlines()]
        return report, events

    def bundle_map(self, report):
        return {b["bundle_id"]: b for b in report["bundles"]}

    def offsets_map(self, bundle):
        return {entry["source_id"]: Fraction(entry["offset_ns"]["num"],
                                             entry["offset_ns"]["den"])
                for entry in bundle["offsets"]}

    def three_source_job(self):
        """A/B/C at 10 Hz, 8 points each; base bounds B-A and C-A in [0,10]."""
        for name, cid in (("A", "cap-a"), ("B", "cap-b"), ("C", "cap-c")):
            self.write_package(name, cid, 10, 8, "int16", "little",
                               [("main", 0.5, 0.0)],
                               lambda p, c, s=ord(name): (p * 3 + s) % 40)
        return {
            "reference": "A",
            "sources": [
                {"source_id": "A", "package": "A"},
                {"source_id": "B", "package": "B"},
                {"source_id": "C", "package": "C"},
            ],
            "anchors": [
                anchor("ba", ep("A", 0), ep("B", 0), q(0), q(10)),
                anchor("ca", ep("A", 0), ep("C", 0), q(0), q(10)),
            ],
            "bundles": [
                {
                    "bundle_id": "impossible",
                    "events": [event("eA", "A", "main", 0),
                               event("eB", "B", "main", 0),
                               event("eC", "C", "main", 0)],
                    "relations": [
                        relation("r1", "eA", "eB", q(0), q(0)),
                        relation("r2", "eB", "eC", q(0), q(0)),
                        relation("r3", "eA", "eC", q(1), q(1)),
                    ],
                },
                {
                    "bundle_id": "conditional",
                    "events": [event("eA", "A", "main", 0),
                               event("eB", "B", "main", 0),
                               event("eC", "C", "main", 0)],
                    "relations": [
                        relation("r1", "eA", "eB", q(5), q(5)),
                        relation("r2", "eB", "eC", q(2), q(2)),
                    ],
                },
                {
                    "bundle_id": "empty",
                    "events": [event("eA", "A", "main", 0)],
                    "relations": [],
                },
            ],
        }


class AcceptanceScenario(JoinCase):
    def test_impossible_cycle_conditional_and_guaranteed(self):
        job = self.three_source_job()
        path = self.write_job(job)
        report, events = self.join(path)
        bundles = self.bundle_map(report)

        bad = bundles["impossible"]
        self.assertEqual(bad["status"], "impossible")
        self.assertIsNone(bad["offsets"])
        self.assertIsNotNone(bad["cycle"])
        cycle = bad["cycle"]
        self.assertEqual(cycle["total_weight"],
                         {"num": -1, "den": 1})
        self.assertLess(cycle["total_weight"]["num"], 0)
        # Endpoints chain into a closed loop and each edge is a real
        # constraint rebuilt from the raw input.
        rates = {"A": 10, "B": 10, "C": 10}
        rebuilt = rebuild_cycle_edges(job, "impossible", cycle, rates)
        for position, (frm, to, weight, kind, ident, side) in enumerate(
                rebuilt):
            nxt = rebuilt[(position + 1) % len(rebuilt)]
            self.assertEqual(to, nxt[0], "cycle edges must chain")
        self.assertEqual(sum(weight for *_, weight, _, _, _ in rebuilt),
                         Fraction(-1))

        good = bundles["conditional"]
        self.assertEqual(good["status"], "conditional")
        self.assertIsNone(good["cycle"])
        self.assertEqual(self.offsets_map(good),
                         {"A": Fraction(0), "B": Fraction(5),
                          "C": Fraction(7)})

        empty = bundles["empty"]
        self.assertEqual(empty["status"], "guaranteed")
        self.assertEqual(self.offsets_map(empty),
                         {"A": Fraction(0), "B": Fraction(0),
                          "C": Fraction(0)})

        # Only feasible bundles contribute event rows, ordered by
        # (bundle_id, event_id).
        keys = [(e["bundle_id"], e["event_id"]) for e in events]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual({e["bundle_id"] for e in events},
                         {"conditional", "empty"})

    def test_base_anchor_addition_keeps_independence(self):
        job = self.three_source_job()
        job["anchors"].append(anchor("ba5", ep("A", 0), ep("B", 0),
                                     q(5), q(5)))
        path = self.write_job(job, "JOB2.json")
        report, _ = self.join(path, out_name="out2")
        bundles = self.bundle_map(report)
        # The conditional bundle still resolves to B=5, C=7.
        self.assertEqual(self.offsets_map(bundles["conditional"]),
                         {"A": Fraction(0), "B": Fraction(5),
                          "C": Fraction(7)})
        # The impossible bundle stays impossible and uncontaminated.
        self.assertEqual(bundles["impossible"]["status"], "impossible")

    def test_guaranteed_when_relation_is_implied(self):
        job = self.three_source_job()
        job["bundles"] = [{
            "bundle_id": "implied",
            "events": [event("eA", "A", "main", 0),
                       event("eB", "B", "main", 0)],
            "relations": [relation("r", "eA", "eB", q(0), q(10))],
        }]
        path = self.write_job(job, "JOB3.json")
        report, events = self.join(path, out_name="out3")
        bundle = report["bundles"][0]
        self.assertEqual(bundle["status"], "guaranteed")
        self.assertIsNone(bundle["cycle"])
        self.assertEqual(self.offsets_map(bundle),
                         {"A": Fraction(0), "B": Fraction(0),
                          "C": Fraction(0)})
        self.assertEqual(len(events), 2)

    def test_event_time_and_value_exact(self):
        job = self.three_source_job()
        # Require B to be at sample 3 of B relative to A sample 1.
        job["bundles"] = [{
            "bundle_id": "times",
            "events": [event("eA", "A", "main", 1),
                       event("eB", "B", "main", 3)],
            "relations": [relation("r", "eA", "eB",
                                   q(2 * 10 ** 8 + 5), q(2 * 10 ** 8 + 5))],
        }]
        path = self.write_job(job, "JOB4.json")
        # A rate 10 -> sample 1 = 10^8 ns; B sample 3 = 3*10^8 ns.
        # x_B = 5; time_B = 5 + 3*10^8.
        report, events = self.join(path, out_name="out4")
        by_id = {e["event_id"]: e for e in events}
        self.assertEqual(Fraction(by_id["eA"]["time_ns"]["num"],
                                  by_id["eA"]["time_ns"]["den"]),
                         Fraction(10 ** 8))
        self.assertEqual(Fraction(by_id["eB"]["time_ns"]["num"],
                                  by_id["eB"]["time_ns"]["den"]),
                         Fraction(5) + Fraction(3 * 10 ** 8))
        # Values come straight from the calibrated raw samples.
        a_values = naive_decode_channel(self.root / "A", "main")
        b_values = naive_decode_channel(self.root / "B", "main")
        self.assertEqual(by_id["eA"]["value"], a_values[1])
        self.assertEqual(by_id["eB"]["value"], b_values[3])


class RationalBoundaries(JoinCase):
    def test_third_nanosecond_non_integer_delta(self):
        # Left source at 1 Hz index L, right at 3 Hz index 1: delta is a
        # third of a nanosecond boundary that floats cannot represent exactly.
        # delta = 10^9*(1/3 - L/1). Choose L=0 -> 10^9/3.
        self.write_package("L", "cap-l", 1, 8, "int16", "little",
                           [("m", 1.0, 0.0)], lambda p, c: p)
        self.write_package("R", "cap-r", 3, 8, "int16", "little",
                           [("m", 1.0, 0.0)], lambda p, c: p)
        delta = Fraction(10 ** 9, 3)  # T(R,1) - T(L,0) with zero offsets
        # Pin x_R = x_L exactly, so the relation on (L,0)/(R,1) is fixed by
        # the base anchors; a 1/3 ns off-by-one is then a genuine
        # contradiction whose evidence carries a denominator of 3.
        base = [anchor("base", ep("L", 0), ep("R", 0), q(0), q(0))]
        job = {
            "reference": "L",
            "sources": [{"source_id": "L", "package": "L"},
                        {"source_id": "R", "package": "R"}],
            "anchors": base,
            "bundles": [
                {"bundle_id": "exact",
                 "events": [event("eL", "L", "m", 0),
                            event("eR", "R", "m", 1)],
                 "relations": [relation("r", "eL", "eR",
                                        q(delta.numerator, delta.denominator),
                                        q(delta.numerator, delta.denominator))]},
                {"bundle_id": "plus_third",
                 "events": [event("eL", "L", "m", 0),
                            event("eR", "R", "m", 1)],
                 "relations": [relation(
                     "r", "eL", "eR",
                     q(delta.numerator + 1, delta.denominator),
                     q(delta.numerator + 1, delta.denominator))]},
            ],
        }
        path = self.write_job(job, "JOB_THIRD.json")
        report, events = self.join(path, out_name="out_third")
        bundles = self.bundle_map(report)
        # x_R - x_L must equal exactly delta - delta = 0 for "exact".
        self.assertEqual(bundles["exact"]["status"], "guaranteed")
        self.assertEqual(self.offsets_map(bundles["exact"])["R"], Fraction(0))
        # A 1/3 ns shift is inconsistent with the base difference of 0.
        self.assertEqual(bundles["plus_third"]["status"], "impossible")
        cycle = bundles["plus_third"]["cycle"]
        self.assertLess(cycle["total_weight"]["num"], 0)
        # total weight has denominator 3 (a genuine rational contradiction).
        self.assertEqual(cycle["total_weight"]["den"] % 3, 0)
        rebuilt = rebuild_cycle_edges(job, "plus_third", cycle,
                                      {"L": 1, "R": 3})
        self.assertEqual(sum(weight for *_, weight, _, _, _ in rebuilt),
                         Fraction(cycle["total_weight"]["num"],
                                  cycle["total_weight"]["den"]))

    def test_exact_equality_is_feasible(self):
        # Closed interval equality at the boundary must be feasible.
        self.write_package("A", "cap-a", 10, 8, "int16", "little",
                           [("m", 1.0, 0.0)], lambda p, c: p)
        self.write_package("B", "cap-b", 10, 8, "int16", "little",
                           [("m", 1.0, 0.0)], lambda p, c: p)
        job = {
            "reference": "A",
            "sources": [{"source_id": "A", "package": "A"},
                        {"source_id": "B", "package": "B"}],
            "anchors": [anchor("ab", ep("A", 0), ep("B", 0), q(5), q(5))],
            "bundles": [{
                "bundle_id": "eq",
                "events": [event("eA", "A", "m", 0),
                           event("eB", "B", "m", 0)],
                "relations": [relation("r", "eA", "eB", q(5), q(5))],
            }],
        }
        path = self.write_job(job, "JOB_EQ.json")
        report, _ = self.join(path, out_name="out_eq")
        bundle = report["bundles"][0]
        self.assertEqual(bundle["status"], "guaranteed")
        self.assertEqual(self.offsets_map(bundle),
                         {"A": Fraction(0), "B": Fraction(5)})


class StructuralCases(JoinCase):
    def _two_source_job(self, anchors, bundles):
        for name in ("A", "B", "C"):
            self.write_package(name, "cap-" + name, 10, 8, "int16",
                               "little", [("m", 1.0, 0.0)],
                               lambda p, c: p)
        return {"reference": "A",
                "sources": [{"source_id": "A", "package": "A"},
                            {"source_id": "B", "package": "B"},
                            {"source_id": "C", "package": "C"}],
                "anchors": anchors, "bundles": bundles}

    def test_redundant_and_reversed_anchors(self):
        anchors = [
            anchor("ab", ep("A", 0), ep("B", 0), q(2), q(8)),
            anchor("ab_rev", ep("B", 0), ep("A", 0), q(-8), q(-2)),
            anchor("ba_dup", ep("A", 0), ep("B", 0), q(0), q(10)),
            anchor("ac", ep("A", 0), ep("C", 0), q(0), q(10)),
        ]
        bundles = [{
            "bundle_id": "rev",
            "events": [event("eA", "A", "m", 0),
                       event("eB", "B", "m", 0)],
            "relations": [relation("r", "eB", "eA", q(-8), q(-2))],
        }]
        job = self._two_source_job(anchors, bundles)
        path = self.write_job(job, "JOB_REV.json")
        report, _ = self.join(path, out_name="out_rev")
        bundle = report["bundles"][0]
        self.assertEqual(bundle["status"], "guaranteed")
        # Base bounds: B-A in [2,8] (tightest), C-A in [0,10]. Minimal B = 2.
        self.assertEqual(self.offsets_map(bundle),
                         {"A": Fraction(0), "B": Fraction(2),
                          "C": Fraction(0)})

    def test_joint_tightening(self):
        anchors = [anchor("ab", ep("A", 0), ep("B", 0), q(0), q(10)),
                   anchor("bc", ep("B", 0), ep("C", 0), q(0), q(10)),
                   anchor("ac", ep("A", 0), ep("C", 0), q(0), q(10))]
        bundles = [{
            "bundle_id": "tight",
            "events": [event("eA", "A", "m", 0),
                       event("eB", "B", "m", 0),
                       event("eC", "C", "m", 0)],
            "relations": [
                relation("r1", "eA", "eB", q(7), q(10)),
                relation("r2", "eC", "eB", q(0), q(0)),
            ],
        }]
        job = self._two_source_job(anchors, bundles)
        path = self.write_job(job, "JOB_TIGHT.json")
        report, _ = self.join(path, out_name="out_tight")
        bundle = report["bundles"][0]
        # B-A in [7,10], C=B, C-A in [0,10] -> B in [7,10], C in [7,10].
        # Componentwise minimum: A=0, B=7, C=7.
        self.assertEqual(bundle["status"], "conditional")
        self.assertEqual(self.offsets_map(bundle),
                         {"A": Fraction(0), "B": Fraction(7),
                          "C": Fraction(7)})

    def test_unbounded_base_rejected(self):
        # C never connected to the reference -> unbounded, exit 2, no output.
        anchors = [anchor("ab", ep("A", 0), ep("B", 0), q(0), q(10))]
        bundles = [{"bundle_id": "b",
                    "events": [event("eA", "A", "m", 0),
                               event("eB", "B", "m", 0)],
                    "relations": []}]
        job = self._two_source_job(anchors, bundles)
        path = self.write_job(job, "JOB_UNBOUND.json")
        self.join(path, out_name="out_unbound", expect=2)
        self.assertFalse((self.root / "out_unbound").exists())

    def test_contradictory_base_rejected(self):
        anchors = [anchor("ab", ep("A", 0), ep("B", 0), q(10), q(10)),
                   anchor("ab2", ep("A", 0), ep("B", 0), q(20), q(20)),
                   anchor("ac", ep("A", 0), ep("C", 0), q(0), q(10))]
        bundles = [{"bundle_id": "b",
                    "events": [event("eA", "A", "m", 0)],
                    "relations": []}]
        job = self._two_source_job(anchors, bundles)
        path = self.write_job(job, "JOB_CONTRA.json")
        self.join(path, out_name="out_contra", expect=2)
        self.assertFalse((self.root / "out_contra").exists())

    def test_same_capture_different_source(self):
        # Two source_ids sharing one capture_id (and even one package).
        self.write_package("A", "cap-shared", 10, 8, "int16", "little",
                           [("m", 1.0, 0.0)], lambda p, c: p)
        self.write_package("B", "cap-shared", 10, 8, "int16", "little",
                           [("m", 1.0, 0.0)], lambda p, c: p + 100)
        job = {
            "reference": "A",
            "sources": [{"source_id": "A", "package": "A"},
                        {"source_id": "B", "package": "B"},
                        {"source_id": "B_copy", "package": "B"}],
            "anchors": [anchor("ab", ep("A", 0), ep("B", 0), q(0), q(10)),
                        anchor("ac", ep("A", 0), ep("B_copy", 0),
                               q(0), q(10))],
            "bundles": [{
                "bundle_id": "b",
                "events": [event("eA", "A", "m", 0),
                           event("eB", "B", "m", 0),
                           event("eC", "B_copy", "m", 0)],
                "relations": [relation("r1", "eA", "eB", q(3), q(3)),
                              relation("r2", "eA", "eC", q(7), q(7))],
            }],
        }
        path = self.write_job(job, "JOB_SHARED.json")
        report, _ = self.join(path, out_name="out_shared")
        # B and B_copy share capture_id but keep independent clocks.
        captures = {s["source_id"]: s["capture_id"]
                    for s in report["sources"]}
        self.assertEqual(captures["B"], captures["B_copy"])
        self.assertEqual(self.offsets_map(report["bundles"][0]),
                         {"A": Fraction(0), "B": Fraction(3),
                          "B_copy": Fraction(7)})

    def test_array_reorder_byte_invariance(self):
        job = self.three_source_job()
        first = self.write_job(job, "JOB_ORDER1.json")
        self.join(first, out_name="out_order1")
        shuffled = json.loads(json.dumps(job))
        shuffled["sources"] = list(reversed(shuffled["sources"]))
        shuffled["anchors"] = list(reversed(shuffled["anchors"]))
        shuffled["bundles"] = list(reversed(shuffled["bundles"]))
        for bundle in shuffled["bundles"]:
            bundle["events"] = list(reversed(bundle["events"]))
            bundle["relations"] = list(reversed(bundle["relations"]))
        second = self.write_job(shuffled, "JOB_ORDER2.json")
        self.join(second, out_name="out_order2")
        for name in ("report.json", "events.ndjson"):
            self.assertEqual((self.root / "out_order1" / name).read_bytes(),
                             (self.root / "out_order2" / name).read_bytes())


class SameSourceAndReference(JoinCase):
    def test_same_source_relation_feasible_and_infeasible(self):
        for name in ("A", "B"):
            self.write_package(name, "cap-" + name, 10, 8, "float32",
                               "little", [("m", 1.0, 0.0)],
                               lambda p, c: 1.0)
        # A(2) -> A(0) has fixed delta -2e8 ns. A relation of exactly that
        # value is guaranteed; one off by 1 ns is impossible with a
        # single-edge self-loop negative cycle.
        job = {
            "reference": "A",
            "sources": [{"source_id": "A", "package": "A"},
                        {"source_id": "B", "package": "B"}],
            "anchors": [anchor("ab", ep("A", 0), ep("B", 0), q(0), q(10))],
            "bundles": [
                {"bundle_id": "same_ok",
                 "events": [event("e0", "A", "m", 0),
                            event("e2", "A", "m", 2)],
                 "relations": [relation("r", "e2", "e0",
                                        q(-2 * 10 ** 8),
                                        q(-2 * 10 ** 8))]},
                {"bundle_id": "same_bad",
                 "events": [event("e0", "A", "m", 0),
                            event("e2", "A", "m", 2)],
                 "relations": [relation("r", "e2", "e0",
                                        q(-2 * 10 ** 8 + 1),
                                        q(-2 * 10 ** 8 + 1))]},
            ],
        }
        path = self.write_job(job, "JOB_SAME.json")
        report, events = self.join(path, out_name="out_same")
        bundles = self.bundle_map(report)
        self.assertEqual(bundles["same_ok"]["status"], "guaranteed")
        self.assertEqual(bundles["same_bad"]["status"], "impossible")
        cycle = bundles["same_bad"]["cycle"]
        self.assertEqual(Fraction(cycle["total_weight"]["num"],
                                  cycle["total_weight"]["den"]), Fraction(-1))
        for edge in cycle["edges"]:
            self.assertEqual(edge["from"], edge["to"])

    def test_reference_not_first_in_sorted_order(self):
        # reference "B" sorts in the middle; x_B must still be exactly 0 and
        # the other offsets are the componentwise minimum around it.
        for name in ("A", "B", "C"):
            self.write_package(name, "cap-" + name, 10, 8, "int16",
                               "little", [("m", 1.0, 0.0)],
                               lambda p, c: p)
        job = {
            "reference": "B",
            "sources": [{"source_id": s, "package": s}
                        for s in ("A", "B", "C")],
            "anchors": [
                anchor("ab", ep("A", 0), ep("B", 0), q(0), q(10)),
                anchor("bc", ep("B", 0), ep("C", 0), q(0), q(10)),
            ],
            "bundles": [{"bundle_id": "b",
                         "events": [event("e", "B", "m", 0)],
                         "relations": []}],
        }
        path = self.write_job(job, "JOB_REFMID.json")
        report, _ = self.join(path, out_name="out_refmid")
        offsets = self.offsets_map(report["bundles"][0])
        self.assertEqual(offsets["B"], Fraction(0))
        # x_A - x_B in [-10, 0] -> minimal A = -10; C - B in [0,10] -> C = 0.
        self.assertEqual(offsets["A"], Fraction(-10))
        self.assertEqual(offsets["C"], Fraction(0))


class IndependentEnumeration(JoinCase):
    """Cross-check the solver against brute-force grid enumeration."""

    def _build(self, rates):
        job_sources = []
        for name, rate in rates.items():
            self.write_package(name, "cap-" + name, rate, 8, "int16",
                               "little", [("m", 1.0, 0.0)],
                               lambda p, c: p)
            job_sources.append({"source_id": name, "package": name})
        return job_sources

    def test_feasibility_minimum_and_guaranteed(self):
        rates = {"A": 10, "B": 10, "C": 10}
        sources = self._build(rates)
        anchors = [
            anchor("ab", ep("A", 0), ep("B", 0), q(0), q(10)),
            anchor("ca", ep("A", 0), ep("C", 0), q(0), q(10)),
        ]
        bundles = [
            {"bundle_id": "feasible",
             "events": [event("eA", "A", "m", 0),
                        event("eB", "B", "m", 0),
                        event("eC", "C", "m", 0)],
             "relations": [relation("r1", "eA", "eB", q(2), q(6)),
                           relation("r2", "eB", "eC", q(1), q(4))]},
            {"bundle_id": "infeasible",
             "events": [event("eA", "A", "m", 0),
                        event("eB", "B", "m", 0)],
             "relations": [relation("r1", "eA", "eB", q(0), q(1)),
                           relation("r2", "eA", "eB", q(5), q(9))]},
            {"bundle_id": "guaranteed",
             "events": [event("eA", "A", "m", 0),
                        event("eB", "B", "m", 0)],
             "relations": [relation("r1", "eA", "eB", q(0), q(10))]},
        ]
        job = {"reference": "A", "sources": sources, "anchors": anchors,
               "bundles": bundles}
        path = self.write_job(job, "JOB_ENUM.json")
        report, _ = self.join(path, out_name="out_enum")
        bundles_out = self.bundle_map(report)

        nodes = ["A", "B", "C"]

        def constraints_for(relations):
            cons = []
            for a in anchors:
                cons.append((a["left"]["source_id"], a["right"]["source_id"],
                             Fraction(a["lower_ns"]["num"],
                                      a["lower_ns"]["den"]),
                             Fraction(a["upper_ns"]["num"],
                                      a["upper_ns"]["den"])))
            for r in relations:
                cons.append((r["left"].replace("e", ""), r["right"].replace(
                    "e", ""),
                    Fraction(r["lower_ns"]["num"], r["lower_ns"]["den"]),
                    Fraction(r["upper_ns"]["num"], r["upper_ns"]["den"])))
            return cons

        # Feasible bundle: enumeration minimum matches solver offsets, and
        # some feasible base point violates a relation -> conditional.
        rels = bundles[0]["relations"]
        cons = constraints_for(rels)
        self.assertTrue(reference_solve(nodes, cons)[0])
        minima = enumerate_region(nodes, cons, -2, 12, 1)
        solver_offsets = self.offsets_map(bundles_out["feasible"])
        self.assertEqual({n: minima[n] for n in nodes},
                         {n: solver_offsets[n] for n in nodes})
        base_cons = constraints_for([])
        base_minima = enumerate_region(nodes, base_cons, -2, 12, 1)
        violated = False
        for combo in itertools.product(
                [Fraction(x) for x in range(-2, 13)],
                repeat=2):
            assign = {"A": Fraction(0), "B": combo[0], "C": combo[1]}
            if any(assign[v] - assign[u] < lo or assign[v] - assign[u] > hi
                   for u, v, lo, hi in base_cons):
                continue
            if any(assign[v] - assign[u] < lo or assign[v] - assign[u] > hi
                   for u, v, lo, hi in cons):
                violated = True
                break
        self.assertTrue(violated)
        self.assertEqual(bundles_out["feasible"]["status"], "conditional")
        self.assertEqual({n: base_minima[n] for n in nodes},
                         {"A": Fraction(0), "B": Fraction(0),
                          "C": Fraction(0)})

        # Infeasible bundle: independent enumeration finds no feasible point.
        infeas_cons = constraints_for(bundles[1]["relations"])
        self.assertIsNone(enumerate_region(nodes, infeas_cons, -2, 12, 1))
        self.assertFalse(reference_solve(nodes, infeas_cons)[0])
        self.assertEqual(bundles_out["infeasible"]["status"], "impossible")

        # Guaranteed bundle: every base-feasible grid point satisfies it.
        guar_cons = constraints_for(bundles[2]["relations"])
        all_ok = True
        for combo in itertools.product(
                [Fraction(x) for x in range(-2, 13)], repeat=2):
            assign = {"A": Fraction(0), "B": combo[0], "C": combo[1]}
            if any(assign[v] - assign[u] < lo or assign[v] - assign[u] > hi
                   for u, v, lo, hi in base_cons):
                continue
            if any(assign[v] - assign[u] < lo or assign[v] - assign[u] > hi
                   for u, v, lo, hi in guar_cons):
                all_ok = False
                break
        self.assertTrue(all_ok)
        self.assertEqual(bundles_out["guaranteed"]["status"], "guaranteed")


class PackageVerification(JoinCase):
    def _packages(self):
        self.write_package("A", "cap-a", 20, 16, "int16", "little",
                           [("main", 0.5, -1.0), ("aux", 2.0, 3.0)],
                           lambda p, c: (p * 3 + c * 7) % 40 - 20)
        self.write_package("B", "cap-b", 20, 16, "float32", "big",
                           [("main", 0.25, 1.5)],
                           lambda p, c: ((p * 11) % 17) - 8.0)
        self.write_package("C", "cap-c", 20, 16, "uint16", "little",
                           [("ch", 0.125, 0.0)],
                           lambda p, c: (p * 5) % 100)

    def _job(self, bundles=None):
        return {
            "reference": "A",
            "sources": [{"source_id": "A", "package": "A"},
                        {"source_id": "B", "package": "B"},
                        {"source_id": "C", "package": "C"}],
            "anchors": [anchor("ab", ep("A", 0), ep("B", 0), q(0), q(10)),
                        anchor("ac", ep("A", 0), ep("C", 0), q(0), q(10))],
            "bundles": bundles or [{
                "bundle_id": "b",
                "events": [event("eA", "A", "main", 3),
                           event("eB", "B", "main", 5),
                           event("eC", "C", "ch", 7)],
                "relations": [
                    relation("r1", "eA", "eB", q(10 ** 8 + 4),
                             q(10 ** 8 + 4)),
                    relation("r2", "eA", "eC", q(2 * 10 ** 8 + 9),
                             q(2 * 10 ** 8 + 9))],
            }],
        }

    def test_event_times_and_values_exact(self):
        self._packages()
        path = self.write_job(self._job(), "JOB_PKG.json")
        report, events = self.join(path, out_name="out_pkg")
        by_id = {e["event_id"]: e for e in events}
        # A rate 20: sample 3 = 3*10^9/20 = 1.5e8 ns; x_A = 0.
        self.assertEqual(Fraction(by_id["eA"]["time_ns"]["num"],
                                  by_id["eA"]["time_ns"]["den"]),
                         Fraction(3 * 10 ** 9, 20))
        # x_B = 4, sample 5 -> 5*10^9/20 = 2.5e8.
        self.assertEqual(Fraction(by_id["eB"]["time_ns"]["num"],
                                  by_id["eB"]["time_ns"]["den"]),
                         Fraction(4) + Fraction(5 * 10 ** 9, 20))
        # x_C = 9, sample 7 -> 7*10^9/20 = 3.5e8.
        self.assertEqual(Fraction(by_id["eC"]["time_ns"]["num"],
                                  by_id["eC"]["time_ns"]["den"]),
                         Fraction(9) + Fraction(7 * 10 ** 9, 20))
        for key, pkg, channel, index in (
                ("eA", "A", "main", 3), ("eB", "B", "main", 5),
                ("eC", "C", "ch", 7)):
            values = naive_decode_channel(self.root / pkg, channel)
            self.assertEqual(by_id[key]["value"], values[index])
        # Report evidence hashes match the real files.
        for source in report["sources"]:
            pkg = self.root / source["source_id"]
            self.assertEqual(source["raw_sha256"], hashlib.sha256(
                (pkg / "raw.bin").read_bytes()).hexdigest())
            self.assertEqual(source["manifest_sha256"], hashlib.sha256(
                (pkg / "manifest.json").read_bytes()).hexdigest())

    def _assert_rejected_preserving_output(self):
        # First publish a good output, then a failing run must not alter it.
        self._packages()
        good = self.write_job(self._job(), "JOB_GOOD.json")
        self.join(good, out_name="keep")
        before = {name: (self.root / "keep" / name).read_bytes()
                  for name in ("report.json", "events.ndjson")}
        return before

    def test_bad_raw_sha_rejected(self):
        before = self._assert_rejected_preserving_output()
        raw = self.root / "B" / "raw.bin"
        data = bytearray(raw.read_bytes())
        data[3] ^= 0xFF
        raw.write_bytes(bytes(data))
        bad = self.write_job(self._job(), "JOB_BADSHA.json")
        self.join(bad, out_name="keep", expect=2)
        for name, payload in before.items():
            self.assertEqual((self.root / "keep" / name).read_bytes(), payload)

    def test_bad_meta_fingerprint_rejected(self):
        before = self._assert_rejected_preserving_output()
        manifest_path = self.root / "B" / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["config"]["sample_rate"] = 21  # fingerprint now stale
        manifest_path.write_text(json.dumps(manifest, sort_keys=True,
                                            indent=2))
        bad = self.write_job(self._job(), "JOB_BADMETA.json")
        self.join(bad, out_name="keep", expect=2)
        for name, payload in before.items():
            self.assertEqual((self.root / "keep" / name).read_bytes(), payload)

    def test_nonfinite_unreferenced_float_rejected(self):
        before = self._assert_rejected_preserving_output()
        # Replace A with a float32 package and inject a genuine NaN raw value
        # into its *aux* channel at an index no event references. The whole
        # calibrated value set (not just referenced samples) must be finite,
        # and the manifest is re-fingerprinted so the rejection is about
        # finiteness, not the raw SHA.
        self.write_package("A", "cap-a", 20, 16, "float32", "little",
                           [("main", 1.0, 0.0), ("aux", 1.0, 0.0)],
                           lambda p, c: 1.0)
        raw_path = self.root / "A" / "raw.bin"
        raw = bytearray(raw_path.read_bytes())
        frame = 4 * 2
        unreferenced_index = 11
        offset = unreferenced_index * frame + 1 * 4  # aux channel float32
        raw[offset:offset + 4] = struct.pack("<f", float("nan"))
        raw_path.write_bytes(bytes(raw))
        manifest_path = self.root / "A" / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        meta = {
            "capture_id": manifest["capture_id"],
            "sample_rate": manifest["config"]["sample_rate"],
            "points": manifest["config"]["points"],
            "dtype": manifest["config"]["dtype"],
            "byte_order": manifest["config"]["byte_order"],
            "channels": manifest["channels"],
            "raw_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        manifest["raw_bytes"] = meta["raw_bytes"]
        manifest["raw_sha256"] = meta["sha256"]
        manifest["meta_sha256"] = canonical_meta_sha(meta)
        manifest_path.write_text(json.dumps(manifest, sort_keys=True,
                                            indent=2))
        bad = self.write_job(self._job(), "JOB_NAN.json")
        self.join(bad, out_name="keep", expect=2)
        for name, payload in before.items():
            self.assertEqual((self.root / "keep" / name).read_bytes(), payload)

    def test_bad_event_reference_rejected(self):
        before = self._assert_rejected_preserving_output()
        job = self._job(bundles=[{
            "bundle_id": "bad",
            "events": [event("eA", "A", "main", 999)],
            "relations": [],
        }])
        bad = self.write_job(job, "JOB_BADEV.json")
        self.join(bad, out_name="keep", expect=2)
        for name, payload in before.items():
            self.assertEqual((self.root / "keep" / name).read_bytes(), payload)

    def test_bad_anchor_reference_rejected_even_if_bundle_impossible(self):
        before = self._assert_rejected_preserving_output()
        job = self._job(bundles=[{
            "bundle_id": "impossible",
            "events": [event("eA", "A", "main", 0),
                       event("eB", "B", "main", 0)],
            "relations": [relation("r1", "eA", "eB", q(0), q(0)),
                          relation("r2", "eA", "eB", q(100), q(100))],
        }])
        job["anchors"].append(anchor("bad", ep("A", 0), ep("B", 999),
                                     q(0), q(1)))
        bad = self.write_job(job, "JOB_BADANCHOR.json")
        self.join(bad, out_name="keep", expect=2)
        for name, payload in before.items():
            self.assertEqual((self.root / "keep" / name).read_bytes(), payload)

    def test_existing_output_directory_refused(self):
        self._packages()
        path = self.write_job(self._job(), "JOB_EXISTS.json")
        self.join(path, out_name="taken")
        before = {name: (self.root / "taken" / name).read_bytes()
                  for name in ("report.json", "events.ndjson")}
        self.join(path, out_name="taken", expect=2)
        for name, payload in before.items():
            self.assertEqual((self.root / "taken" / name).read_bytes(), payload)

    def test_output_inside_package_rejected(self):
        self._packages()
        path = self.write_job(self._job(), "JOB_INSIDE.json")
        self.run_cli("--source", path, "--output", self.root / "A" / "sub",
                     expect=2)
        self.assertFalse((self.root / "A" / "sub").exists())


class RealFetchedPackages(JoinCase):
    """End-to-end across really fetched packages, cross-checked with the
    baseline excursions archive values."""

    def setUp(self):
        super().setUp()
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.stop()
        super().tearDown()

    def fetch(self, capture, name):
        from signal_spectrum.waveform.collector import (
            build_config, run_acquisition)
        from signal_spectrum.waveform.device import DeviceServer
        from signal_spectrum.waveform.store import Staging
        server = DeviceServer(capture, max_range_bytes=97)
        server.start()
        self.servers.append(server)
        out = self.root / name
        run_acquisition(build_config(server.url()), Staging(out))
        return out

    def test_fetched_packages_join_and_match_archive_values(self):
        from signal_spectrum.waveform.device import build_capture
        from signal_spectrum.excursions.cli import main as ex_main
        cap_a = build_capture("cap-multi", 500, 32, "int16", "little",
                              [("main", 0.001, 0.0), ("aux", 0.5, 2.0)],
                              lambda p, c: ((p * 7 + c * 13) % 200) - 100)
        cap_b = build_capture("cap-multi", 1000, 40, "float32", "big",
                              [("main", 0.25, -3.0)],
                              lambda p, c: ((p * 11) % 97) - 48.5)
        cap_c = build_capture("cap-c", 600, 36, "uint16", "little",
                              [("u", 0.125, -1.0)],
                              lambda p, c: (p * 37) % 5000)
        pkg_a = self.fetch(cap_a, "pkg_a")
        pkg_b = self.fetch(cap_b, "pkg_b")
        pkg_c = self.fetch(cap_c, "pkg_c")

        job = {
            "reference": "alpha",
            "sources": [
                {"source_id": "alpha", "package": "pkg_a"},
                {"source_id": "beta", "package": "pkg_b"},
                {"source_id": "gamma", "package": "pkg_c"},
            ],
            "anchors": [
                anchor("ba", ep("alpha", 0), ep("beta", 0), q(0), q(10)),
                anchor("ca", ep("alpha", 0), ep("gamma", 0), q(0), q(10)),
            ],
            "bundles": [{
                "bundle_id": "b",
                "events": [event("eA", "alpha", "main", 5),
                           event("eB", "beta", "main", 7),
                           event("eC", "gamma", "u", 9)],
                "relations": [
                    # deltas computed below from the real rates.
                    relation("r1", "eA", "eB", q(0), q(0)),
                    relation("r2", "eA", "eC", q(0), q(0)),
                ],
            }],
        }
        # Compute the actual deltas so the relation bounds force known offsets.
        delta_b = (Fraction(10 ** 9 * 7, 1000)
                   - Fraction(10 ** 9 * 5, 500))
        delta_c = (Fraction(10 ** 9 * 9, 600)
                   - Fraction(10 ** 9 * 5, 500))
        # Force x_beta - x_alpha = 3 and x_gamma - x_alpha = 6.
        job["bundles"][0]["relations"] = [
            relation("r1", "eA", "eB", q((delta_b + 3).numerator,
                                         (delta_b + 3).denominator),
                     q((delta_b + 3).numerator, (delta_b + 3).denominator)),
            relation("r2", "eA", "eC", q((delta_c + 6).numerator,
                                         (delta_c + 6).denominator),
                     q((delta_c + 6).numerator, (delta_c + 6).denominator)),
        ]
        path = self.write_job(job, "JOB_REAL.json")
        report, events = self.join(path, out_name="out_real")
        bundle = report["bundles"][0]
        self.assertEqual(bundle["status"], "conditional")
        offsets = self.offsets_map(bundle)
        self.assertEqual(offsets, {"alpha": Fraction(0),
                                   "beta": Fraction(3),
                                   "gamma": Fraction(6)})
        by_id = {e["event_id"]: e for e in events}
        # time_ns = offset + 10^9*index/rate.
        self.assertEqual(Fraction(by_id["eA"]["time_ns"]["num"],
                                  by_id["eA"]["time_ns"]["den"]),
                         Fraction(10 ** 9 * 5, 500))
        self.assertEqual(Fraction(by_id["eB"]["time_ns"]["num"],
                                  by_id["eB"]["time_ns"]["den"]),
                         Fraction(3) + Fraction(10 ** 9 * 7, 1000))
        self.assertEqual(Fraction(by_id["eC"]["time_ns"]["num"],
                                  by_id["eC"]["time_ns"]["den"]),
                         Fraction(6) + Fraction(10 ** 9 * 9, 600))

        # Cross-check every event value against the baseline excursion archive
        # built from the same packages.
        sources_path = self.root / "SOURCES.json"
        sources_path.write_text(json.dumps({"sources": [
            {"source_id": "pkg_a", "package": "pkg_a"},
            {"source_id": "pkg_b", "package": "pkg_b"},
            {"source_id": "pkg_c", "package": "pkg_c"},
        ]}))
        archive = self.root / "exc_archive"
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ex_main(["build", "--sources", str(sources_path),
                                      "--archive", str(archive)]), 0)
        for eid, pkgid, channel in (("eA", "pkg_a", "main"),
                                    ("eB", "pkg_b", "main"),
                                    ("eC", "pkg_c", "u")):
            proc = subprocess.run(
                [sys.executable, "-m", "signal_spectrum.excursions.cli",
                 "query", "--archive", str(archive), "--source", pkgid,
                 "--channel", channel, "--start", "0", "--stop", "1",
                 "--op", "ge", "--threshold=-1000000.0", "--limit", "1"],
                capture_output=True, text=True, env=self.env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            resumed = result["runs"]
            self.assertEqual(len(resumed), 1)
            # The excursions peak at index 0 equals archive value[0]; verify
            # the specific event index instead via a single-point query.
            index = {"eA": 5, "eB": 7, "eC": 9}[eid]
            proc = subprocess.run(
                [sys.executable, "-m", "signal_spectrum.excursions.cli",
                 "query", "--archive", str(archive), "--source", pkgid,
                 "--channel", channel, "--start", str(index),
                 "--stop", str(index + 1), "--op", "ge",
                 "--threshold=-1e300", "--limit", "1"],
                capture_output=True, text=True,
                env=self.env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            point = json.loads(proc.stdout)["runs"]
            self.assertEqual(len(point), 1)
            self.assertEqual(by_id[eid]["value"], point[0]["peak_value"])


class ResourceBound(JoinCase):
    """Peak RSS on a corpus whose total raw bytes far exceed the 192 MiB cap.

    Method: three ~80 MiB float32 packages (240 MiB total, larger than the
    cap) are each streamed once by the CLI subprocess; its high-water mark is
    read from ``resource.getrusage(RUSAGE_CHILDREN).ru_maxrss`` (KiB on
    Linux) sampled before and after.  The measured value is printed.
    """

    POINTS = 20_000_000  # 80 MiB of float32 per source, 240 MiB total

    def _write_big(self, name, capture_id, spike_index):
        pkg = self.root / name
        pkg.mkdir()
        raw_path = pkg / "raw.bin"
        chunk = b"\x00" * (4 * 1_000_000)
        with open(raw_path, "wb") as stream:
            for _ in range(self.POINTS // 1_000_000):
                stream.write(chunk)
        arr = struct.pack("<f", 5.0)
        with open(raw_path, "r+b") as stream:
            stream.seek(spike_index * 4)
            stream.write(arr)
        sha = hashlib.sha256()
        with open(raw_path, "rb") as stream:
            while True:
                block = stream.read(1 << 20)
                if not block:
                    break
                sha.update(block)
        raw_bytes = self.POINTS * 4
        meta = {"capture_id": capture_id, "sample_rate": 1000,
                "points": self.POINTS, "dtype": "float32",
                "byte_order": "little",
                "channels": [{"name": "ch", "scale": 1.0, "offset": 0.0}],
                "raw_bytes": raw_bytes, "sha256": sha.hexdigest()}
        manifest = {
            "schema_version": 1, "source_url": "http://127.0.0.1:9/d",
            "capabilities": {"max_range_bytes": 4096, "lease": False},
            "consistency_channel": "etag", "etag": '"rss"',
            "capture_id": capture_id,
            "config": {"sample_rate": 1000, "points": self.POINTS,
                       "dtype": "float32", "byte_order": "little"},
            "channels": meta["channels"], "raw_bytes": raw_bytes,
            "raw_sha256": meta["sha256"],
            "meta_sha256": canonical_meta_sha(meta), "analysis": None,
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

    def test_rss_under_cap(self):
        for index, name in enumerate(("big1", "big2", "big3")):
            self._write_big(name, "cap-" + name, 1_000_000 + index)
        job = {
            "reference": "big1",
            "sources": [{"source_id": n, "package": n}
                        for n in ("big1", "big2", "big3")],
            "anchors": [
                anchor("ab", ep("big1", 0), ep("big2", 0), q(0), q(10)),
                anchor("ac", ep("big1", 0), ep("big3", 0), q(0), q(10)),
            ],
            "bundles": [{
                "bundle_id": "b",
                "events": [event("e1", "big1", "ch", 1_000_000),
                           event("e2", "big2", "ch", 1_000_001),
                           event("e3", "big3", "ch", 1_000_002)],
                "relations": [
                    relation("r1", "e1", "e2", q(10 ** 6), q(10 ** 6)),
                    relation("r2", "e1", "e3", q(2 * 10 ** 6),
                             q(2 * 10 ** 6))],
            }],
        }
        path = self.write_job(job, "JOB_RSS.json")
        # Measure the peak RSS of exactly this one CLI child in a fresh driver
        # process, so the reading is attributable to this command rather than
        # to an earlier test that happened to spawn a heavier child.
        out_dir = self.root / "out_rss"
        driver = (
            "import resource, subprocess, sys\n"
            "subprocess.run([sys.executable, '-m', 'signal_spectrum."
            "clockjoin.cli', '--source', sys.argv[1], '--output', "
            "sys.argv[2]], check=True)\n"
            "print(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)\n"
        )
        total_raw = sum((self.root / n / "raw.bin").stat().st_size
                        for n in ("big1", "big2", "big3"))
        self.assertGreater(total_raw, RSS_CAP_KIB * 1024,
                          "corpus must be significantly larger than the cap")
        proc = subprocess.run(
            [sys.executable, "-c", driver, str(path), str(out_dir)],
            capture_output=True, text=True, env=self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        peak_kib = int(proc.stdout.strip().splitlines()[-1])
        print("signal-clock-join peak RSS: %d KiB (cap %d KiB)"
              % (peak_kib, RSS_CAP_KIB))
        self.assertLessEqual(peak_kib, RSS_CAP_KIB)
        report = json.loads(
            (self.root / "out_rss" / "report.json").read_text())
        self.assertEqual(report["sources"][0]["points"], self.POINTS)
        events = [json.loads(line) for line in
                  (self.root / "out_rss" / "events.ndjson").read_text()
                  .splitlines()]
        self.assertEqual(len(events), 3)



class RandomizedEngine(unittest.TestCase):
    """Property tests for the exact engine against a brute-force reference."""

    def setUp(self):
        sys.path.insert(0, str(SRC))
        from signal_spectrum.clockjoin import clock
        self.clock = clock

    def _solve(self, node_count, edges, ref=0):
        cycle = self.clock.find_negative_cycle(node_count, edges)
        if cycle is not None:
            return False, cycle
        offsets = self.clock.shortest_to_reference(node_count, edges, ref)
        return True, offsets

    def test_negative_cycle_evidence_is_real_and_negative(self):
        import random
        rng = random.Random(20240501)
        for trial in range(400):
            node_count = rng.randint(2, 5)
            edges = []
            # Every node is bounded relative to node 0 (the reference).
            for node in range(1, node_count):
                for u, v in ((0, node), (node, 0)):
                    w = Fraction(rng.randint(-8, 8), rng.choice((1, 2, 3)))
                    edges.append(self.clock.Edge(
                        self.clock.ANCHOR, "a%d" % len(edges), "upper",
                        "n%d" % u, "n%d" % v, u, v, w))
            for _ in range(rng.randint(1, 9)):
                u = rng.randrange(node_count)
                v = rng.randrange(node_count)
                w = Fraction(rng.randint(-6, 6), rng.choice((1, 2, 3)))
                edges.append(self.clock.Edge(
                    self.clock.ANCHOR, "a%d" % len(edges), "upper",
                    "n%d" % u, "n%d" % v, u, v, w))
            feasible, result = self._solve(node_count, edges)
            if feasible:
                # Verify: every constraint holds, and each coordinate is
                # minimal (the reference solver agrees and sees no smaller
                # point).
                for edge in edges:
                    self.assertLessEqual(
                        result[edge.to_idx] - result[edge.from_idx],
                        edge.weight)
                self.assertEqual(result[0], Fraction(0))
                # Compare with an independent single-source shortest-path
                # computation: x_s = -dist(s, ref), i.e. shortest path from
                # the reference to ``s`` in the edge-reversed graph.
                inf = None
                dist = [inf] * node_count
                dist[0] = Fraction(0)
                for _ in range(node_count):
                    changed = False
                    for edge in edges:
                        # reversed edge: from edge.to_idx to edge.from_idx
                        base = dist[edge.to_idx]
                        if base is None:
                            continue
                        cand = base + edge.weight
                        cur = dist[edge.from_idx]
                        if cur is None or cand < cur:
                            dist[edge.from_idx] = cand
                            changed = True
                    if not changed:
                        break
                for node in range(node_count):
                    self.assertEqual(result[node], -dist[node])
            else:
                # Evidence: closed walk over the supplied edges, total < 0.
                total = Fraction(0)
                ids = []
                for position, edge in enumerate(result):
                    nxt = result[(position + 1) % len(result)]
                    self.assertEqual(edge.to_idx, nxt.from_idx)
                    total += edge.weight
                    ids.append(id(edge))
                self.assertLess(total, 0)
                for edge in result:
                    self.assertIn(edge, edges)

    def test_reorder_invariance_of_engine(self):
        import random
        rng = random.Random(7)
        for trial in range(60):
            node_count = rng.randint(3, 5)
            base = []
            for node in range(1, node_count):
                for u, v in ((0, node), (node, 0)):
                    w = Fraction(rng.randint(-8, 8), rng.choice((1, 2, 3)))
                    base.append((u, v, w, "b%d" % len(base)))
            for _ in range(rng.randint(2, 7)):
                u = rng.randrange(node_count)
                v = rng.randrange(node_count)
                w = Fraction(rng.randint(-3, 5), rng.choice((1, 2, 3)))
                base.append((u, v, w, "e%d" % len(base)))
            def build(order):
                edges = [self.clock.Edge(
                    self.clock.ANCHOR, ident, "upper", "n%d" % u,
                    "n%d" % v, u, v, w) for (u, v, w, ident) in order]
                return self._solve(node_count, edges)
            f1, r1 = build(base)
            shuffled = list(base)
            rng.shuffle(shuffled)
            f2, r2 = build(shuffled)
            self.assertEqual(f1, f2)
            if f1:
                self.assertEqual(r1, r2)
            else:
                self.assertEqual(
                    [e.id for e in r1], [e.id for e in r2])


class JobValidation(JoinCase):
    def _good(self):
        self.write_package("A", "cap-a", 10, 8, "int16", "little",
                           [("m", 1.0, 0.0)], lambda p, c: p)
        self.write_package("B", "cap-b", 10, 8, "int16", "little",
                           [("m", 1.0, 0.0)], lambda p, c: p)
        return {
            "reference": "A",
            "sources": [{"source_id": "A", "package": "A"},
                        {"source_id": "B", "package": "B"}],
            "anchors": [anchor("ab", ep("A", 0), ep("B", 0), q(0), q(10))],
            "bundles": [{"bundle_id": "b",
                         "events": [event("eA", "A", "m", 0)],
                         "relations": []}],
        }

    def assert_rejected(self, payload, name, raw=None):
        path = self.root / name
        if raw is not None:
            path.write_text(raw)
        else:
            path.write_text(json.dumps(payload))
        self.run_cli("--source", path, "--output", self.root / ("o_" + name),
                     expect=2)
        self.assertFalse((self.root / ("o_" + name)).exists())

    def test_structural_job_violations(self):
        good = self._good()
        variants = []

        v = json.loads(json.dumps(good)); v["extra"] = 1; variants.append(v)
        v = json.loads(json.dumps(good)); del v["bundles"]; variants.append(v)
        v = json.loads(json.dumps(good)); v["reference"] = "Z"; variants.append(v)
        v = json.loads(json.dumps(good)); v["sources"] = []; variants.append(v)
        v = json.loads(json.dumps(good))
        v["sources"][0]["extra"] = 1; variants.append(v)
        v = json.loads(json.dumps(good))
        v["sources"][0]["source_id"] = ""; variants.append(v)
        v = json.loads(json.dumps(good))
        v["sources"][1]["source_id"] = "A"; variants.append(v)
        v = json.loads(json.dumps(good))
        v["anchors"][0]["lower_ns"] = q(11); variants.append(v)
        v = json.loads(json.dumps(good))
        v["anchors"][0]["lower_ns"] = {"num": True, "den": 1}; variants.append(v)
        v = json.loads(json.dumps(good))
        v["anchors"][0]["lower_ns"] = {"num": 0, "den": 0}; variants.append(v)
        v = json.loads(json.dumps(good))
        v["anchors"][0]["lower_ns"] = {"num": 10 ** 19, "den": 1}
        variants.append(v)
        v = json.loads(json.dumps(good))
        v["anchors"][0]["anchor_id"] = v["anchors"][0]["anchor_id"][:0]
        variants.append(v)
        v = json.loads(json.dumps(good))
        v["anchors"].append(dict(v["anchors"][0])); variants.append(v)
        v = json.loads(json.dumps(good))
        v["bundles"][0]["relations"] = [
            relation("r", "eA", "nope", q(0), q(1))]; variants.append(v)
        v = json.loads(json.dumps(good))
        v["bundles"][0]["events"][0]["channel"] = "nope"; variants.append(v)
        v = json.loads(json.dumps(good))
        v["bundles"][0]["events"][0]["index"] = 99; variants.append(v)
        v = json.loads(json.dumps(good))
        v["bundles"] = []; variants.append(v)
        v = json.loads(json.dumps(good))
        v["bundles"].append(json.loads(json.dumps(v["bundles"][0])))
        variants.append(v)
        for index, payload in enumerate(variants):
            self.assert_rejected(payload, "bad%d.json" % index)

    def test_json_parse_violations(self):
        self._good()
        self.assert_rejected(None, "dup.json",
                             raw='{"reference":"A","reference":"A",'
                                 '"sources":[],"anchors":[],"bundles":[]}')
        self.assert_rejected(None, "nan.json",
                             raw='{"reference":"A","sources":[],"anchors":[],'
                                 '"bundles":[],"x":NaN}')
        self.assert_rejected(None, "trunc.json", raw='{"reference":"A"')

    def test_job_max_bytes(self):
        self._good()
        pad = "x" * (8 * 1024 * 1024)
        payload = json.dumps({
            "reference": "A",
            "sources": [{"source_id": "A", "package": "A"}],
            "anchors": [],
            "bundles": [{"bundle_id": pad, "events": [
                event("eA", "A", "m", 0)], "relations": []}],
        })
        path = self.root / "toobig.json"
        path.write_text(payload)
        self.run_cli("--source", path, "--output", self.root / "o_big",
                     expect=2)


if __name__ == "__main__":
    unittest.main()
