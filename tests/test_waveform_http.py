"""Real TCP/HTTP protocol tests against the bundled device simulator.

Every request here goes through a loopback socket via :class:`HttpDevice`;
nothing mocks the HTTP layer.  They cover the joint strong-ETag identity,
conditional reads (If-Match/412), strict 206/Content-Range handling and the
lease lifecycle including expiry (410) and idempotent release.
"""
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from signal_spectrum.waveform.device import (
    DeviceServer, build_capture, default_raw_fn)
from signal_spectrum.waveform.httpio import HttpDevice
from signal_spectrum.waveform.protocol import (
    IdentityChanged,
    LeaseExpired,
    ProtocolError,
)


def make_capture(cid="cap-1", points=20, channels=(("a", 1.0, 0.0),
                                                   ("b", 2.0, 0.0)),
                 dtype="int16", byte_order="little"):
    return build_capture(cid, 1000, points, dtype, byte_order,
                         list(channels), default_raw_fn)


class HttpProtocol(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.capture = make_capture()
        self.server = DeviceServer(self.capture, max_range_bytes=64,
                                   supports_lease=True, lease_ttl=30)
        self.url = self.server.start()
        self.client = HttpDevice(self.url, timeout=5)

    def tearDown(self):
        self.server.stop()
        self.temp.cleanup()

    def test_capabilities_and_meta_identity(self):
        caps = self.client.get_capabilities()
        self.assertEqual(caps, {"max_range_bytes": 64, "lease": True})
        meta, etag = self.client.get_meta()
        self.assertEqual(meta["capture_id"], "cap-1")
        self.assertTrue(etag.startswith('"') and not etag.startswith("W/"))
        # Same identity yields identical meta and ETag.
        again_meta, again_etag = self.client.get_meta()
        self.assertEqual(again_meta, meta)
        self.assertEqual(again_etag, etag)

    def test_range_206_content_range_and_bytes(self):
        meta, etag = self.client.get_meta()
        sink_path = Path(self.temp.name) / "block.bin"
        with open(sink_path, "wb") as sink:
            _, (start, end, total), length = self.client.read_range(
                0, 9, etag, sink=sink)
        self.assertEqual((start, end, total), (0, 9, meta["raw_bytes"]))
        self.assertEqual(length, 10)
        self.assertEqual(sink_path.read_bytes(), self.capture.raw[:10])

    def test_if_match_precondition_412(self):
        _, etag = self.client.get_meta()
        stale = '"' + "d" * 32 + '"'
        with self.assertRaises(IdentityChanged):
            self.client.read_range(0, 9, stale)
        # Meta conditional reads must also honour If-Match.
        response = self.client._request(
            "GET", "/capture/meta", headers={"If-Match": stale})
        try:
            self.assertEqual(response.status, 412)
        finally:
            response.close()

    def test_lease_snapshot_is_immutable_after_swap(self):
        token = self.client.create_lease()
        self.assertTrue(token.startswith("lease-"))
        leased_meta, leased_etag = self.client.get_meta(lease_token=token)
        # The live device moves to a new capture; the leased snapshot must not.
        new_capture = make_capture("cap-2", points=12)
        self.server.set_capture(new_capture)
        live_meta, live_etag = self.client.get_meta()
        self.assertNotEqual(live_etag, leased_etag)
        snapshot_meta, snapshot_etag = self.client.get_meta(lease_token=token)
        self.assertEqual(snapshot_meta, leased_meta)
        self.assertEqual(snapshot_etag, leased_etag)
        sink_path = Path(self.temp.name) / "leased.bin"
        with open(sink_path, "wb") as sink:
            self.client.read_range(0, 7, leased_etag,
                                   lease_token=token, sink=sink)
        self.assertEqual(sink_path.read_bytes(), self.capture.raw[:8])

    def test_lease_expiry_returns_410_and_release_idempotent(self):
        token = self.client.create_lease()
        self.client.get_meta(lease_token=token)
        self.server.expire_leases()
        with self.assertRaises(LeaseExpired):
            self.client.get_meta(lease_token=token)
        _, etag = self.client.get_meta()
        with self.assertRaises(LeaseExpired):
            self.client.read_range(0, 3, etag, lease_token=token)
        # Repeated DELETE must be safe, even for an expired/unknown token.
        self.client.release_lease(token)
        self.client.release_lease(token)
        self.client.release_lease("does-not-exist")

    def test_etag_only_device_refuses_lease_creation(self):
        etag_only = DeviceServer(make_capture(), max_range_bytes=32,
                                 supports_lease=False)
        etag_only.start()
        try:
            client = HttpDevice(etag_only.url(), timeout=5)
            self.assertFalse(client.get_capabilities()["lease"])
            self.assertIsNone(client.create_lease())
        finally:
            etag_only.stop()

    def test_etag_changes_when_bytes_change_even_same_capture_id(self):
        _, first_etag = self.client.get_meta()
        swapped = make_capture("cap-1", points=9)  # same id, new layout/bytes
        self.server.set_capture(swapped)
        _, second_etag = self.client.get_meta()
        self.assertNotEqual(first_etag, second_etag)

    # -- malicious / broken response handling ------------------------------

    def test_ignored_range_200_is_fatal(self):
        _, etag = self.client.get_meta()
        self.server.add_fault("ignore_range")
        with self.assertRaises(ProtocolError):
            self.client.read_range(0, 4, etag)

    def test_bad_content_range_is_fatal(self):
        _, etag = self.client.get_meta()
        self.server.add_fault("bad_content_range")
        with self.assertRaises(ProtocolError):
            self.client.read_range(0, 4, etag)

    def test_excess_bytes_are_fatal(self):
        _, etag = self.client.get_meta()
        self.server.add_fault("excess_bytes")
        sink_path = Path(self.temp.name) / "x.bin"
        with open(sink_path, "wb") as sink:
            with self.assertRaises(ProtocolError):
                self.client.read_range(0, 4, etag, sink=sink)

    def test_missing_weak_conflicting_etag_are_fatal(self):
        for fault in ("missing_etag", "weak_etag", "conflicting_etag"):
            self.server.clear_faults()
            _, etag = self.client.get_meta()
            self.server.add_fault(fault)
            with self.assertRaises(ProtocolError):
                self.client.read_range(0, 4, etag)

    def test_truncated_stream_is_a_retryable_connection_failure(self):
        from signal_spectrum.waveform.httpio import ConnectionFailure
        _, etag = self.client.get_meta()
        self.server.set_fragment(2)
        self.server.add_fault("truncate")
        sink_path = Path(self.temp.name) / "t.bin"
        with open(sink_path, "wb") as sink:
            with self.assertRaises(ConnectionFailure):
                self.client.read_range(0, 15, etag, sink=sink)
        # Clearing the fault makes the identical range succeed, proving the
        # client contract for retrying the same range after a truncated read.
        self.server.clear_faults()
        with open(sink_path, "wb") as sink:
            _, _, length = self.client.read_range(0, 15, etag, sink=sink)
        self.assertEqual(length, 16)

    def test_request_log_proves_conditional_reads(self):
        _, etag = self.client.get_meta()
        self.client.read_range(0, 3, etag)
        self.client.read_range(4, 7, etag)
        entries = [e for e in self.server.request_log() if e["kind"] == "data"]
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(e["if_match"] == etag for e in entries))
        self.assertEqual([e["range"] for e in entries],
                         [[0, 3], [4, 7]])

    def test_control_json_is_bounded_to_64kib(self):
        # A hostile or broken control endpoint must never make the client
        # buffer without limit.  Valid metadata under the documented field
        # constraints cannot reach 64 KiB, so this speaks raw HTTP to serve an
        # oversized body and asserts the bounded reader rejects it.
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from signal_spectrum.waveform.protocol import MAX_JSON_BYTES

        body = b'{"max_range_bytes": 100, "lease": false, "pad": "' \
            + b"z" * (MAX_JSON_BYTES + 100) + b'"}'

        class OversizedHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = HTTPServer(("127.0.0.1", 0), OversizedHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            client = HttpDevice("http://%s:%d" % (host, port), timeout=5)
            with self.assertRaises(ProtocolError):
                client.get_capabilities()
            # The strict JSON parser independently enforces the same ceiling.
            from signal_spectrum.waveform.protocol import parse_json_object
            with self.assertRaises(ProtocolError):
                parse_json_object(b"x" * (MAX_JSON_BYTES + 1))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
