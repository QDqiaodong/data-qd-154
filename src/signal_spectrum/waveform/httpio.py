"""Bounded HTTP/1.1 client used by the waveform collector.

Only the standard library is used (``http.client`` over real TCP sockets).
Every read is bounded: JSON control responses must fit in 64 KiB and binary
blocks are streamed through a fixed-size work buffer (never more than the
device-advertised range cap plus the read buffer, always well below the
1 MiB collector budget).  A connection is created per attempt so a half-open
keep-alive socket can never corrupt a retry.
"""
import http.client
from urllib.parse import urlsplit

from .protocol import (
    MAX_JSON_BYTES,
    IdentityChanged,
    LeaseExpired,
    ProtocolError,
    parse_content_range,
    parse_json_object,
    parse_strong_etag,
    validate_capabilities,
    validate_lease,
    validate_meta,
)


class ConnectionFailure(ProtocolError):
    """Socket-level failure or truncated stream; the range may be retried."""


class HttpDevice:
    def __init__(self, base_url, timeout, read_buffer=65536):
        parts = urlsplit(base_url)
        if parts.scheme != "http" or not parts.hostname:
            raise ProtocolError("only http:// URLs with a host are supported")
        if parts.query or parts.fragment:
            raise ProtocolError("device URL must not carry a query or fragment")
        self.host = parts.hostname
        self.port = parts.port or 80
        self.prefix = parts.path.rstrip("/")
        self.timeout = timeout
        self.read_buffer = read_buffer

    def _connect(self):
        return http.client.HTTPConnection(
            self.host, self.port, timeout=self.timeout)

    def _path(self, suffix):
        return self.prefix + suffix

    # -- bounded readers ---------------------------------------------------

    def _read_json(self, response):
        length = response.getheader("Content-Length")
        if length is None:
            raise ProtocolError("JSON control response lacks Content-Length")
        try:
            length = int(length)
        except ValueError:
            raise ProtocolError("non-integer Content-Length") from None
        if length < 0 or length > MAX_JSON_BYTES:
            raise ProtocolError("JSON control response exceeds %d bytes"
                                % MAX_JSON_BYTES)
        if response.getheader("Transfer-Encoding", "").lower() == "chunked":
            raise ProtocolError("chunked control responses are not accepted")
        raw = response.read(length + 1)
        if len(raw) != length:
            raise ConnectionFailure("truncated control response")
        if len(raw) > MAX_JSON_BYTES:
            raise ProtocolError("JSON control response exceeds %d bytes"
                                % MAX_JSON_BYTES)
        return parse_json_object(raw)

    def _drain(self, response, limit=MAX_JSON_BYTES):
        """Drain a bounded error body so framing stays well-defined."""
        try:
            response.read(limit + 1)
        except (http.client.HTTPException, OSError):
            pass

    def _request(self, method, path, headers=None):
        conn = self._connect()
        try:
            conn.request(method, path, headers=headers or {})
            return conn.getresponse()
        except (http.client.HTTPException, OSError) as error:
            conn.close()
            raise ConnectionFailure("connection failed: " + str(error)) from None

    # -- control endpoints -------------------------------------------------

    def get_capabilities(self):
        response = self._request("GET", self._path("/capabilities"))
        try:
            if response.status != 200:
                self._drain(response)
                raise ProtocolError("capabilities returned HTTP %d"
                                    % response.status)
            etag = response.getheader("ETag")
            if etag is not None:
                parse_strong_etag(etag)
            return validate_capabilities(self._read_json(response))
        finally:
            response.close()

    def get_meta(self, lease_token=None):
        path = (self._path("/leases/%s/meta" % lease_token)
                if lease_token else self._path("/capture/meta"))
        response = self._request("GET", path)
        try:
            status = response.status
            if status == 410 and lease_token:
                self._drain(response)
                raise LeaseExpired("lease %s expired" % lease_token)
            if status != 200:
                self._drain(response)
                raise ProtocolError("meta returned HTTP %d" % status)
            etag = parse_strong_etag(response.getheader("ETag"))
            return validate_meta(self._read_json(response)), etag
        finally:
            response.close()

    def create_lease(self):
        response = self._request("POST", self._path("/leases"))
        try:
            status = response.status
            if status == 405:
                self._drain(response)
                return None
            if status != 201:
                self._drain(response)
                raise ProtocolError("lease creation returned HTTP %d" % status)
            return validate_lease(self._read_json(response))
        finally:
            response.close()

    def release_lease(self, token):
        # Best effort; release failures are never allowed to mask the real
        # outcome of the acquisition.
        try:
            response = self._request(
                "DELETE", self._path("/leases/%s" % token))
            response.close()
        except (ProtocolError, http.client.HTTPException, OSError):
            pass

    # -- ranged raw reads --------------------------------------------------

    def read_range(self, start, end, etag, lease_token=None, sink=None):
        """Read one inclusive byte range, streaming into ``sink``.

        Returns ``(response_etag, (start, end, total), bytes_read)`` on a
        fully validated 206 block.  Identity mismatches raise
        :class:`IdentityChanged` (412), expired leases
        :class:`LeaseExpired` (410), framing/protocol violations
        :class:`ProtocolError` and truncated streams
        :class:`ConnectionFailure`.
        """
        path = (self._path("/leases/%s/data" % lease_token)
                if lease_token else self._path("/capture/data"))
        headers = {
            "Range": "bytes=%d-%d" % (start, end),
            "If-Match": etag,
        }
        response = self._request("GET", path, headers=headers)
        try:
            status = response.status
            if status == 412:
                self._drain(response)
                raise IdentityChanged("If-Match precondition failed")
            if status == 410 and lease_token:
                self._drain(response)
                raise LeaseExpired("lease %s expired during data read"
                                   % lease_token)
            if status == 200:
                # A device that ignores Range cannot be consumed as a block.
                self._drain(response)
                raise ProtocolError("device ignored Range and returned 200")
            if status != 206:
                self._drain(response)
                raise ProtocolError("data read returned HTTP %d" % status)

            response_etag = parse_strong_etag(response.getheader("ETag"))
            if response_etag != etag:
                raise ProtocolError("block ETag %s does not match session "
                                    "identity %s" % (response_etag, etag))
            cr = parse_content_range(response.getheader("Content-Range"))
            cr_start, cr_end, cr_total = cr
            if cr_start != start or cr_end != end:
                raise ProtocolError(
                    "Content-Range %d-%d does not match request %d-%d"
                    % (cr_start, cr_end, start, end))

            te = response.getheader("Transfer-Encoding", "").lower()
            if te == "chunked":
                raise ProtocolError("chunked data responses are not accepted")
            declared = response.getheader("Content-Length")
            if declared is None:
                raise ProtocolError("206 response lacks Content-Length")
            try:
                declared = int(declared)
            except ValueError:
                raise ProtocolError("non-integer Content-Length") from None
            expected = end - start + 1
            if declared != expected:
                raise ProtocolError(
                    "Content-Length %d differs from range size %d"
                    % (declared, expected))

            written = 0
            try:
                while written < expected:
                    chunk = response.read(
                        min(self.read_buffer, expected - written))
                    if not chunk:
                        raise ConnectionFailure(
                            "truncated stream at offset %d of %d"
                            % (start + written, end))
                    if written + len(chunk) > expected:
                        raise ProtocolError(
                            "response exceeded the requested range")
                    if sink is not None:
                        sink.write(chunk)
                    written += len(chunk)
                extra = response.read(1)
                if extra:
                    raise ProtocolError(
                        "response exceeded the requested range")
            finally:
                response.close()
            return response_etag, (cr_start, cr_end, cr_total), written
        finally:
            response.close()
