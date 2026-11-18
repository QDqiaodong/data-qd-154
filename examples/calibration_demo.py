"""Offline demonstration of ``signal-calibration``.

Builds one synthetic published waveform package whose ``ch0`` raw values are a
piecewise-constant reference-step trial (unequal-length steps, a non-unit
channel scale/offset), writes a STEPS file with up and down segments plus two
future readings, runs the real CLI as a subprocess and prints the published
certificate.

Run with:  PYTHONPATH=src python3 examples/calibration_demo.py
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


def step(start, stop, reference, direction):
    return {"start": start, "stop": stop, "reference": reference,
            "direction": direction}


def main():
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        # Reference values 0..3.  Up means 0,4,2,6 pool to an isotonic
        # 0,3,3,6; down means 3,4,6,8 already increase.  Step lengths differ.
        up_layout = [(0, 3, 0), (3, 4, 1), (4, 6, 2), (6, 8, 3)]
        down_layout = [(8, 11, 0), (11, 12, 1), (12, 14, 2), (14, 16, 3)]
        up_means = {0: 0, 1: 4, 2: 2, 3: 6}
        down_means = {0: 3, 1: 4, 2: 6, 3: 8}
        raw_values = []
        steps = []
        for start, stop, reference in up_layout:
            steps.append(step(start, stop, reference, "up"))
            raw_values.extend([up_means[reference]] * (stop - start))
        for start, stop, reference in down_layout:
            steps.append(step(start, stop, reference, "down"))
            raw_values.extend([down_means[reference]] * (stop - start))

        # The channel scales raw counts by 0.5 and offsets them; the step
        # means in the certificate are therefore half of the raw values.
        write_package(root / "pkg", "capture-demo", 20, len(raw_values),
                      "float32", "little",
                      [("ch0", 0.5, 0.0)],
                      lambda point, channel: float(raw_values[point]))

        trial = {
            "steps": steps,
            "readings": [
                {"id": "future-at-3", "channel": "ch0",
                 "direction": "unknown", "low": 1.5, "high": 1.5},
                {"id": "future-band", "channel": "ch0",
                 "direction": "up", "low": 1.5, "high": 2.0},
            ],
            "residual_tolerance": 1,
            "hysteresis_tolerance": 2,
        }
        steps_path = root / "STEPS.json"
        steps_path.write_text(json.dumps(trial, sort_keys=True, indent=2))

        certificate_path = root / "certificate.json"
        subprocess.run(
            [sys.executable, "-m", "signal_spectrum.calibration.cli",
             "--package", str(root / "pkg"),
             "--steps", str(steps_path),
             "--certificate", str(certificate_path)],
            check=True,
            env={"PYTHONPATH": str(SRC)},
        )
        print("=== certificate.json ===")
        print(certificate_path.read_text())


if __name__ == "__main__":
    main()
