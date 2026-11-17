"""Unit tests for strict protocol validation and streaming decode rules.

These tests never open a socket; they pin down the JSON/ETag/meta contract
and the point-major, streaming, bounded-memory decoder.
"""
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from signal_spectrum.waveform.protocol import (
    MAX_JSON_BYTES,
    ProtocolError,
    parse_content_range,
    parse_strong_etag,
    validate_capabilities,
    validate_meta,
)
from signal_spectrum.waveform.decode import decode_stream


def meta(**overrides):
    base = {
        "capture_id": "cap-1",
        "sample_rate": 1000,
        "points": 8,
        "dtype": "int16",
        "byte_order": "little",
        "channels": [
            {"name": "a", "scale": 0.001, "offset": 0.0},
            {"name": "b", "scale": 0.002, "offset": 1.0},
        ],
        "raw_bytes": 8 * 2 * 2,
        "sha256": "a" * 64,
    }
    base.update(overrides)
    return base


class StrictJson(unittest.TestCase):
    def test_duplicate_keys_rejected(self):
        from signal_spectrum.waveform.protocol import parse_json_object
        with self.assertRaises(ProtocolError):
            parse_json_object(b'{"lease": true, "lease": false}')

    def test_nan_infinity_rejected(self):
        from signal_spectrum.waveform.protocol import parse_json_object
        for body in (b'{"x": NaN}', b'{"x": Infinity}', b'{"x": -Infinity}'):
            with self.assertRaises(ProtocolError):
                parse_json_object(body)

    def test_oversized_rejected(self):
        from signal_spectrum.waveform.protocol import parse_json_object
        with self.assertRaises(ProtocolError):
            parse_json_object(b"x" * (MAX_JSON_BYTES + 1))


class CapabilitiesAndMeta(unittest.TestCase):
    def test_capabilities_exact_fields_and_bounds(self):
        self.assertEqual(
            validate_capabilities({"max_range_bytes": 1, "lease": False}),
            {"max_range_bytes": 1, "lease": False})
        for bad in (
            {"max_range_bytes": 0, "lease": False},
            {"max_range_bytes": 65537, "lease": False},
            {"max_range_bytes": True, "lease": False},
            {"max_range_bytes": 100, "lease": "yes"},
            {"max_range_bytes": 100},
            {"max_range_bytes": 100, "lease": False, "extra": 1},
        ):
            with self.assertRaises(ProtocolError):
                validate_capabilities(bad)

    def test_meta_bool_never_a_number(self):
        with self.assertRaises(ProtocolError):
            validate_meta(meta(sample_rate=True))
        with self.assertRaises(ProtocolError):
            validate_meta(meta(points=8.0))

    def test_meta_unknown_and_missing_fields(self):
        bad = meta()
        bad["extra"] = 1
        with self.assertRaises(ProtocolError):
            validate_meta(bad)
        bad = meta()
        del bad["dtype"]
        with self.assertRaises(ProtocolError):
            validate_meta(bad)

    def test_meta_enums_names_sha(self):
        with self.assertRaises(ProtocolError):
            validate_meta(meta(dtype="int32"))
        with self.assertRaises(ProtocolError):
            validate_meta(meta(byte_order="middle"))
        dup = meta()
        dup["channels"][1]["name"] = "a"
        with self.assertRaises(ProtocolError):
            validate_meta(dup)
        blank = meta()
        blank["channels"][0]["name"] = ""
        with self.assertRaises(ProtocolError):
            validate_meta(blank)
        with self.assertRaises(ProtocolError):
            validate_meta(meta(sha256="z" * 64))
        with self.assertRaises(ProtocolError):
            validate_meta(meta(sha256="abc"))

    def test_raw_bytes_must_equal_product(self):
        with self.assertRaises(ProtocolError):
            validate_meta(meta(raw_bytes=31))

    def test_non_finite_calibration_rejected(self):
        bad = meta()
        bad["channels"][0]["scale"] = float("nan")
        with self.assertRaises(ProtocolError):
            validate_meta(bad)

    def test_valid_normalizes_sha_case(self):
        out = validate_meta(meta(sha256="A" * 64))
        self.assertEqual(out["sha256"], "a" * 64)


class ETagAndRange(unittest.TestCase):
    def test_strong_etag_only(self):
        self.assertEqual(parse_strong_etag('"abc123"'), '"abc123"')
        for bad in (None, "", 'W/"abc"', 'w/"abc"', 'abc', '"abc'):
            with self.assertRaises(ProtocolError):
                parse_strong_etag(bad)

    def test_content_range(self):
        self.assertEqual(parse_content_range("bytes 0-99/1000"), (0, 99, 1000))
        for bad in ("0-99/1000", "bytes 99-9/1000", "bytes 0-99/*",
                    "bytes 0-99/50", "bytes x-y/z"):
            with self.assertRaises(ProtocolError):
                parse_content_range(bad)


class Decoder(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _decode(self, raw, m, analysis=None, fragment_size=3):
        raw_path = self.dir / "raw.bin"
        raw_path.write_bytes(raw)
        out = self.dir / "samples.ndjson"
        return decode_stream(validate_meta(m), raw_path, out,
                             analysis=analysis), out

    def test_interleaved_signed_int16_calibration(self):
        points, channels = 6, 2
        raw = bytearray()
        expected = []
        for i in range(points):
            row = []
            for c, code in enumerate((i - 3, 100 + i)):
                raw += struct.pack("<h", code)
                scale = 0.5 if c == 0 else 0.1
                offset = 1.5 if c == 0 else -2.0
                row.append(code * scale + offset)
            expected.append(row)
        m = meta(points=points, raw_bytes=len(raw))
        m["channels"] = [
            {"name": "a", "scale": 0.5, "offset": 1.5},
            {"name": "b", "scale": 0.1, "offset": -2.0},
        ]
        _, out = self._decode(bytes(raw), m)
        lines = [json.loads(line) for line in out.read_text().splitlines()]
        self.assertEqual([row["index"] for row in lines], list(range(points)))
        for row, values in zip(lines, expected):
            self.assertEqual(row["values"], values)

    def test_uint16_big_endian_and_float32(self):
        # uint16 big-endian, single channel.
        codes = [0, 1, 65535, 32768]
        raw = b"".join(struct.pack(">H", c) for c in codes)
        m = meta(points=4, dtype="uint16", byte_order="big",
                 channels=[{"name": "u", "scale": 1.0, "offset": 0.0}],
                 raw_bytes=len(raw))
        _, out = self._decode(raw, m)
        self.assertEqual(
            [json.loads(line)["values"][0] for line in
             out.read_text().splitlines()],
            [0.0, 1.0, 65535.0, 32768.0])

        # float32 little-endian with odd fragment boundaries exercised via the
        # file reader below.
        values = [1.25, -2.5, 3.75, -4.0]
        raw = b"".join(struct.pack("<f", v) for v in values)
        m = meta(points=4, dtype="float32", byte_order="little",
                 channels=[{"name": "f", "scale": 2.0, "offset": 0.0}],
                 raw_bytes=len(raw))
        result, out = self._decode(raw, m, analysis=(0, 4))
        lines = [json.loads(line) for line in out.read_text().splitlines()]
        got = [row["values"][0] for row in lines]
        for g, v in zip(got, values):
            self.assertAlmostEqual(g, v * 2.0, places=5)
        self.assertEqual(len(result["analysis_values"]), 4)

    def test_odd_fragments_cross_word_and_channel_boundaries(self):
        points, channels = 5, 3
        raw = bytearray()
        for i in range(points):
            for c in range(channels):
                raw += struct.pack("<f", float(i * 10 + c))
        m = meta(points=points, dtype="float32",
                 channels=[{"name": n, "scale": 1.0, "offset": 0.0}
                           for n in ("a", "b", "c")],
                 raw_bytes=len(raw))
        # Fragment sizes deliberately prime / not multiples of the word width.
        for fragment in (1, 3, 7):
            from signal_spectrum.waveform import decode as decode_mod
            old_buffer = decode_mod._READ_BUFFER
            decode_mod._READ_BUFFER = fragment
            try:
                _, out = self._decode(bytes(raw), m)
            finally:
                decode_mod._READ_BUFFER = old_buffer
            rows = [json.loads(line) for line in out.read_text().splitlines()]
            self.assertEqual(len(rows), points)
            for i, row in enumerate(rows):
                self.assertEqual(row["values"],
                                 [float(i * 10 + c) for c in range(channels)])

    def test_nan_raw_value_rejected(self):
        raw = struct.pack("<f", float("nan")) * 8
        m = meta(points=8, dtype="float32",
                 channels=[{"name": "f", "scale": 1.0, "offset": 0.0}],
                 raw_bytes=len(raw))
        with self.assertRaises(ProtocolError):
            self._decode(raw, m)

    def test_length_product_mismatch_rejected(self):
        m = meta(points=8, raw_bytes=32)
        with self.assertRaises(ProtocolError):
            self._decode(b"\x00" * 30, m)

    def test_trailing_partial_frame_rejected(self):
        m = meta(points=8, raw_bytes=32)
        with self.assertRaises(ProtocolError):
            self._decode(b"\x00" * 33, m)

    def test_analysis_overrange_rejects_export_only(self):
        # Calibrated value within finiteness but beyond the legacy DFT limit.
        raw = struct.pack("<h", 20000) * 8
        m = meta(points=8, raw_bytes=16,
                 channels=[{"name": "a", "scale": 100000.0, "offset": 0.0}])
        # Basic decode (no analysis) succeeds with finite calibrated values.
        result, _ = self._decode(raw, m)
        self.assertEqual(result["decoded_points"], 8)
        with self.assertRaises(ProtocolError):
            self._decode(raw, m, analysis=(0, 4))

    def test_analysis_count_bounds(self):
        raw = b"\x00" * 16
        m = meta(points=8, raw_bytes=16)
        with self.assertRaises(ProtocolError):
            self._decode(raw, m, analysis=(0, 3))
        with self.assertRaises(ProtocolError):
            self._decode(raw, m, analysis=(0, 9))
        with self.assertRaises(ProtocolError):
            self._decode(raw, m, analysis=(5, 4))


if __name__ == "__main__":
    unittest.main()
