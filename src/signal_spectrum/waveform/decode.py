"""Streaming raw-byte decoder, per-point calibration and analysis export.

The decoder never holds the complete payload or a complete per-channel array:
raw bytes are read through a fixed buffer, at most one partial interleaved
frame is carried between reads, and calibrated points are emitted one point at
a time into the NDJSON writer.
"""
import json
import math
import struct

from ..model import analyze
from .protocol import (
    ANALYSIS_ABS_LIMIT,
    WIDTH,
    ProtocolError,
)

_READ_BUFFER = 65536


def _unpacker(dtype, byte_order):
    prefix = ">" if byte_order == "big" else "<"
    if dtype == "int16":
        code = "h"
    elif dtype == "uint16":
        code = "H"
    else:
        code = "f"
    width = WIDTH[dtype]
    packer = struct.Struct(prefix + code)

    def unpack(chunk):
        return packer.unpack(chunk)[0]

    return unpack, width


def iter_frames(raw_path, frame_size):
    """Yield complete point frames, streaming a file without size assumptions."""
    carry = bytearray()
    with open(raw_path, "rb") as stream:
        while True:
            chunk = stream.read(_READ_BUFFER)
            if not chunk:
                break
            carry.extend(chunk)
            usable = len(carry) - (len(carry) % frame_size)
            offset = 0
            while offset + frame_size <= usable:
                yield bytes(carry[offset:offset + frame_size])
                offset += frame_size
            del carry[:usable]
    if carry:
        raise ProtocolError(
            "raw length is not a whole number of interleaved frames "
            "(%d trailing bytes)" % len(carry))


def decode_stream(meta, raw_path, ndjson_path, analysis=None):
    """Decode and calibrate every point; write samples.ndjson line by line.

    ``meta`` must already be validated.  Each output line contains exactly
    ``index`` and ``values`` (one calibrated value per channel, channel
    order).  When ``analysis`` is ``(channel, count)`` the first ``count``
    calibrated values of that channel are collected (bounded, at most 512)
    and the legacy single-segment DFT input is returned for later
    verification; out-of-range values reject the whole publication because
    the old DFT cannot accept them.
    """
    channels = meta["channels"]
    channel_count = len(channels)
    sample_width = WIDTH[meta["dtype"]]
    frame_size = channel_count * sample_width
    total_frames = meta["points"]
    expected_bytes = total_frames * frame_size
    unpack, _ = _unpacker(meta["dtype"], meta["byte_order"])

    analysis_values = None
    analysis_channel = analysis_count = None
    if analysis is not None:
        analysis_channel, analysis_count = analysis
        if not (0 <= analysis_channel < channel_count):
            raise ProtocolError("analysis channel out of range")
        if not (4 <= analysis_count <= 512) or analysis_count > total_frames:
            raise ProtocolError("analysis count must be 4..512 and <= points")
        analysis_values = []

    written = 0
    with open(ndjson_path, "w", encoding="utf-8") as out:
        for frame in iter_frames(raw_path, frame_size):
            values = []
            for channel in range(channel_count):
                raw_value = unpack(frame[channel * sample_width:
                                        (channel + 1) * sample_width])
                if not math.isfinite(raw_value):
                    raise ProtocolError(
                        "non-finite raw value at index %d channel %d"
                        % (written, channel))
                spec = channels[channel]
                calibrated = raw_value * spec["scale"] + spec["offset"]
                if not math.isfinite(calibrated):
                    raise ProtocolError(
                        "non-finite calibrated value at index %d channel %d"
                        % (written, channel))
                values.append(calibrated)
                if (analysis is not None
                        and channel == analysis_channel
                        and written < analysis_count):
                    if abs(calibrated) > ANALYSIS_ABS_LIMIT:
                        raise ProtocolError(
                            "analysis channel value %.17g exceeds the legacy "
                            "DFT magnitude limit %d"
                            % (calibrated, ANALYSIS_ABS_LIMIT))
                    analysis_values.append(calibrated)
            record = {"index": written, "values": values}
            out.write(json.dumps(record, sort_keys=True,
                                 ensure_ascii=False, allow_nan=False))
            out.write("\n")
            written += 1

    if written != total_frames:
        raise ProtocolError(
            "decoded %d points but meta declares %d"
            % (written, total_frames))
    if analysis is not None and len(analysis_values) != analysis_count:
        raise ProtocolError("analysis collection was incomplete")
    return {
        "frame_size": frame_size,
        "expected_bytes": expected_bytes,
        "decoded_points": written,
        "analysis_values": analysis_values,
    }


def build_analysis(meta, channel, count, values):
    """Build the exact legacy signal-spectrum input object for a channel."""
    return {
        "sample_rate": meta["sample_rate"],
        "window": "rectangular",
        "remove_mean": False,
        "samples": list(values[:count]),
    }


def verify_analysis(payload):
    """Run the real legacy DFT now so an un-consumable export cannot publish."""
    return analyze(payload)
