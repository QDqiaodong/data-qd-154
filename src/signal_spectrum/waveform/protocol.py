"""Strict wire-protocol primitives shared by the client and the simulator.

Everything in here is pure data validation: no sockets, no filesystem.  The
protocol itself is documented in ``docs/waveform-fetch-contract.md``.

Rules enforced for every JSON control response:

* exactly the documented fields, no unknown keys;
* duplicate JSON keys, ``NaN``/``Infinity`` constants are rejected while
  parsing;
* booleans are never accepted where integers/numbers are required;
* all numeric bounds from the contract are checked;
* the whole control response must fit in :data:`MAX_JSON_BYTES` (64 KiB).
"""
import json
import math
import re

MAX_JSON_BYTES = 64 * 1024
MAX_RANGE_CAP = 65_536
MIN_RANGE_CAP = 1
MAX_RAW_BYTES = 256 * 1024 * 1024
MIN_POINTS = 4
MAX_CHANNELS = 16
SAMPLE_RATE_MAX = 10**9
ANALYSIS_MIN = 4
ANALYSIS_MAX = 512
# The legacy single-segment DFT only accepts magnitudes up to 10**9.
ANALYSIS_ABS_LIMIT = 10**9

DTYPES = ("int16", "uint16", "float32")
BYTE_ORDERS = ("little", "big")
WIDTH = {"int16": 2, "uint16": 2, "float32": 4}
_HEX64 = re.compile(r"[0-9a-fA-F]{64}\Z")
# A token is one path segment: no slash and no control characters.
_TOKEN_BAD = re.compile(r"[\x00-\x1f\x7f/]")


class ProtocolError(ValueError):
    """A response violated the device protocol; retrying cannot repair it."""


class IdentityChanged(ProtocolError):
    """The remote capture identity (strong ETag) changed mid-acquisition."""


class LeaseExpired(ProtocolError):
    """A leased immutable snapshot has expired (HTTP 410)."""


class BudgetExceeded(ProtocolError):
    """A finite retry/restart budget has been exhausted."""


class CheckpointError(ProtocolError):
    """Local committed state is missing, unparseable or has been tampered."""


def parse_json_object(raw):
    """Parse a bounded JSON byte string with the project's strict rules."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_JSON_BYTES:
        raise ProtocolError("JSON control response exceeds %d bytes" % MAX_JSON_BYTES)

    def unique(pairs):
        seen = set()
        for key, value in pairs:
            if key in seen:
                raise ProtocolError("duplicate JSON key: " + str(key))
            seen.add(key)
        return dict(pairs)

    def reject_constant(value):
        raise ProtocolError("non-finite JSON constant is not allowed: " + value)

    try:
        obj = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError("invalid JSON response: " + str(error)) from None
    if not isinstance(obj, dict):
        raise ProtocolError("JSON response must be an object")
    return obj


def require_exact_keys(obj, expected, label):
    if not isinstance(obj, dict):
        raise ProtocolError("%s must be a JSON object" % label)
    if set(obj) != set(expected):
        raise ProtocolError(
            "%s must contain exactly %s, got %s"
            % (label, ",".join(sorted(expected)), ",".join(sorted(obj)))
        )


def strict_int(value, minimum, maximum, label):
    if type(value) is not int:  # bool is explicitly not an int here
        raise ProtocolError("%s must be an integer" % label)
    if not minimum <= value <= maximum:
        raise ProtocolError("%s outside allowed range %d..%d" % (label, minimum, maximum))
    return value


def strict_bool(value, label):
    if type(value) is not bool:
        raise ProtocolError("%s must be a boolean" % label)
    return value


def strict_finite(value, label):
    if type(value) not in (int, float) or type(value) is bool:
        raise ProtocolError("%s must be a finite number" % label)
    if not math.isfinite(value):
        raise ProtocolError("%s must be finite" % label)
    return value


def strict_name(value, label):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ProtocolError("%s must be a non-empty identifier" % label)
    if len(value) > 128:
        raise ProtocolError("%s is longer than 128 characters" % label)
    return value


def validate_capabilities(obj):
    require_exact_keys(obj, ("max_range_bytes", "lease"), "capabilities")
    cap = strict_int(obj["max_range_bytes"], MIN_RANGE_CAP, MAX_RANGE_CAP,
                     "capabilities.max_range_bytes")
    lease = strict_bool(obj["lease"], "capabilities.lease")
    return {"max_range_bytes": cap, "lease": lease}


def validate_channel(obj, index):
    label = "channels[%d]" % index
    require_exact_keys(obj, ("name", "scale", "offset"), label)
    name = strict_name(obj["name"], label + ".name")
    scale = strict_finite(obj["scale"], label + ".scale")
    offset = strict_finite(obj["offset"], label + ".offset")
    return {"name": name, "scale": scale, "offset": offset}


def validate_meta(obj):
    """Validate a capture metadata object and return a normalized copy."""
    require_exact_keys(
        obj,
        ("capture_id", "sample_rate", "points", "dtype", "byte_order",
         "channels", "raw_bytes", "sha256"),
        "capture meta",
    )
    capture_id = strict_name(obj["capture_id"], "capture_id")
    sample_rate = strict_int(obj["sample_rate"], 1, SAMPLE_RATE_MAX, "sample_rate")
    points = strict_int(obj["points"], MIN_POINTS, 10**12, "points")
    dtype = obj["dtype"]
    if dtype not in DTYPES:
        raise ProtocolError("dtype must be one of " + ",".join(DTYPES))
    byte_order = obj["byte_order"]
    if byte_order not in BYTE_ORDERS:
        raise ProtocolError("byte_order must be little or big")
    channels = obj["channels"]
    if type(channels) is not list or not (1 <= len(channels) <= MAX_CHANNELS):
        raise ProtocolError("channels must be a list of 1..%d objects" % MAX_CHANNELS)
    normalized_channels = [validate_channel(ch, i) for i, ch in enumerate(channels)]
    names = [ch["name"] for ch in normalized_channels]
    if len(set(names)) != len(names):
        raise ProtocolError("channel names must be unique")
    width = WIDTH[dtype]
    expected_bytes = points * len(channels) * width
    raw_bytes = strict_int(obj["raw_bytes"], expected_bytes, expected_bytes, "raw_bytes")
    if raw_bytes > MAX_RAW_BYTES:
        raise ProtocolError("raw_bytes exceeds the 256 MiB acquisition limit")
    sha = obj["sha256"]
    if not isinstance(sha, str) or _HEX64.match(sha) is None:
        raise ProtocolError("sha256 must be 64 hexadecimal characters")
    return {
        "capture_id": capture_id,
        "sample_rate": sample_rate,
        "points": points,
        "dtype": dtype,
        "byte_order": byte_order,
        "channels": normalized_channels,
        "raw_bytes": raw_bytes,
        "sha256": sha.lower(),
    }


def validate_lease(obj):
    require_exact_keys(obj, ("token",), "lease response")
    token = obj["token"]
    if not isinstance(token, str) or not token or _TOKEN_BAD.search(token):
        raise ProtocolError("lease token must be a non-empty string without "
                            "slashes or control characters")
    return token


def parse_strong_etag(header):
    """Return the opaque strong ETag string, including its double quotes.

    Weak tags (``W/"..."``) and missing tags are rejected because the whole
    consistency design relies on an entity tag that changes whenever any byte
    of the joint identity changes.
    """
    if header is None or not isinstance(header, str) or not header:
        raise ProtocolError("response is missing a strong ETag")
    if header.startswith(("W/", "w/")):
        raise ProtocolError("weak ETag is not accepted: " + header)
    if len(header) < 2 or not (header.startswith('"') and header.endswith('"')):
        raise ProtocolError("malformed ETag, expected quoted strong validator")
    if any(ord(c) < 32 for c in header):
        raise ProtocolError("ETag contains control characters")
    return header


def parse_content_range(header):
    """Parse ``bytes start-end/total`` from a 206 response."""
    if not isinstance(header, str) or not header:
        raise ProtocolError("206 response is missing Content-Range")
    match = _CONTENT_RANGE_RE.match(header)
    if match is None:
        raise ProtocolError("malformed Content-Range: " + header)
    start, end, total = (int(match.group(1)), int(match.group(2)),
                         int(match.group(3)))
    if start < 0 or end < start or total <= end:
        raise ProtocolError("inconsistent Content-Range: " + header)
    return start, end, total


import re as _re

_CONTENT_RANGE_RE = _re.compile(r"bytes (\d+)-(\d+)/(\d+)\Z")
