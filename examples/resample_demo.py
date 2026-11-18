"""Offline demonstration of ``signal-resample``.

Builds one synthetic published waveform package in a temporary directory
(two channels from different "devices", non-unit calibration), resamples it
from 48,000 Hz to 16,000 Hz with the real CLI as a subprocess, and prints the
derived dataset's lineage together with a constant-gain and stopband check.

Run with:  PYTHONPATH=src python3 examples/resample_demo.py
"""
import array
import hashlib
import json
import math
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


def read_channel(dest, channel, channel_count):
    flat = array.array("d")
    flat.frombytes((Path(dest) / "samples.f64le").read_bytes())
    return flat[channel::channel_count]


def main():
    source_rate, target_rate = 48_000, 16_000
    points = 4096
    amplitude = 10_000
    # Channel 0: 3 kHz passband sine (must survive).  Channel 1: 18 kHz sine
    # (must be removed by the anti-alias kernel).

    def raw(point, channel):
        angle = 2 * math.pi * (3_000 if channel == 0 else 18_000) * point \
            / source_rate
        return int(round(amplitude * math.sin(angle)))

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        pkg = root / "pkg"
        write_package(pkg, "demo-capture", source_rate, points, "int16",
                      "little",
                      [("passband", 1.0, 0.0), ("stopband", 1.0, 0.0)],
                      raw)
        dest = root / "resampled"
        proc = subprocess.run(
            [sys.executable, "-m", "signal_spectrum.resample.cli",
             "--input", str(pkg), "--target-rate", str(target_rate),
             "--dest", str(dest)],
            capture_output=True, text=True,
            env={"PYTHONPATH": str(SRC)})
        if proc.returncode != 0:
            print(proc.stderr, file=sys.stderr)
            raise SystemExit(proc.returncode)
        print("CLI summary:")
        print(json.dumps(json.loads(proc.stdout), indent=2, sort_keys=True))
        lineage = json.loads((dest / "lineage.json").read_text())
        print("\nlineage.json:")
        print(json.dumps(lineage, indent=2, sort_keys=True))

        channels = len(lineage["output_channels"])
        kept = read_channel(dest, 0, channels)
        removed = read_channel(dest, 1, channels)
        edge = len(kept) // 10
        interior = kept[edge:len(kept) - edge]
        kept_rms = math.sqrt(math.fsum(v * v for v in interior)
                             / len(interior))
        removed_rms = math.sqrt(math.fsum(v * v for v in removed) / len(removed))
        input_rms = amplitude / math.sqrt(2.0)
        print("\nchecks:")
        print("  output points        :", len(kept))
        print("  first global m       :", lineage["first_output_index"])
        print("  ratio                : %d/%d"
              % (lineage["ratio_p"], lineage["ratio_q"]))
        print("  passband amplitude   : %.4f (input %.4f)"
              % (kept_rms * math.sqrt(2.0), amplitude))
        print("  stopband RMS ratio   : %.6f"
              % (removed_rms / input_rms))
        print("  output sha256        :", lineage["output_sha256"])
        assert abs(kept_rms - input_rms) < 0.02 * input_rms
        assert removed_rms < 0.01 * input_rms


if __name__ == "__main__":
    main()
