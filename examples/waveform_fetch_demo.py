"""Offline end-to-end demonstration of signal-waveform-fetch.

Runs the real TCP/HTTP device simulator in-process and the real collector
against it, showing both consistency channels, an identity change mid-transfer
(same capture id, new layout) and a clean failure.  No external network,
accounts or mocks of the HTTP layer are involved.

Run with:  PYTHONPATH=src python3 examples/waveform_fetch_demo.py
"""
import json
import tempfile
from pathlib import Path

from signal_spectrum.model import analyze
from signal_spectrum.waveform.collector import build_config, run_acquisition
from signal_spectrum.waveform.device import (
    DeviceServer, build_capture, default_raw_fn)
from signal_spectrum.waveform.protocol import BudgetExceeded
from signal_spectrum.waveform.store import Staging, is_published


def title(text):
    print("\n=== " + text + " ===")


def main():
    first = build_capture(
        "cap-demo", 2000, 48, "int16", "little",
        [("a", 0.001, 0.0), ("b", 0.002, 1.5)], default_raw_fn)

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)

        title("ETag-only device (conditional reads, no lease)")
        etag_server = DeviceServer(first, max_range_bytes=101,
                                   supports_lease=False)
        etag_server.start()
        try:
            out = root / "etag-pkg"
            manifest = run_acquisition(build_config(etag_server.url()),
                                       Staging(out))
            print("published:", out.name, "channel:",
                  manifest["consistency_channel"])
        finally:
            etag_server.stop()

        title("Lease device with the legacy-DFT analysis export")
        lease_server = DeviceServer(first, max_range_bytes=77,
                                    supports_lease=True, lease_ttl=30)
        lease_server.start()
        try:
            out = root / "lease-pkg"
            manifest = run_acquisition(
                build_config(lease_server.url(), analysis=(0, 4)),
                Staging(out))
            print("published:", out.name, "channel:",
                  manifest["consistency_channel"])
            analysis = json.loads((out / "analysis.json").read_text())
            print("legacy DFT peak bin:", analyze(analysis)["peak_bin"])
            print("leases released:", lease_server.leases() == {})
        finally:
            lease_server.stop()

        title("Same capture id, new layout mid-transfer -> restart from zero")
        swapped = build_capture(
            "cap-demo", 500, 48, "float32", "big",
            [("only", 0.25, -3.0)], default_raw_fn)
        moving = DeviceServer(first, max_range_bytes=40,
                              supports_lease=False)
        moving.start()
        moving.swap_after_data_requests(swapped, 2)
        try:
            out = root / "restart-pkg"
            manifest = run_acquisition(
                build_config(moving.url(), analysis=(0, 4)), Staging(out))
            print("final dtype:", manifest["config"]["dtype"],
                  "byte_order:", manifest["config"]["byte_order"],
                  "channels:", len(manifest["channels"]))
        finally:
            moving.stop()

        title("Continuously changing device -> bounded failure, no package")
        other = build_capture(
            "rot", 9999, 48, "int16", "little",
            [("a", 1.0, 0.0)], lambda p, c: 9000 + p + c)
        unstable = DeviceServer(first, max_range_bytes=32,
                                supports_lease=False)
        unstable.start()
        unstable.device_state.auto_alternate = [first, other]
        import os
        os.environ["SIGSPECTRUM_WF_RESTARTS"] = "2"
        try:
            out = root / "bounded-fail"
            try:
                run_acquisition(build_config(unstable.url()), Staging(out))
            except BudgetExceeded as error:
                print("bounded failure:", error)
            print("package published:", is_published(out),
                  "| staging kept:", Staging(out).path.is_dir())
        finally:
            del os.environ["SIGSPECTRUM_WF_RESTARTS"]
            unstable.stop()

    print("\nAll demonstration scenarios completed.")


if __name__ == "__main__":
    main()
