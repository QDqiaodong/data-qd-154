"""Store-level tests: SQLite commits, identity, recovery markers, reopen.

Reopening through a fresh ``Store.open`` and through the command-line
subprocess both stand in for a new process picking up an existing database.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from signal_spectrum.workbench.store import Store, StoreError  # noqa: E402
from signal_spectrum.workbench import engine  # noqa: E402

CONFIG = {
    "sample_rate": 8,
    "frame_size": 4,
    "hop": 2,
    "window": "rectangular",
    "remove_mean": True,
}


def make_store(root, name="wb.db", report_name="report.json"):
    return Store.create(
        os.path.join(root, name),
        engine.validate_config(CONFIG),
        os.path.abspath(os.path.join(root, report_name)),
    )


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = self.temp.name

    def tearDown(self):
        self.temp.cleanup()

    def db_path(self, name="wb.db"):
        return os.path.join(self.root, name)

    def test_init_creates_generation_zero_and_refuses_existing_db(self):
        store = make_store(self.root)
        self.assertEqual(store.committed_generation, 0)
        # published -1 internally until the first publish confirms.
        store.mark_published(0)
        self.assertIsNone(store.pending_generation)
        content = json.loads(store.report_content(0))
        self.assertEqual(content["generation"], 0)
        self.assertIsNone(content["live_psd"])
        store.close()

        with self.assertRaises(StoreError):
            Store.create(self.db_path(), CONFIG, os.path.abspath(os.path.join(self.root, "r2.json")))

    def test_commit_is_atomic_and_state_reopens(self):
        store = make_store(self.root)
        store.mark_published(0)
        gen, report = store.commit_request(
            "r1", "a" * 64, [(0, 1.0), (1, 2.0), (2, 3.0), (3, 4.0)], 0
        )
        self.assertEqual(gen, 1)
        self.assertEqual(store.seal, 0)
        self.assertEqual(store.horizon, 4)
        store.close()

        # Reopen as a brand-new process would.
        reopened = Store.open(self.db_path())
        self.assertEqual(reopened.committed_generation, 1)
        samples, tombstone = reopened.load_state()
        self.assertEqual(samples, {0: 1.0, 1: 2.0, 2: 3.0, 3: 4.0})
        self.assertEqual(tombstone, set())
        self.assertEqual(reopened.lookup_request("r1"), {"sha256": "a" * 64, "generation": 1})
        self.assertIsNone(reopened.lookup_request("missing"))
        reopened.close()

    def test_reopen_through_subprocess(self):
        store = make_store(self.root)
        store.mark_published(0)
        store.commit_request("r1", "a" * 64, [(i, float(i)) for i in range(8)], 2)
        store.close()

        run = subprocess.run(
            [sys.executable, "-m", "signal_spectrum.workbench.cli", "status", "--db", self.db_path()],
            capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(SRC)},
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        payload = json.loads(run.stdout)
        self.assertEqual(payload["committed_generation"], 1)
        self.assertEqual(payload["published_generation"], 1)
        self.assertIsNone(payload["pending"])
        self.assertEqual(payload["seal"], 2)
        self.assertEqual(payload["horizon"], 8)

    def test_horizon_never_retreats_after_deleting_highest_index(self):
        store = make_store(self.root)
        store.mark_published(0)
        store.commit_request("r1", "a" * 64, [(i, float(i)) for i in range(8)], 0)
        self.assertEqual(store.horizon, 8)
        # Delete the highest written index: horizon stays at 8 (no retreat).
        store.commit_request("r2", "b" * 64, [(7, None)], 0)
        self.assertEqual(store.horizon, 8)
        samples, tombstone = store.load_state()
        self.assertNotIn(7, samples)
        self.assertIn(7, tombstone)
        report = json.loads(store.report_content(2))
        # Windows up to horizon 8 are still listed; the last window is a gap.
        self.assertEqual([w["start"] for w in report["windows"]], [0, 2, 4])
        self.assertEqual(
            [w["status"] for w in report["windows"]], ["complete", "complete", "gap"]
        )

    def test_deleting_never_written_index_is_accepted_and_advances_horizon(self):
        store = make_store(self.root)
        store.mark_published(0)
        store.commit_request("r1", "a" * 64, [(i, float(i)) for i in range(8)], 0)
        # A tombstone for an index never written is still an accepted write, so
        # the high-water mark advances; it never moves backwards later.
        store.commit_request("r2", "b" * 64, [(11, None)], 0)
        self.assertEqual(store.horizon, 12)
        _, tombstone = store.load_state()
        self.assertIn(11, tombstone)
        store.commit_request("r3", "c" * 64, [(11, 5.0)], 0)
        self.assertEqual(store.horizon, 12)

    def test_revision_withdraws_and_recomputes_overlapping_windows(self):
        store = make_store(self.root)
        store.mark_published(0)
        store.commit_request("r1", "a" * 64, [(i, float(v)) for i, v in enumerate(
            [0, 1, 0, -1, 0, 1, 0, -1])], 2)
        store.commit_request("r2", "b" * 64, [(3, -2.0)], 2)
        before = json.loads(store.report_content(2))
        # Windows start 0 and start 2 both contain index 3.
        self.assertTrue(all(w["status"] == "complete" for w in before["windows"]))
        store.commit_request("r3", "c" * 64, [(4, None)], 2)
        after = json.loads(store.report_content(3))
        self.assertEqual(
            [(w["start"], w["status"]) for w in after["windows"]],
            [(0, "complete"), (2, "gap"), (4, "gap")],
        )
        self.assertEqual(after["live_complete_count"], 1)
        # Sealing freezes the gaps and leaves only window 0 in sealed_psd.
        store.commit_request("r4", "d" * 64, [], 8)
        sealed = json.loads(store.report_content(4))
        self.assertEqual(sealed["sealed_gap_count"], 2)
        self.assertEqual(sealed["sealed_complete_count"], 1)
        self.assertEqual(sealed["live_complete_count"], 1)
        # The sealed contribution equals window 0 alone.
        self.assertEqual(
            [round(b["psd"], 12) for b in sealed["sealed_psd"]],
            [round(b["psd"], 12) for b in sealed["live_psd"]],
        )

    def test_duplicate_request_id_is_constrained(self):
        store = make_store(self.root)
        store.mark_published(0)
        store.commit_request("r1", "a" * 64, [(0, 1.0)], 0)
        with self.assertRaises(sqlite3.IntegrityError):
            store.commit_request("r1", "b" * 64, [(1, 2.0)], 0)
        # Failed insert rolled back: generation and horizon unchanged.
        self.assertEqual(store.committed_generation, 1)
        self.assertEqual(store.horizon, 1)

    def test_pending_marker_and_stored_generations(self):
        store = make_store(self.root)
        # Before confirming publication of gen 0, pending is generation 0.
        self.assertEqual(store.pending_generation, 0)
        store.mark_published(0)
        gen, _ = store.commit_request("r1", "a" * 64, [(0, 1.0)], 0)
        self.assertEqual(store.pending_generation, 1)
        # Every generation keeps its own immutable, self-contained report.
        first = store.report_content(0)
        second = store.report_content(1)
        self.assertNotEqual(first, second)
        store.mark_published(gen)
        self.assertIsNone(store.pending_generation)

    def test_each_new_request_generates_even_when_identical_outcome(self):
        store = make_store(self.root)
        store.mark_published(0)
        store.commit_request("r1", "a" * 64, [(0, 1.0)], 0)
        store.commit_request("r2", "b" * 64, [(0, 1.0)], 0)  # same resulting value
        self.assertEqual(store.committed_generation, 2)
        g1 = json.loads(store.report_content(1))
        g2 = json.loads(store.report_content(2))
        self.assertEqual(g1["windows"], g2["windows"])
        self.assertEqual(g1["live_psd"], g2["live_psd"])
        self.assertEqual(g2["generation"], 2)

    def test_report_bytes_are_deterministic(self):
        store = make_store(self.root)
        store.mark_published(0)
        store.commit_request("r1", "a" * 64, [(i, float(i)) for i in range(5)], 0)
        content = store.report_content(1)
        again = store.report_content(1)
        self.assertEqual(content, again)
        # allow_nan=False style guarantee: reload and check finiteness.
        report = json.loads(content)
        for bin_entry in report["live_psd"]:
            self.assertTrue(__import__("math").isfinite(bin_entry["psd"]))


if __name__ == "__main__":
    unittest.main()
