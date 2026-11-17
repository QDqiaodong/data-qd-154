"""Strict reader for schema_version=1 waveform package manifests.

The build side of ``signal-excursions`` trusts only two files inside a
published package: ``manifest.json`` and ``raw.bin``.  The manifest is the
*package* manifest exactly as the acquisition pipeline publishes it (twelve
top-level fields, ``schema_version`` 1) -- it is not the remote device meta
object.  Besides per-field validation this module re-derives the canonical
meta fingerprint from the manifest's own ``config``/``channels``/
``raw_bytes``/``raw_sha256`` and requires it to equal the published
``meta_sha256``, so an internally inconsistent manifest can never seed an
archive.
"""
import hashlib
import json
import re

from ..waveform import protocol as wp
from .errors import ExcursionError

MANIFEST_NAME = "manifest.json"
RAW_NAME = "raw.bin"
MANIFEST_MAX_BYTES = wp.MAX_JSON_BYTES  # 64 KiB, same bound as control JSON

MANIFEST_KEYS = (
    "schema_version", "source_url", "capabilities", "consistency_channel",
    "etag", "capture_id", "config", "channels", "raw_bytes", "raw_sha256",
    "meta_sha256", "analysis",
)
CONFIG_KEYS = ("sample_rate", "points", "dtype", "byte_order")
CAPABILITY_KEYS = ("max_range_bytes", "lease")
CHANNEL_KEYS = ("name", "scale", "offset")
ANALYSIS_KEYS = ("channel", "count", "file")

_HEX64 = re.compile(r"[0-9a-fA-F]{64}\Z")


def _fail(message):
    raise ExcursionError(message)


def _checked(callable_, *args):
    """Run a waveform-protocol validator, re-raising as ExcursionError."""
    try:
        return callable_(*args)
    except wp.ProtocolError as error:
        raise ExcursionError(str(error)) from None


def _exact_keys(obj, expected, label):
    _checked(wp.require_exact_keys, obj, expected, label)


def _strict_int(value, minimum, maximum, label):
    return _checked(wp.strict_int, value, minimum, maximum, label)


def _strict_name(value, label):
    return _checked(wp.strict_name, value, label)


def _strict_finite(value, label):
    return _checked(wp.strict_finite, value, label)


def _hex_digest(value, label):
    if not isinstance(value, str) or _HEX64.match(value) is None:
        _fail("%s must be 64 hexadecimal characters" % label)
    return value.lower()


def parse_manifest_bytes(raw):
    """Parse bounded manifest bytes with the project's strict JSON rules."""
    if not isinstance(raw, (bytes, bytearray)):
        _fail("manifest bytes expected")
    if len(raw) > MANIFEST_MAX_BYTES:
        _fail("manifest exceeds %d bytes" % MANIFEST_MAX_BYTES)

    def unique(pairs):
        seen = set()
        for key, value in pairs:
            if key in seen:
                _fail("duplicate JSON key in manifest: " + str(key))
            seen.add(key)
        return dict(pairs)

    def reject_constant(value):
        _fail("non-finite JSON constant in manifest: " + value)

    try:
        obj = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExcursionError("invalid manifest JSON: " + str(error)) from None
    if not isinstance(obj, dict):
        _fail("manifest must be a JSON object")
    return obj


def meta_fingerprint(meta):
    """Canonical SHA256 of a normalized meta object (collector-compatible)."""
    return hashlib.sha256(json.dumps(
        meta, sort_keys=True, ensure_ascii=False, allow_nan=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def _validate_config(config):
    _exact_keys(config, CONFIG_KEYS, "manifest.config")
    sample_rate = _strict_int(config["sample_rate"], 1, wp.SAMPLE_RATE_MAX,
                              "config.sample_rate")
    points = _strict_int(config["points"], wp.MIN_POINTS, 10**12,
                         "config.points")
    dtype = config["dtype"]
    if dtype not in wp.DTYPES:
        _fail("config.dtype must be one of " + ",".join(wp.DTYPES))
    byte_order = config["byte_order"]
    if byte_order not in wp.BYTE_ORDERS:
        _fail("config.byte_order must be little or big")
    return {"sample_rate": sample_rate, "points": points, "dtype": dtype,
            "byte_order": byte_order}


def _validate_channels(channels):
    if type(channels) is not list or not (1 <= len(channels) <= wp.MAX_CHANNELS):
        _fail("manifest.channels must be a list of 1..%d objects"
              % wp.MAX_CHANNELS)
    normalized = []
    for index, channel in enumerate(channels):
        label = "channels[%d]" % index
        _exact_keys(channel, CHANNEL_KEYS, label)
        name = _strict_name(channel["name"], label + ".name")
        scale = _strict_finite(channel["scale"], label + ".scale")
        offset = _strict_finite(channel["offset"], label + ".offset")
        normalized.append({"name": name, "scale": scale, "offset": offset})
    names = [channel["name"] for channel in normalized]
    if len(set(names)) != len(names):
        _fail("manifest channel names must be unique")
    return normalized


def _validate_analysis(analysis, channel_count, points):
    if analysis is None:
        return None
    _exact_keys(analysis, ANALYSIS_KEYS, "manifest.analysis")
    channel = _strict_int(analysis["channel"], 0, channel_count - 1,
                          "analysis.channel")
    count = _strict_int(analysis["count"], wp.ANALYSIS_MIN,
                        min(wp.ANALYSIS_MAX, points), "analysis.count")
    if analysis["file"] != "analysis.json":
        _fail("analysis.file must be 'analysis.json'")
    return {"channel": channel, "count": count, "file": "analysis.json"}


def validate_manifest(obj):
    """Validate a parsed schema_version=1 package manifest.

    Returns a normalized dict with the fields the archive needs.  Raises
    :class:`ExcursionError` on any violation, including an ``meta_sha256``
    that does not match the fingerprint re-derived from the manifest's own
    declared configuration.
    """
    _exact_keys(obj, MANIFEST_KEYS, "manifest")
    schema_version = obj["schema_version"]
    if type(schema_version) is not int or schema_version != 1:
        _fail("unsupported manifest schema_version: %r" % (schema_version,))

    source_url = obj["source_url"]
    if not isinstance(source_url, str) or not source_url:
        _fail("manifest.source_url must be a non-empty string")

    capabilities = obj["capabilities"]
    _exact_keys(capabilities, CAPABILITY_KEYS, "manifest.capabilities")
    _strict_int(capabilities["max_range_bytes"], wp.MIN_RANGE_CAP,
                wp.MAX_RANGE_CAP, "capabilities.max_range_bytes")
    _checked(wp.strict_bool, capabilities["lease"], "capabilities.lease")

    channel_mode = obj["consistency_channel"]
    if channel_mode not in ("lease", "etag"):
        _fail("manifest.consistency_channel must be lease or etag")
    etag = _checked(wp.parse_strong_etag, obj["etag"])
    capture_id = _strict_name(obj["capture_id"], "manifest.capture_id")

    config = _validate_config(obj["config"])
    channels = _validate_channels(obj["channels"])

    width = wp.WIDTH[config["dtype"]]
    expected_bytes = config["points"] * len(channels) * width
    raw_bytes = _strict_int(obj["raw_bytes"], expected_bytes, expected_bytes,
                            "manifest.raw_bytes")
    if raw_bytes > wp.MAX_RAW_BYTES:
        _fail("manifest.raw_bytes exceeds the 256 MiB acquisition limit")
    raw_sha256 = _hex_digest(obj["raw_sha256"], "manifest.raw_sha256")
    meta_sha256 = _hex_digest(obj["meta_sha256"], "manifest.meta_sha256")
    analysis = _validate_analysis(obj["analysis"], len(channels),
                                  config["points"])

    # Cross-check: the published meta fingerprint must be reproducible from
    # the manifest's own declared fields, binding config, channels,
    # raw_bytes and raw_sha256 to meta_sha256.
    reconstructed = {
        "capture_id": capture_id,
        "sample_rate": config["sample_rate"],
        "points": config["points"],
        "dtype": config["dtype"],
        "byte_order": config["byte_order"],
        "channels": channels,
        "raw_bytes": raw_bytes,
        "sha256": raw_sha256,
    }
    if meta_fingerprint(reconstructed) != meta_sha256:
        _fail("manifest meta_sha256 is inconsistent with its own "
              "config/channels/raw_bytes/raw_sha256")

    return {
        "source_url": source_url,
        "consistency_channel": channel_mode,
        "etag": etag,
        "capture_id": capture_id,
        "sample_rate": config["sample_rate"],
        "points": config["points"],
        "dtype": config["dtype"],
        "byte_order": config["byte_order"],
        "channels": channels,
        "raw_bytes": raw_bytes,
        "raw_sha256": raw_sha256,
        "meta_sha256": meta_sha256,
        "analysis": analysis,
    }
