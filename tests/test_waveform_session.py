"""End-to-end acquisition tests over real loopback HTTP.

These tests exercise the full collector against :class:`DeviceServer` (real
TCP sockets, real request/response framing) and cover every acceptance
scenario: both consistency channels, mid-transfer configuration changes under
a reused capture id, lease expiry, stream-truncation continuation, bounded
failure under continuous change, crash checkpoints, tamper detection and the
optional legacy-DFT analysis export.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from signal_spectrum.model import analyze
from signal_spectrum.waveform.collector import (
    CRASH_AFTER_CHECKPOINT,
    CRASH_AFTER_RAW,
    build_config,
    run_acquisition,
)
from signal_spectrum.waveform.device import (
    DeviceServer, build_capture, default_raw_fn)
from signal_spectrum.waveform.protocol import (
    BudgetExceeded,
    CheckpointError,
    ProtocolError,
)
from signal_spectrum.waveform.store import Staging, is_published


def two_channel_int16(cid="cap-link", points=48):
    return build_capture(
        cid, 2000, points, "int16", "little",
        [("a", 0.001, 0.0), ("b", 0.002, 1.5)], default_raw_fn)


def float_capture(cid="cap-link", points=48):
    # Same capture id, completely different layout and calibration.
    return build_capture(
        cid, 500, points, "float32", "big",
        [("only", 0.25, -3.0)], default_raw_fn)


def read_ndjson(path):
    rows = []
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            rows.append(json.loads(line))
    return rows


class AcquisitionBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self._servers = []

    def tearDown(self):
        for server in self._servers:
            server.stop()
        self.temp.cleanup()

    def serve(self, capture, **kwargs):
        server = DeviceServer(capture, **kwargs)
        server.start()
        self._servers.append(server)
        return server

    def output(self, name="pkg"):
        return self.root / name

    def fetch(self, url, out, **kwargs):
        cfg = build_config(url, **kwargs)
        manifest = run_acquisition(cfg, Staging(out))
        rows = read_ndjson(out / "samples.ndjson")
        raw = (out / "raw.bin").read_bytes()
        return manifest, rows, raw

    def assert_package_matches(self, out, capture, channel):
        self.assertTrue(is_published(out))
        raw = (out / "raw.bin").read_bytes()
        self.assertEqual(raw, capture.raw)
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         capture.meta["sha256"])
        manifest = json.loads((out / "manifest.json").read_text())
        self.assertEqual(manifest["raw_sha256"], capture.meta["sha256"])
        self.assertEqual(manifest["raw_bytes"], len(capture.raw))
        self.assertEqual(manifest["etag"], capture.etag)
        self.assertEqual(manifest["consistency_channel"], channel)
        self.assertEqual(manifest["capture_id"],
                         capture.meta["capture_id"])
        self.assertEqual(manifest["config"]["dtype"],
                         capture.meta["dtype"])
        self.assertEqual(manifest["channels"], capture.meta["channels"])
        rows = read_ndjson(out / "samples.ndjson")
        self.assertEqual([row["index"] for row in rows],
                         list(range(capture.meta["points"])))
        self.assertTrue(all(
            len(row["values"]) == len(capture.meta["channels"])
            for row in rows))
        return manifest, rows, raw


class BothChannels(AcquisitionBase):
    def test_etag_only_device_full_success_without_analysis(self):
        capture = two_channel_int16()
        server = self.serve(capture, max_range_bytes=101,
                            supports_lease=False)
        out = self.output()
        manifest, rows, raw = self.fetch(server.url(), out)
        self.assertEqual(manifest["consistency_channel"], "etag")
        self.assertIsNone(manifest["analysis"])
        self.assertFalse((out / "analysis.json").exists())
        # Every data request was a conditional read of the live capture.
        data_entries = [e for e in server.request_log()
                        if e["kind"] == "data"]
        self.assertTrue(data_entries)
        self.assertTrue(all(e["if_match"] == capture.etag for e in data_entries))
        self.assertTrue(all("/leases/" not in e["path"] for e in data_entries))
        # Spot-check calibration of the first point.
        first = rows[0]["values"]
        self.assertEqual(first[0],
                         default_raw_fn(0, 0) * 0.001 + 0.0)
        self.assertEqual(first[1],
                         default_raw_fn(0, 1) * 0.002 + 1.5)

    def test_lease_device_uses_lease_reads_and_releases(self):
        capture = two_channel_int16()
        server = self.serve(capture, max_range_bytes=77,
                            supports_lease=True, lease_ttl=30)
        out = self.output()
        manifest, _, _ = self.fetch(server.url(), out)
        self.assertEqual(manifest["consistency_channel"], "lease")
        kinds = {}
        for entry in server.request_log():
            kinds[entry["kind"]] = kinds.get(entry["kind"], 0) + 1
        self.assertIn("lease_create", kinds)
        self.assertIn("lease_data", kinds)
        self.assertIn("lease_release", kinds)
        self.assertNotIn("data", kinds)  # no live-capture data reads
        # The lease slot was released at normal completion.
        self.assertEqual(server.leases(), {})

    def test_force_etag_channel_on_lease_capable_device(self):
        capture = two_channel_int16()
        server = self.serve(capture, max_range_bytes=64, supports_lease=True)
        out = self.output()
        manifest, _, _ = self.fetch(server.url(), out,
                                    force_channel="etag")
        self.assertEqual(manifest["consistency_channel"], "etag")
        self.assertNotIn("lease_create",
                         {e["kind"] for e in server.request_log()})

    def test_published_directory_is_never_overwritten(self):
        capture = two_channel_int16()
        server = self.serve(capture, max_range_bytes=64, supports_lease=False)
        out = self.output()
        self.fetch(server.url(), out)
        with self.assertRaises(CheckpointError):
            self.fetch(server.url(), out)


class IdentityChange(AcquisitionBase):
    def test_same_capture_id_new_layout_restarts_from_zero(self):
        # Linked acceptance: first identity is 2ch little-endian int16; after
        # one block the device swaps to 1ch big-endian float32 with the SAME
        # capture id and a changed scale.  The published package may only
        # describe the new layout.
        first = two_channel_int16(points=48)
        second = float_capture(cid="cap-link", points=48)
        server = self.serve(first, max_range_bytes=40,
                            supports_lease=False)
        # Deterministically swap identity after one committed data block.
        server.swap_after_data_requests(second, 2)
        out = self.output()
        manifest, rows, raw = self.fetch(
            server.url(), out, analysis=(0, 4))

        self.assertEqual(manifest["config"]["dtype"], "float32")
        self.assertEqual(manifest["config"]["byte_order"], "big")
        self.assertEqual(len(manifest["channels"]), 1)
        self.assertEqual(manifest["channels"][0]["scale"], 0.25)
        self.assertEqual(len(raw), second.meta["raw_bytes"])
        self.assertEqual(raw, second.raw)
        self.assertTrue(all(len(r["values"]) == 1 for r in rows))
        analysis = json.loads((out / "analysis.json").read_text())
        self.assertEqual(analysis["window"], "rectangular")
        self.assertFalse(analysis["remove_mean"])
        self.assertEqual(analysis["sample_rate"], 500)
        self.assertEqual(len(analysis["samples"]), 4)
        # The export must be consumable by the legacy DFT unchanged.
        report = analyze(analysis)
        self.assertIn("bins", report)
        # The request log proves no block of the old identity was committed
        # under the new one: the restart discards the whole prefix.
        etags = {e["etag"] for e in server.request_log()
                 if e["kind"] == "data"}
        self.assertIn(first.etag, etags)
        self.assertIn(second.etag, etags)

    def test_continuous_change_exhausts_budget_without_publishing(self):
        first = two_channel_int16(points=64)
        other = build_capture("rot", 9999, 64, "int16", "little",
                              [("a", 1.0, 0.0)],
                              lambda point, channel: 9000 + point + channel)
        self.assertNotEqual(first.raw, other.raw)
        server = self.serve(first, max_range_bytes=32,
                            supports_lease=False)
        # Deterministically alternate between two identities before each data
        # response, so no conditional read can ever finish a capture.
        server.device_state.auto_alternate = [first, other]
        out = self.output()
        old_budget = os.environ.get("SIGSPECTRUM_WF_RESTARTS")
        os.environ["SIGSPECTRUM_WF_RESTARTS"] = "2"
        try:
            with self.assertRaises(BudgetExceeded):
                self.fetch(server.url(), out)
        finally:
            if old_budget is None:
                del os.environ["SIGSPECTRUM_WF_RESTARTS"]
            else:
                os.environ["SIGSPECTRUM_WF_RESTARTS"] = old_budget
        self.assertFalse(is_published(out))
        # The identifiable staging state survives the bounded failure.
        self.assertTrue(Staging(out).path.is_dir())


class LeaseBehaviour(AcquisitionBase):
    def test_lease_expiry_mid_transfer_restarts_with_new_lease(self):
        capture = two_channel_int16(points=64)
        server = self.serve(capture, max_range_bytes=48,
                            supports_lease=True, lease_ttl=30)
        out = self.output()
        # Expire the pinned lease once its first data request has been
        # served; the collector must discard that prefix and pin a fresh one.
        server.expire_leases_after_data_requests(1)
        manifest, rows, raw = self.fetch(server.url(), out)
        self.assertEqual(manifest["consistency_channel"], "lease")
        self.assertEqual(raw, capture.raw)
        # Two distinct leases were used; the expired prefix was discarded.
        lease_creates = [e for e in server.request_log()
                         if e["kind"] == "lease_create"]
        self.assertGreaterEqual(len(lease_creates), 2)
        self.assert_package_matches(out, capture, "lease")

    def test_expired_lease_resume_does_not_splice_identities(self):
        capture = two_channel_int16(points=48)
        server = self.serve(capture, max_range_bytes=40,
                            supports_lease=True)
        out = self.output()
        staging = Staging(out)
        cfg = build_config(server.url())
        # Seed a checkpoint that pretends one block of an expired lease exists.
        staging.prepare_fresh()
        block = capture.raw[:40]
        with open(staging.raw_path, "wb") as stream:
            stream.write(block)
        checkpoint = {
            "version": 1,
            "url": server.url(),
            "meta": capture.meta,
            "etag": capture.etag,
            "channel": "lease",
            "lease_token": "lease-gone",
            "committed_offset": 40,
            "prefix_sha256": hashlib.sha256(block).hexdigest(),
            "restarts_used": 0,
            "analysis": None,
        }
        staging.write_checkpoint(checkpoint)
        server.expire_leases()  # the old token is unknown -> 410
        manifest = run_acquisition(cfg, staging, resume=True)
        raw = (out / "raw.bin").read_bytes()
        self.assertEqual(raw, capture.raw)  # restarted, never spliced
        self.assertEqual(manifest["etag"], capture.etag)


class TruncationAndResume(AcquisitionBase):
    def test_truncated_stream_retries_same_range_same_identity(self):
        capture = two_channel_int16(points=48)
        server = self.serve(capture, max_range_bytes=64,
                            supports_lease=False)
        # Deterministically truncate the first two data deliveries; every
        # later response is well-formed again.
        server.add_repeated_fault("truncate", 2)
        out = self.output()
        os.environ["SIGSPECTRUM_WF_BLOCK_RETRIES"] = "4"
        try:
            manifest, _, raw = self.fetch(server.url(), out)
        finally:
            del os.environ["SIGSPECTRUM_WF_BLOCK_RETRIES"]
        self.assertEqual(raw, capture.raw)
        # The same starting offsets were requested again after truncation:
        # two injected failures mean exactly two repeated start offsets.
        starts = [e["range"][0] for e in server.request_log()
                  if e["kind"] == "data"]
        self.assertEqual(len(starts) - len(set(starts)), 2)

    def test_retry_budget_is_finite(self):
        capture = two_channel_int16(points=32)
        server = self.serve(capture, max_range_bytes=32,
                            supports_lease=False)
        server.add_fault("truncate")
        out = self.output()
        os.environ["SIGSPECTRUM_WF_BLOCK_RETRIES"] = "1"
        try:
            with self.assertRaises((BudgetExceeded, ProtocolError, OSError)):
                self.fetch(server.url(), out)
        finally:
            del os.environ["SIGSPECTRUM_WF_BLOCK_RETRIES"]
        self.assertFalse(is_published(out))

    def _crash_and_resume(self, crash_point):
        capture = two_channel_int16(points=64)
        server = self.serve(capture, max_range_bytes=48,
                            supports_lease=False)
        out = self.output()
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        env["SIGSPECTRUM_WF_CRASH"] = crash_point
        env["SIGSPECTRUM_WF_BLOCK_RETRIES"] = "4"
        first = subprocess.run(
            [sys.executable, "-m", "signal_spectrum.waveform.cli",
             "--url", server.url(), "--output", str(out)],
            capture_output=True, text=True, env=env)
        self.assertEqual(first.returncode, 12, first.stderr)
        self.assertFalse(is_published(out))
        staging = Staging(out)
        checkpoint = staging.read_checkpoint()
        # Second run without the crash hook resumes and finishes.
        env.pop("SIGSPECTRUM_WF_CRASH", None)
        second = subprocess.run(
            [sys.executable, "-m", "signal_spectrum.waveform.cli",
             "--url", server.url(), "--output", str(out), "--resume"],
            capture_output=True, text=True, env=env)
        self.assertEqual(second.returncode, 0, second.stderr)
        return server, capture, out, checkpoint

    def test_crash_after_raw_append_tail_is_not_committed(self):
        server, capture, out, checkpoint = self._crash_and_resume(
            CRASH_AFTER_RAW)
        raw = (out / "raw.bin").read_bytes()
        self.assertEqual(raw, capture.raw)
        # The crash point leaves a raw tail beyond the checkpoint offset.
        self.assertGreaterEqual(checkpoint["committed_offset"], 0)

    def test_crash_after_checkpoint_resumes_without_repeat_or_gap(self):
        server, capture, out, _ = self._crash_and_resume(
            CRASH_AFTER_CHECKPOINT)
        raw = (out / "raw.bin").read_bytes()
        self.assertEqual(raw, capture.raw)
        rows = read_ndjson(out / "samples.ndjson")
        self.assertEqual([r["index"] for r in rows],
                         list(range(capture.meta["points"])))

    def test_tampered_committed_prefix_is_rejected(self):
        capture = two_channel_int16(points=32)
        server = self.serve(capture, max_range_bytes=40,
                            supports_lease=False)
        out = self.output()
        staging = Staging(out)
        staging.prepare_fresh()
        block = capture.raw[:40]
        with open(staging.raw_path, "wb") as stream:
            stream.write(block)
        tampered = bytearray(block)
        tampered[0] ^= 0xFF
        staging.write_checkpoint({
            "version": 1,
            "url": server.url(),
            "meta": capture.meta,
            "etag": capture.etag,
            "channel": "etag",
            "lease_token": None,
            "committed_offset": 40,
            "prefix_sha256": hashlib.sha256(bytes(tampered)).hexdigest(),
            "restarts_used": 0,
            "analysis": None,
        })
        with self.assertRaises(CheckpointError):
            run_acquisition(build_config(server.url()), staging, resume=True)
        self.assertFalse(is_published(out))

    def test_resume_requires_flag_and_matches_url(self):
        capture = two_channel_int16(points=24)
        server = self.serve(capture, max_range_bytes=40,
                            supports_lease=False)
        out = self.output()
        self.fetch(server.url(), out)
        # A fresh (non-resume) run against the now-published name is refused.
        with self.assertRaises(CheckpointError):
            Staging(out).prepare_fresh()

        # A staging directory left by a failure cannot be reused without
        # --resume; only --resume may open it.
        failed_out = self.output("failed")
        staging = Staging(failed_out)
        staging.prepare_fresh()
        staging.write_checkpoint({
            "version": 1, "url": server.url(), "meta": capture.meta,
            "etag": capture.etag, "channel": "etag", "lease_token": None,
            "committed_offset": 0,
            "prefix_sha256": hashlib.sha256(b"").hexdigest(),
            "restarts_used": 0, "analysis": None,
        })
        with self.assertRaises(CheckpointError):
            run_acquisition(build_config(server.url()), staging, resume=False)
        # A checkpoint bound to a different URL is rejected on resume.
        other = self.serve(capture, max_range_bytes=40, supports_lease=False)
        self.assertNotEqual(other.url(), server.url())
        with self.assertRaises(CheckpointError):
            run_acquisition(build_config(other.url()), staging, resume=True)

    def test_analysis_channel_out_of_range_fails_cleanly(self):
        capture = two_channel_int16(points=24)
        server = self.serve(capture, max_range_bytes=40,
                            supports_lease=False)
        out = self.output()
        with self.assertRaises(ProtocolError):
            self.fetch(server.url(), out, analysis=(5, 4))
        self.assertFalse(is_published(out))


class MaliciousResponses(AcquisitionBase):
    def _fault_never_publishes(self, fault):
        capture = two_channel_int16(points=24)
        server = self.serve(capture, max_range_bytes=32,
                            supports_lease=False)
        server.add_fault(fault)
        out = self.output()
        os.environ["SIGSPECTRUM_WF_BLOCK_RETRIES"] = "1"
        try:
            with self.assertRaises(ProtocolError):
                self.fetch(server.url(), out)
        finally:
            del os.environ["SIGSPECTRUM_WF_BLOCK_RETRIES"]
        self.assertFalse(is_published(out))

    def test_range_ignored_200(self):
        self._fault_never_publishes("ignore_range")

    def test_bad_content_range(self):
        self._fault_never_publishes("bad_content_range")

    def test_excess_bytes(self):
        self._fault_never_publishes("excess_bytes")

    def test_missing_weak_conflicting_etag(self):
        for fault in ("missing_etag", "weak_etag", "conflicting_etag"):
            self._fault_never_publishes(fault)

    def test_same_etag_changed_calibration_is_caught(self):
        # Variant 1: after two delivered blocks the device swaps to a capture
        # with different raw bytes AND calibration but maliciously keeps
        # serving the old strong ETag.  Mixed bytes cannot match the declared
        # full-payload SHA, so nothing is published.
        capture = two_channel_int16(points=48)
        changed = build_capture(
            "cap-link", 999, 48, "int16", "little",
            [("a", 9.0, 0.0), ("b", 8.0, 0.0)],
            lambda point, channel: 12000 + point * 31 + channel * 7)
        self.assertNotEqual(changed.raw, capture.raw)
        server = self.serve(capture, max_range_bytes=40,
                            supports_lease=False)
        server.swap_after_data_requests(changed, 2, keep_etag=True)
        out = self.output()
        with self.assertRaises(ProtocolError):
            self.fetch(server.url(), out)
        self.assertFalse(is_published(out))

        # Variant 2: identical ETag and framing, silently mutated bytes; the
        # block-level streaming hash / final SHA still detect the corruption.
        fresh = self.serve(capture, max_range_bytes=32, supports_lease=False)
        fresh.add_fault("tamper_body")
        out2 = self.output("pkg2")
        with self.assertRaises(ProtocolError):
            self.fetch(fresh.url(), out2)
        self.assertFalse(is_published(out2))


class CliBehaviour(AcquisitionBase):
    def test_cli_exit_codes_and_analysis_consumable_by_legacy(self):
        capture = two_channel_int16(points=32)
        server = self.serve(capture, max_range_bytes=53,
                            supports_lease=False)
        out = self.output()
        env = dict(os.environ, PYTHONPATH=str(SRC))
        ok = subprocess.run(
            [sys.executable, "-m", "signal_spectrum.waveform.cli",
             "--url", server.url(), "--output", str(out),
             "--channel", "0", "--analysis-count", "4"],
            capture_output=True, text=True, env=env)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        payload = json.loads(ok.stdout)
        self.assertEqual(payload["consistency_channel"], "etag")
        analysis = json.loads((out / "analysis.json").read_text())
        # Deterministic legacy consumption.
        report_one = analyze(analysis)
        report_two = analyze(json.loads((out / "analysis.json").read_text()))
        self.assertEqual(json.dumps(report_one, sort_keys=True),
                         json.dumps(report_two, sort_keys=True))
        # All NDJSON values match an independent calibration.
        rows = read_ndjson(out / "samples.ndjson")
        for row in rows:
            i = row["index"]
            self.assertEqual(row["values"], [
                default_raw_fn(i, 0) * 0.001,
                default_raw_fn(i, 1) * 0.002 + 1.5,
            ])
        # Re-running against the published dir fails with code 2.
        again = subprocess.run(
            [sys.executable, "-m", "signal_spectrum.waveform.cli",
             "--url", server.url(), "--output", str(out)],
            capture_output=True, text=True, env=env)
        self.assertEqual(again.returncode, 2)

    def test_cli_rejects_invalid_analysis_arguments(self):
        env = dict(os.environ, PYTHONPATH=str(SRC))
        for args in (["--channel", "0"],
                     ["--analysis-count", "4"],
                     ["--channel", "-1", "--analysis-count", "4"],
                     ["--channel", "0", "--analysis-count", "3"],
                     ["--channel", "0", "--analysis-count", "513"]):
            proc = subprocess.run(
                [sys.executable, "-m", "signal_spectrum.waveform.cli",
                 "--url", "http://127.0.0.1:1", "--output",
                 str(self.output("x")), *args],
                capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 2, args)


if __name__ == "__main__":
    unittest.main()
