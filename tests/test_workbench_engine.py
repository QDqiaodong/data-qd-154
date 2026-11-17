"""Unit tests for workbench validation, windowing and Welch PSD math.

The numeric cases are hand-computed: DC/Nyquist windows, overlapping and
non-overlapping sine windows and the Parseval-style identity

    sum(PSD) * sample_rate / N == sum((centered * w)**2) / sum(w**2)
"""
import math
import unittest

from signal_spectrum.workbench import engine


def config(**overrides):
    base = {
        "sample_rate": 8,
        "frame_size": 4,
        "hop": 2,
        "window": "rectangular",
        "remove_mean": True,
    }
    base.update(overrides)
    return base


class ValidationTests(unittest.TestCase):
    def test_config_exact_fields(self):
        good = config()
        self.assertEqual(engine.validate_config(dict(good)), good)
        bad = dict(good)
        bad["extra"] = 1
        with self.assertRaises(ValueError):
            engine.validate_config(bad)
        with self.assertRaises(ValueError):
            engine.validate_config({k: v for k, v in good.items() if k != "hop"})

    def test_config_bounds_and_enums(self):
        for frame_size in (2, 3, 5, 130, 127):
            with self.assertRaises(ValueError):
                engine.validate_config(config(frame_size=frame_size))
        for hop in (0, 5):
            with self.assertRaises(ValueError):
                engine.validate_config(config(hop=hop))
        with self.assertRaises(ValueError):
            engine.validate_config(config(sample_rate=0))
        with self.assertRaises(ValueError):
            engine.validate_config(config(sample_rate=True))
        with self.assertRaises(ValueError):
            engine.validate_config(config(window="hamming"))
        with self.assertRaises(ValueError):
            engine.validate_config(config(remove_mean=1))
        # Even N from 4..128 is accepted.
        for n in (4, 6, 128):
            engine.validate_config(config(frame_size=n, hop=n))

    def test_request_exact_fields_and_identifier(self):
        base = {"request_id": "r1", "writes": [], "seal": 0}
        self.assertEqual(engine.validate_request(dict(base), old_seal=0), ("r1", [], 0))
        with self.assertRaises(ValueError):
            engine.validate_request({"request_id": "r1", "writes": []}, old_seal=0)
        # Advancing the seal is legal and normalized.
        self.assertEqual(
            engine.validate_request({**base, "seal": 1}, old_seal=0), ("r1", [], 1)
        )
        with self.assertRaises(ValueError):
            engine.validate_request({"request_id": " r", "writes": [], "seal": 0}, old_seal=0)
        with self.assertRaises(ValueError):
            engine.validate_request({"request_id": "", "writes": [], "seal": 0}, old_seal=0)

    def test_seal_monotonic_and_bounds(self):
        with self.assertRaises(ValueError):
            engine.validate_request(
                {"request_id": "r", "writes": [], "seal": 3}, old_seal=4
            )
        with self.assertRaises(ValueError):
            engine.validate_request(
                {"request_id": "r", "writes": [], "seal": 100001}, old_seal=0
            )
        ok = engine.validate_request(
            {"request_id": "r", "writes": [], "seal": 4}, old_seal=4
        )
        self.assertEqual(ok, ("r", [], 4))

    def test_write_rules(self):
        def req(writes, seal=0):
            return {"request_id": "r", "writes": writes, "seal": seal}

        rid, writes, _ = engine.validate_request(
            req([{"index": 5, "value": None}, {"index": 6, "value": -2.5}]), old_seal=0
        )
        self.assertEqual(writes, [(5, None), (6, -2.5)])
        # bool is not an integer
        with self.assertRaises(ValueError):
            engine.validate_request(req([{"index": True, "value": 1}]), old_seal=0)
        # duplicate index inside one batch
        with self.assertRaises(ValueError):
            engine.validate_request(
                req([{"index": 9, "value": 1}, {"index": 9, "value": 2}]), old_seal=0
            )
        # non-finite and out-of-amplitude values
        for bad in (float("nan"), float("inf"), 10**9 + 1, -(10**9 + 1)):
            with self.assertRaises(ValueError):
                engine.validate_request(req([{"index": 9, "value": bad}]), old_seal=0)
        # index bounds and sealed-frontier rejection
        with self.assertRaises(ValueError):
            engine.validate_request(req([{"index": -1, "value": 1}]), old_seal=0)
        with self.assertRaises(ValueError):
            engine.validate_request(req([{"index": 100001, "value": 1}]), old_seal=0)
        with self.assertRaises(ValueError):
            engine.validate_request(req([{"index": 2, "value": 1}]), old_seal=3)
        # batch size limit
        with self.assertRaises(ValueError):
            engine.validate_request(
                req([{"index": i, "value": 1} for i in range(20001)]), old_seal=0
            )
        # malformed write entry
        with self.assertRaises(ValueError):
            engine.validate_request(req([{"index": 1}]), old_seal=0)
        with self.assertRaises(ValueError):
            engine.validate_request(req([{"index": 1, "value": 1, "x": 0}]), old_seal=0)

    def test_indexes_at_limit_accepted(self):
        writes = [{"index": engine.MAX_INDEX, "value": 0}]
        rid, normalized, seal = engine.validate_request(
            {"request_id": "r", "writes": writes, "seal": engine.MAX_INDEX}, old_seal=0
        )
        self.assertEqual(normalized, [(engine.MAX_INDEX, 0.0)])
        self.assertEqual(seal, engine.MAX_INDEX)


class WindowClassificationTests(unittest.TestCase):
    def test_starts_and_horizon(self):
        self.assertEqual(engine.window_starts(4, 2, 0), [])
        self.assertEqual(engine.window_starts(4, 2, 3), [])
        self.assertEqual(engine.window_starts(4, 2, 4), [0])
        self.assertEqual(engine.window_starts(4, 2, 8), [0, 2, 4])
        self.assertEqual(engine.window_starts(4, 4, 9), [0, 4])

    def test_complete_gap_and_sealed(self):
        cfg = config()
        samples = {i: float(i) for i in range(8)}
        windows = engine.classify_windows(samples, set(), 4, 2, horizon=8, seal=2)
        self.assertEqual(
            [(w["start"], w["complete"], w["sealed"]) for w in windows],
            [(0, True, False), (2, True, False), (4, True, False)],
        )
        # A tombstone in the middle gaps every overlapping window.
        windows = engine.classify_windows(samples, {4}, 4, 2, horizon=8, seal=8)
        self.assertEqual(
            [(w["start"], w["complete"], w["sealed"]) for w in windows],
            [(0, True, True), (2, False, True), (4, False, True)],
        )

    def test_tail_shorter_than_frame_is_not_reported(self):
        cfg = config()
        samples = {i: 1.0 for i in range(5)}
        windows = engine.classify_windows(samples, set(), 4, 2, horizon=5, seal=0)
        self.assertEqual([w["start"] for w in windows], [0])

    def test_horizon_takes_the_seal_into_account(self):
        # With only two written points but seal 4, the frontier is 4 and one
        # whole (gap) window is reportable and sealed.
        report = engine.build_report(
            generation=1, request_id="r", config=config(),
            samples={0: 1.0, 1: 2.0}, tombstone=set(),
            horizon=max(2, 4), seal=4,
        )
        self.assertEqual(
            [(w["start"], w["status"], w["sealed"]) for w in report["windows"]],
            [(0, "gap", True)],
        )


class NumericTests(unittest.TestCase):
    def _report(self, values, cfg=None, seal=None):
        cfg = cfg or config()
        n = cfg["frame_size"]
        samples = {i: float(v) for i, v in enumerate(values)}
        horizon = max(n, len(values))
        return engine.build_report(
            generation=1,
            request_id="r",
            config=cfg,
            samples=samples,
            tombstone=set(),
            horizon=horizon,
            seal=horizon if seal is None else seal,
        )

    def test_dc_window_rectangular_no_mean_removal(self):
        # Constant 3.0, N=4, rectangular, no mean removal: all energy sits at
        # DC.  PSD[0] = |sum|^2 / (rate * N) = 144 / (8*4) = 4.5.
        report = self._report([3, 3, 3, 3], config(remove_mean=False))
        psd = [b["psd"] for b in report["live_psd"]]
        self.assertAlmostEqual(psd[0], 4.5)
        for value in psd[1:]:
            self.assertAlmostEqual(value, 0.0)

    def test_nyquist_window(self):
        # [1,-1,1,-1]: DFT energy only at the Nyquist bin k=2.
        # PSD[2] = |-4|^2 / (8*4) = 0.5.
        report = self._report([1, -1, 1, -1], config(remove_mean=False))
        psd = [b["psd"] for b in report["live_psd"]]
        self.assertAlmostEqual(psd[2], 0.5)
        self.assertAlmostEqual(psd[0], 0.0)
        self.assertAlmostEqual(psd[1], 0.0)

    def test_non_overlapping_sine_windows_equal_weight_average(self):
        # Two disjoint rectangular windows, each the same unit-amplitude sine
        # with one cycle over four samples: [0,1,0,-1] (energy at bin k=1).
        # DFT = [0,-2j,0,2j]: PSD[1] = 2*4/(8*4) = 0.25, the rest zero.
        report = self._report([0, 1, 0, -1, 0, 1, 0, -1], config(hop=4))
        self.assertEqual(report["live_complete_count"], 2)
        psd = [b["psd"] for b in report["live_psd"]]
        expected = [0.0, 0.25, 0.0]
        for got, want in zip(psd, expected):
            self.assertAlmostEqual(got, want)
        # Equal-weight average of identical windows equals the single window.
        single = self._report([0, 1, 0, -1])
        single_psd = [b["psd"] for b in single["live_psd"]]
        for got, want in zip(psd, single_psd):
            self.assertAlmostEqual(got, want)

    def test_corrected_sine_window_hand_values(self):
        # [0,1,0,-2], mean -0.25 -> centered [0.25,1.25,0.25,-2.25].
        # Hand DFT: k=1 -> 3j (|.|^2=9), k=2 -> -1 (|.|^2=1).
        # PSD[1] = 2*9/32 = 0.5625; PSD[2] = 1/32 = 0.03125.
        report = self._report([0, 1, 0, -2])
        psd = [b["psd"] for b in report["live_psd"]]
        self.assertAlmostEqual(psd[0], 0.0)
        self.assertAlmostEqual(psd[1], 0.5625)
        self.assertAlmostEqual(psd[2], 0.03125)

    def test_overlapping_windows_average(self):
        # N=4, H=2, horizon=6 -> windows start 0 and 2.  Hand-check the
        # average against an independent direct DFT of each window.
        values = [1.0, 2.0, 0.5, -1.0, 3.0, -2.0]
        report = self._report(values, config(hop=2, remove_mean=True))
        self.assertEqual(report["live_complete_count"], 2)

        def direct(window):
            mean = math.fsum(window) / 4
            centered = [x - mean for x in window]
            out = []
            for k in range(3):
                dft = sum(
                    centered[i] * math.e ** (-2j * math.pi * k * i / 4)
                    for i in range(4)
                )
                mult = 1 if k in (0, 2) else 2
                out.append(mult * abs(dft) ** 2 / (8 * 4))
            return out

        expected = [
            (a + b) / 2
            for a, b in zip(direct(values[0:4]), direct(values[2:6]))
        ]
        for bin_entry, want in zip(report["live_psd"], expected):
            self.assertAlmostEqual(bin_entry["psd"], want, places=12)

    def test_parseval_identity_rect_and_hann(self):
        # sum(PSD) * rate / N == sum((centered*w)^2) / sum(w^2)
        for window in ("rectangular", "hann"):
            for remove_mean in (True, False):
                values = [0.7, -1.3, 2.1, -0.4]
                cfg = config(window=window, remove_mean=remove_mean)
                report = self._report(values, cfg)
                n = cfg["frame_size"]
                weights = engine.window_weights(n, window)
                centered = values
                if remove_mean:
                    mean = math.fsum(values) / n
                    centered = [x - mean for x in values]
                windowed = [x * w for x, w in zip(centered, weights)]
                rhs = math.fsum(x * x for x in windowed) / math.fsum(w * w for w in weights)
                lhs = math.fsum(b["psd"] for b in report["live_psd"]) * 8 / n
                self.assertAlmostEqual(lhs, rhs, places=12)
                self.assertTrue(math.isfinite(lhs))

    def test_gap_windows_excluded_and_empty_averages_null(self):
        cfg = config()
        samples = {0: 1.0, 1: 1.0, 2: 1.0, 3: 1.0}  # only window start 0 complete
        report = engine.build_report(
            generation=1, request_id="r", config=cfg, samples=samples,
            tombstone=set(), horizon=8, seal=8,
        )
        self.assertEqual(
            [(w["start"], w["status"]) for w in report["windows"]],
            [(0, "complete"), (2, "gap"), (4, "gap")],
        )
        self.assertEqual(report["live_complete_count"], 1)
        self.assertEqual(report["sealed_complete_count"], 1)
        self.assertEqual(report["sealed_gap_count"], 2)
        self.assertIsNotNone(report["live_psd"])
        self.assertIsNotNone(report["sealed_psd"])

        empty = engine.build_report(
            generation=0, request_id=None, config=cfg, samples={},
            tombstone=set(), horizon=0, seal=0,
        )
        self.assertIsNone(empty["live_psd"])
        self.assertIsNone(empty["sealed_psd"])
        self.assertEqual(empty["windows"], [])

    def test_sealed_psd_only_contains_sealed_complete_windows(self):
        # horizon 8, seal 4: window start 0 (end 4) sealed, starts 2/4 open.
        values = [1.0, -1.0, 0.5, -0.5, 2.0, -2.0, 0.25, -0.25]
        report = self._report(values, config(hop=2), seal=4)
        self.assertEqual(
            [(w["start"], w["sealed"]) for w in report["windows"]],
            [(0, True), (2, False), (4, False)],
        )
        single = self._report(values[:4], config(hop=2), seal=4)
        for sealed_bin, one_bin in zip(report["sealed_psd"], single["live_psd"]):
            self.assertAlmostEqual(sealed_bin["psd"], one_bin["psd"])
        self.assertEqual(report["sealed_complete_count"], 1)
        self.assertEqual(report["live_complete_count"], 3)


if __name__ == "__main__":
    unittest.main()
