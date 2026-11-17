"""Offline HTTP measurement-device simulator.

The simulator speaks the real protocol over real TCP sockets (``http.server``
on a loopback port); neither the simulator nor any test replaces the HTTP
layer with mock function calls.  Test code drives it through the
:class:`DeviceServer` in-process control API (capture switching, lease
expiry, fault injection, request-log inspection); the same handler is also
available as the ``signal-waveform-device`` console command.

The server keeps a full request log so acceptance tests can prove that a
published package never mixes bytes from different capture identities.
"""
import hashlib
import json
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .protocol import (
    MAX_JSON_BYTES,
    validate_meta,
)


# --------------------------------------------------------------------------
# Capture construction
# --------------------------------------------------------------------------

def canonical_json(obj):
    """Deterministic JSON used for identity hashing (sorted keys, no gaps)."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, separators=(",", ":")).encode("utf-8")


def _encode_value(code, dtype, byte_order):
    prefix = ">" if byte_order == "big" else "<"
    if dtype == "int16":
        return struct.pack(prefix + "h", int(code))
    if dtype == "uint16":
        return struct.pack(prefix + "H", int(code) & 0xFFFF)
    return struct.pack(prefix + "f", float(code))


def build_capture(capture_id, sample_rate, points, dtype, byte_order,
                  channels, raw_fn):
    """Build an immutable capture: interleaved raw bytes plus strict meta.

    ``channels`` is a list of ``(name, scale, offset)`` triples and
    ``raw_fn(point_index, channel_index)`` returns the unscaled code value.
    Bytes are point-major: channel samples of one point are adjacent, in the
    declared channel order.
    """
    if len(channels) < 1 or len(channels) > 16:
        raise ValueError("channels must be 1..16")
    frame = bytearray()
    for point in range(points):
        for channel, (_, _, _) in enumerate(channels):
            frame += _encode_value(raw_fn(point, channel), dtype, byte_order)
    raw = bytes(frame)
    digest = hashlib.sha256(raw).hexdigest()
    meta = {
        "capture_id": capture_id,
        "sample_rate": sample_rate,
        "points": points,
        "dtype": dtype,
        "byte_order": byte_order,
        "channels": [
            {"name": name, "scale": scale, "offset": offset}
            for (name, scale, offset) in channels
        ],
        "raw_bytes": len(raw),
        "sha256": digest,
    }
    return Capture(raw, validate_meta(meta))


def default_raw_fn(point, channel):
    """Deterministic small signed ramp used by the standalone command."""
    return ((point * 7 + channel * 13) % 2000) - 1000


class Capture:
    """One immutable raw capture and its joint strong identity."""

    def __init__(self, raw, meta):
        self.raw = raw
        self.meta = meta
        identity = canonical_json(meta) + raw
        self.etag = '"' + hashlib.sha256(identity).hexdigest()[:32] + '"'

    def slice(self, start, end):
        return self.raw[start:end + 1]


# --------------------------------------------------------------------------
# Thread-safe device state
# --------------------------------------------------------------------------

class Lease:
    def __init__(self, token, capture, expires_at):
        self.token = token
        self.capture = capture
        self.expires_at = expires_at


class DeviceState:
    def __init__(self, capabilities_lease, lease_ttl):
        self.lock = threading.RLock()
        self.current = None
        self.leases = {}
        self.lease_counter = 0
        self.supports_lease = capabilities_lease
        self.lease_ttl = lease_ttl
        # Fault injection knobs (see the contract test section).
        self.faults = set()
        # Deterministic one-shot faults: fault name -> number of *data*
        # requests on which it is applied before being removed automatically.
        self.fault_repeats = {}
        self.pending_swap = None
        self.lease_expire_after = None
        self.auto_alternate = None
        self.rotate_every = 0
        self.request_count = 0
        self.fragment = 1
        self.log = []

    # -- capture management ------------------------------------------------

    def set_capture(self, capture):
        with self.lock:
            self.current = capture

    def swap_capture(self, capture, keep_etag=False):
        """Install a new capture; optionally maliciously keep the old ETag."""
        with self.lock:
            old_etag = self.current.etag if self.current else None
            self.current = capture
            if keep_etag and old_etag is not None:
                # Deliberately broken device: identity header is frozen while
                # the bytes/calibration changed.  Clients must still catch it.
                self.current.etag = old_etag
            return old_etag

    # -- leases ------------------------------------------------------------

    def create_lease(self):
        if not self.supports_lease:
            return None
        with self.lock:
            self.lease_counter += 1
            token = "lease-%d" % self.lease_counter
            self.leases[token] = Lease(
                token, self.current, time.monotonic() + self.lease_ttl)
            return token

    def get_lease(self, token):
        with self.lock:
            lease = self.leases.get(token)
            if lease is None or lease.expires_at <= time.monotonic():
                return None
            return lease

    def release_lease(self, token):
        with self.lock:
            self.leases.pop(token, None)

    def expire_leases(self):
        with self.lock:
            now = time.monotonic()
            for token in [t for t, lease in self.leases.items()
                          if lease.expires_at > now]:
                self.leases[token].expires_at = now

    # -- request bookkeeping ----------------------------------------------

    def record(self, entry):
        with self.lock:
            self.request_count += 1
            entry["seq"] = self.request_count
            self.log.append(entry)

    def maybe_rotate(self, rotate_to):
        """Swap to ``rotate_to`` every rotate_every data requests."""
        if not self.rotate_every or rotate_to is None:
            return
        with self.lock:
            data_requests = [e for e in self.log if e.get("kind") == "data"]
            if data_requests and len(data_requests) % self.rotate_every == 0:
                if self.current is not rotate_to:
                    self.set_capture(rotate_to)

    def swap_after_data_requests(self, capture, count, keep_etag=False):
        """Deterministically swap capture before the (count+1)-th data read."""
        with self.lock:
            self.pending_swap = {
                "capture": capture,
                # Swap once ``count`` data requests have already been served.
                "after": count + 1,
                "keep_etag": keep_etag,
            }

    def apply_pending_swap(self):
        with self.lock:
            pending = getattr(self, "pending_swap", None)
            if pending is None:
                return
            data_requests = [e for e in self.log if e.get("kind") == "data"]
            if len(data_requests) >= pending["after"]:
                self.swap_capture(pending["capture"],
                                  keep_etag=pending["keep_etag"])
                self.pending_swap = None

    def apply_auto_alternate(self):
        """Test control: flip between two captures before each data read."""
        pair = getattr(self, "auto_alternate", None)
        if not pair:
            return
        with self.lock:
            self.current = pair[1] if self.current is pair[0] else pair[0]


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------

class DeviceHandler(BaseHTTPRequestHandler):
    server_version = "WaveformDevice/1.0"
    protocol_version = "HTTP/1.1"

    # Quieter test output; the request log is the real evidence.
    def log_message(self, fmt, *args):
        return

    # -- helpers -----------------------------------------------------------

    @property
    def state(self):
        return self.server.device_state

    def _send_json(self, status, payload, extra_headers=None):
        body = canonical_json(payload)
        if len(body) > MAX_JSON_BYTES:
            body = b'{"error":"response too large"}'
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for key, value in extra_headers:
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status, message):
        self._send_json(status, {"error": message})

    def _parse_range(self):
        header = self.headers.get("Range")
        if not header or not header.startswith("bytes="):
            return None
        spec = header[len("bytes="):]
        if "," in spec or "-" not in spec:
            return None
        start_text, end_text = spec.split("-", 1)
        try:
            start = int(start_text)
            end = int(end_text)
        except ValueError:
            return None
        if start < 0 or end < start:
            return None
        return start, end

    def _if_match_matches(self, etag):
        if_match = self.headers.get("If-Match")
        if if_match is None:
            return True, if_match
        return if_match == etag, if_match

    def _apply_repeat_faults(self):
        """Activate count-limited faults deterministically per data request."""
        repeats = self.state.fault_repeats
        for fault in list(repeats):
            if repeats[fault] > 0:
                self.state.faults.add(fault)
                repeats[fault] -= 1
            else:
                self.state.faults.discard(fault)
                del repeats[fault]

    def _maybe_expire_leases(self, kind):
        after = self.state.lease_expire_after
        if after is None or kind not in ("data", "lease_data"):
            return
        data_requests = [e for e in self.state.log
                         if e.get("kind") in ("data", "lease_data")]
        if len(data_requests) >= after:
            self.state.expire_leases()
            self.state.lease_expire_after = None

    def _write_body(self, body, declare_length=None, truncate=False):
        """Write a binary body, optionally fragmented, truncated or over-long."""
        declared = len(body) if declare_length is None else declare_length
        self.send_header("Content-Length", str(declared))
        self.end_headers()
        faults = self.state.faults
        if "truncate" in faults:
            cut = max(1, len(body) // 3)
            body = body[:cut]
            truncate = True
        frag = max(1, self.state.fragment)
        for offset in range(0, len(body), frag):
            self.wfile.write(body[offset:offset + frag])
            self.wfile.flush()
        if truncate:
            # Make the early EOF visible instead of keep-alive framing.
            self.close_connection = True

    def _send_data(self, capture, kind):
        requested = self._parse_range()
        if requested is None:
            # The protocol only defines ranged reads for raw payloads.
            self._send_error_json(416, "a single bytes=a-b Range is required")
            return
        start, end = requested
        total = len(capture.raw)
        if start >= total:
            self._send_error_json(416, "range starts beyond the payload")
            return
        end = min(end, total - 1)
        etag = capture.etag
        matches, if_match = self._if_match_matches(etag)
        self._apply_repeat_faults()
        # Read faults only after the repeat controller has updated the set.
        faults = self.state.faults
        self.state.record({
            "kind": kind, "method": "GET", "path": self.path,
            "range": [start, end], "if_match": if_match,
            "etag": etag, "capture_id": capture.meta["capture_id"],
        })
        self._maybe_expire_leases(kind)
        if not matches:
            self.send_response(412)
            self.send_header("Content-Length", "0")
            self.send_header("ETag", etag)
            self.end_headers()
            return
        body = capture.slice(start, end)
        expected_len = end - start + 1

        if "ignore_range" in faults:
            # Misbehaving device ignores Range and answers with a full 200.
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("ETag", etag)
            self._write_body(capture.raw)
            return

        self.send_response(206)
        self.send_header("Content-Type", "application/octet-stream")
        # ETag fault injection.
        if "missing_etag" not in faults:
            if "weak_etag" in faults:
                self.send_header("ETag", "W/" + etag)
            elif "conflicting_etag" in faults:
                self.send_header("ETag", '"' + "f" * 32 + '"')
            else:
                self.send_header("ETag", etag)
        cr_start, cr_end, cr_total = start, end, total
        if "bad_content_range" in faults:
            cr_start = start + 1
            cr_total = total + 1
        self.send_header("Content-Range",
                         "bytes %d-%d/%d" % (cr_start, cr_end, cr_total))
        if "excess_bytes" in faults:
            # Header framing exposes more bytes than the range promised.
            self._write_body(body + b"\x00\x00\x00",
                             declare_length=len(body) + 3)
        elif "tamper_body" in faults:
            # Keep the declared ETag/framing but corrupt every delivered byte:
            # a client that trusts headers alone would accept mixed bytes.
            tampered = bytes((byte ^ 0x5A) for byte in body)
            self._write_body(tampered)
        else:
            self._write_body(body)

    def _send_meta(self, capture, kind):
        matches, if_match = self._if_match_matches(capture.etag)
        self.state.record({
            "kind": kind, "method": "GET", "path": self.path,
            "if_match": if_match, "etag": capture.etag,
            "capture_id": capture.meta["capture_id"],
        })
        if not matches:
            self.send_response(412)
            self.send_header("Content-Length", "0")
            self.send_header("ETag", capture.etag)
            self.end_headers()
            return
        self._send_json(200, capture.meta, [("ETag", capture.etag)])

    # -- routing -----------------------------------------------------------

    def do_GET(self):
        try:
            path = urlsplit(self.path).path
            state = self.state
            if path == "/capabilities":
                state.record({"kind": "capabilities", "method": "GET",
                              "path": path})
                self._send_json(200, {
                    "max_range_bytes": self.server.max_range_bytes,
                    "lease": state.supports_lease,
                })
                return
            if path == "/capture/meta":
                with state.lock:
                    capture = state.current
                if capture is None:
                    self._send_error_json(409, "device is not sampling")
                    return
                self._send_meta(capture, "meta")
                return
            if path == "/capture/data":
                with state.lock:
                    state.maybe_rotate(self.server.rotate_to)
                    state.apply_auto_alternate()
                    capture = state.current
                if capture is None:
                    self._send_error_json(409, "device is not sampling")
                    return
                self._send_data(capture, "data")
                with state.lock:
                    # Swap after the response was dispatched and logged, so
                    # the first N data requests see the original identity.
                    state.apply_pending_swap()
                return
            if path.startswith("/leases/"):
                rest = path[len("/leases/"):]
                parts = [p for p in rest.split("/") if p]
                if len(parts) != 2 or parts[1] not in ("meta", "data"):
                    self._send_error_json(404, "unknown lease path")
                    return
                token, endpoint = parts
                lease = state.get_lease(token)
                if lease is None:
                    self._send_error_json(410, "lease expired or unknown")
                    return
                if endpoint == "meta":
                    self._send_meta(lease.capture, "lease_meta")
                else:
                    self._send_data(lease.capture, "lease_data")
                return
            self._send_error_json(404, "unknown path: " + path)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def do_POST(self):
        try:
            path = urlsplit(self.path).path
            state = self.state
            if path != "/leases":
                self._send_error_json(404, "unknown path: " + path)
                return
            state.record({"kind": "lease_create", "method": "POST", "path": path})
            if not state.supports_lease:
                self._send_error_json(405, "device does not support leases")
                return
            # Drain any request body so framing stays consistent.
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(min(length, MAX_JSON_BYTES))
            token = state.create_lease()
            self._send_json(201, {"token": token})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def do_DELETE(self):
        try:
            path = urlsplit(self.path).path
            state = self.state
            if not path.startswith("/leases/"):
                self._send_error_json(404, "unknown path: " + path)
                return
            token = path[len("/leases/"):].strip("/")
            state.record({"kind": "lease_release", "method": "DELETE",
                          "path": path, "token": token})
            # Releasing is idempotent, including an unknown/expired token.
            state.release_lease(token)
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True


# --------------------------------------------------------------------------
# Server wrapper / test control API
# --------------------------------------------------------------------------

class DeviceServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, capture, max_range_bytes=4096, supports_lease=True,
                 lease_ttl=30.0, host="127.0.0.1", port=0):
        self.max_range_bytes = max_range_bytes
        self.device_state = DeviceState(supports_lease, lease_ttl)
        self.device_state.set_capture(capture)
        self.rotate_to = None
        super().__init__((host, port), DeviceHandler)
        self._thread = threading.Thread(
            target=self.serve_forever, name="waveform-device", daemon=True)

    def start(self):
        self._thread.start()
        return self.url()

    def url(self):
        host, port = self.server_address
        return "http://%s:%d" % (host, port)

    def stop(self):
        self.shutdown()
        self.server_close()
        self._thread.join(timeout=5)

    # -- control API used by tests ----------------------------------------

    def set_capture(self, capture):
        self.device_state.set_capture(capture)

    def swap_capture(self, capture, keep_etag=False):
        return self.device_state.swap_capture(capture, keep_etag=keep_etag)

    def set_faults(self, faults):
        with self.device_state.lock:
            self.device_state.faults = set(faults)

    def add_fault(self, fault):
        with self.device_state.lock:
            self.device_state.faults.add(fault)

    def add_repeated_fault(self, fault, data_request_count):
        """Apply ``fault`` to the next N data requests, then clear it."""
        with self.device_state.lock:
            self.device_state.fault_repeats[fault] = int(data_request_count)

    def expire_leases_after_data_requests(self, count):
        """Expire all leases deterministically after N data requests."""
        with self.device_state.lock:
            self.device_state.lease_expire_after = int(count)

    def clear_faults(self):
        with self.device_state.lock:
            self.device_state.faults.clear()

    def set_fragment(self, size):
        self.device_state.fragment = max(1, int(size))

    def set_rotate_every(self, count):
        self.device_state.rotate_every = int(count)

    def set_rotation_target(self, capture):
        """Capture swapped in automatically every rotate_every data reads."""
        self.rotate_to = capture

    def swap_after_data_requests(self, capture, count, keep_etag=False):
        """Swap capture deterministically before data request ``count + 1``."""
        self.device_state.swap_after_data_requests(
            capture, count, keep_etag=keep_etag)

    def expire_leases(self):
        self.device_state.expire_leases()

    def request_log(self):
        with self.device_state.lock:
            return list(self.device_state.log)

    def leases(self):
        with self.device_state.lock:
            return dict(self.device_state.leases)
