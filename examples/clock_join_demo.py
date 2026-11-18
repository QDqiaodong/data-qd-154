"""Offline demonstration of ``signal-clock-join``.

Builds three synthetic published waveform packages (A/B/C) in a temporary
directory, writes a job file with base anchors and candidate bundles, runs the
real CLI as a subprocess, and prints the published report and event stream.

Run with:  PYTHONPATH=src python3 examples/clock_join_demo.py
"""
import hashlib
import json
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def canonical_meta_sha(meta):
    return hashlib.sha256(json.dumps(
        meta, sort_keys=True, ensure_ascii=False, allow_nan=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def write_package(pkg_dir, capture_id, sample_rate, points, dtype,
                  byte_order, channels, raw_fn):
    pkg_dir = Path(pkg_dir)
    pkg_dir.mkdir(parents=True)
    code = {"int16": "h", "uint16": "H", "float32": "f"}[dtype]
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
    (pkg_dir / "raw.bin").write_bytes(raw)
    (pkg_dir / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2))


def q(num, den=1):
    return {"num": num, "den": den}


def ep(source_id, index):
    return {"source_id": source_id, "index": index}


def event(event_id, source_id, channel, index):
    return {"event_id": event_id, "source_id": source_id,
            "channel": channel, "index": index}


def relation(relation_id, left, right, lower, upper):
    return {"relation_id": relation_id, "left": left, "right": right,
            "lower_ns": lower, "upper_ns": upper}


def main():
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        write_package(root / "A", "cap-a", 10, 8, "int16", "little",
                      [("main", 0.5, 0.0)], lambda p, c: p)
        write_package(root / "B", "cap-b", 10, 8, "int16", "little",
                      [("main", 0.5, 10.0)], lambda p, c: p)
        # C runs at a different rate, so its sample-time delta is fractional.
        write_package(root / "C", "cap-c", 30, 24, "float32", "big",
                      [("main", 2.0, 1.0)], lambda p, c: float(p))

        job = {
            "reference": "A",
            "sources": [
                {"source_id": "A", "package": "A"},
                {"source_id": "B", "package": "B"},
                {"source_id": "C", "package": "C"},
            ],
            "anchors": [
                {"anchor_id": "ba", "left": ep("A", 0), "right": ep("B", 0),
                 "lower_ns": q(0), "upper_ns": q(10)},
                {"anchor_id": "ca", "left": ep("A", 0), "right": ep("C", 0),
                 "lower_ns": q(0), "upper_ns": q(10)},
            ],
            "bundles": [
                {
                    # B-A = 0, C-B = 0, C-A = 1: each relation alone is
                    # satisfiable, yet no single offset vector satisfies all
                    # three -> impossible, with a real negative cycle.
                    "bundle_id": "conflicting",
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
                    # B-A = 5, C-B = 2 -> minimal A=0, B=5, C=7 (conditional,
                    # because the base anchors alone do not force these).
                    "bundle_id": "resolved",
                    "events": [event("eA", "A", "main", 0),
                               event("eB", "B", "main", 0),
                               event("eC", "C", "main", 0)],
                    "relations": [
                        relation("r1", "eA", "eB", q(5), q(5)),
                        relation("r2", "eB", "eC", q(2), q(2)),
                    ],
                },
                {
                    # No relations -> guaranteed; offsets stay at the base
                    # minimum (B=C=0 is feasible because the base admits 0).
                    "bundle_id": "plain",
                    "events": [event("eA", "A", "main", 0)],
                    "relations": [],
                },
            ],
        }
        job_path = root / "JOB.json"
        job_path.write_text(json.dumps(job, sort_keys=True, indent=2))

        out = root / "joined"
        subprocess.run(
            [sys.executable, "-m", "signal_spectrum.clockjoin.cli",
             "--source", str(job_path), "--output", str(out)],
            check=True,
            env={"PYTHONPATH": str(SRC)},
        )
        print("\n=== report.json ===")
        print((out / "report.json").read_text())
        print("=== events.ndjson ===")
        print((out / "events.ndjson").read_text())


if __name__ == "__main__":
    main()
