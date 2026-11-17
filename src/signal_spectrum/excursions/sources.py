"""Strict reader for the SOURCES.json build manifest list.

The file is a JSON object with exactly one key, ``sources``: an array of
1..32 objects each holding exactly ``source_id`` and ``package``.  Relative
``package`` paths are interpreted against the directory that contains the
SOURCES file.  ``source_id`` follows the project's existing non-empty
identifier constraint and must be unique across the whole list -- the
archive is keyed by it, never by the (possibly duplicated) ``capture_id``.
"""
import json
from pathlib import Path

from ..waveform import protocol as wp
from .errors import ExcursionError

SOURCES_MAX_BYTES = 1024 * 1024
MAX_SOURCES = 32
MIN_SOURCES = 1
MAX_RAW_TOTAL_BYTES = 1024 ** 3  # 1 GiB across all source packages


def _fail(message):
    raise ExcursionError(message)


def parse_sources_bytes(raw):
    if len(raw) > SOURCES_MAX_BYTES:
        _fail("SOURCES file exceeds %d bytes" % SOURCES_MAX_BYTES)

    def unique(pairs):
        seen = set()
        for key, value in pairs:
            if key in seen:
                _fail("duplicate JSON key in SOURCES: " + str(key))
            seen.add(key)
        return dict(pairs)

    def reject_constant(value):
        _fail("non-finite JSON constant in SOURCES: " + value)

    try:
        obj = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExcursionError("invalid SOURCES JSON: " + str(error)) from None
    return obj


def validate_sources(obj):
    """Validate the parsed SOURCES object; return [(source_id, package)]."""
    if not isinstance(obj, dict) or set(obj) != {"sources"}:
        _fail("SOURCES must be an object with exactly one key: sources")
    entries = obj["sources"]
    if type(entries) is not list or not (MIN_SOURCES <= len(entries) <= MAX_SOURCES):
        _fail("sources must be an array of %d..%d entries"
              % (MIN_SOURCES, MAX_SOURCES))
    seen = set()
    normalized = []
    for index, entry in enumerate(entries):
        label = "sources[%d]" % index
        if not isinstance(entry, dict) or set(entry) != {"source_id", "package"}:
            _fail("%s must contain exactly source_id and package" % label)
        try:
            source_id = wp.strict_name(entry["source_id"],
                                       label + ".source_id")
        except wp.ProtocolError as error:
            raise ExcursionError(str(error)) from None
        if source_id in seen:
            _fail("duplicate source_id: " + source_id)
        seen.add(source_id)
        package = entry["package"]
        if not isinstance(package, str) or not package:
            _fail("%s.package must be a non-empty path string" % label)
        if "\x00" in package:
            _fail("%s.package contains a NUL character" % label)
        normalized.append((source_id, package))
    return normalized


def load_sources(sources_path):
    """Read and validate SOURCES.json; resolve package paths.

    Returns a list of ``(source_id, absolute_package_path)`` in file order.
    """
    sources_path = Path(sources_path)
    try:
        with open(sources_path, "rb") as stream:
            raw = stream.read(SOURCES_MAX_BYTES + 1)
    except OSError as error:
        raise ExcursionError("cannot read SOURCES file: " + str(error)) from None
    entries = validate_sources(parse_sources_bytes(raw))
    base = sources_path.resolve().parent
    resolved = []
    for source_id, package in entries:
        path = Path(package)
        if not path.is_absolute():
            path = base / path
        resolved.append((source_id, path))
    return resolved
