"""End-to-end command tests for signal-spectrum-workbench.

Covers the acceptance scenarios: write-then-seal, sealed gaps, batch
rollback, horizon non-retreat, crash injection at both termination points with
the source file removed, replay/old-request semantics and path safety.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

MODULE = ["-m", "signal_spectrum.workbench.cli"]
CONFIG = {
    "sample_rate": 8,
    "frame_size": 4,
    "hop": 2,
    "window": "rectangular",
    "remove_mean": True,
}


def request(request_id, writes, seal):
    return {"request_id": request_id, "writes": writes, "seal": seal}


class WorkbenchCLI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "wb.db"
        self.report = self.root / "report.json"
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(CONFIG))
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        self.env = env

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args, expect=0, env=None, cwd=None):
        proc = subprocess.run(
            [sys.executable, *MODULE, *args],
            capture_output=True, text=True, env=env or self.env, cwd=cwd,
        )
        self.assertEqual(proc.returncode, expect,
                         "args=%r stdout=%r stderr=%r" % (args, proc.stdout, proc.stderr))
        return proc

    def write_request(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload))
        return path

    def init(self):
        self.run_cli("init", "--db", self.db, "--config", self.config_path,
                     "--report", self.report)
        return json.loads(self.report.read_text())

    def apply(self, source, expect=0):
        return self.run_cli("apply", "--db", self.db, "--source", source, expect=expect)

    def status(self, expect=0):
        proc = self.run_cli("status", "--db", self.db, expect=expect)
        return json.loads(proc.stdout)

    # -- basic lifecycle -----------------------------------------------------

    def test_init_empty_report_and_status(self):
        report = self.init()
        self.assertEqual(report["generation"], 0)
        self.assertEqual(report["horizon"], 0)
        self.assertIsNone(report["live_psd"])
        payload = self.status()
        self.assertEqual(payload["committed_generation"], 0)
        self.assertEqual(payload["published_generation"], 0)
        self.assertIsNone(payload["pending"])

    def test_init_does_not_overwrite_and_keeps_old_config(self):
        self.init()
        other_config = self.root / "other.json"
        other_config.write_text(json.dumps({**CONFIG, "sample_rate": 16}))
        other_report = self.root / "other-report.json"
        # Existing DB: init refuses; stored report path/config are unchanged.
        proc = self.run_cli("init", "--db", self.db, "--config", other_config,
                            "--report", other_report, expect=2)
        status = json.loads(proc.stdout)
        self.assertEqual(status["committed_generation"], 0)
        self.assertEqual(status["report_path"], str(self.report.resolve()))
        self.assertFalse(other_report.exists())

    def test_write_then_seal_and_sealed_index_cannot_change(self):
        self.init()
        r1 = self.write_request("r1.json", request(
            "r1", [{"index": i, "value": v} for i, v in enumerate(
                [0, 1, 0, -1, 0, 1, 0, -1])], 2))
        self.apply(r1)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["generation"], 1)
        self.assertEqual(report["horizon"], 8)
        self.assertEqual(report["seal"], 2)
        self.assertEqual(report["live_complete_count"], 3)
        self.assertEqual(report["sealed_complete_count"], 0)

        # Late write below the seal is rejected and changes nothing.
        bad = self.write_request("bad.json", request("bad", [{"index": 0, "value": 9}], 2))
        self.apply(bad, expect=2)
        self.assertEqual(self.status()["committed_generation"], 1)

        # Seal to 8: every window is sealed and all are complete here.
        seal_req = self.write_request("seal.json", request("seal", [], 8))
        self.apply(seal_req)
        late = self.write_request("late.json", request("late", [{"index": 4, "value": 0}], 8))
        self.apply(late, expect=2)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["sealed_gap_count"], 0)
        self.assertEqual(report["sealed_complete_count"], 3)
        self.assertEqual(
            [round(b["psd"], 9) for b in report["sealed_psd"]],
            [0.0, 0.25, 0.0],
        )

    def test_full_linked_acceptance_correction_deletion_seal(self):
        # N=4, H=2: write 0..7 and seal=2, correct index 3 (windows 0 and 2
        # change together), delete index 4 (windows 2 and 4 leave live), then
        # seal=8 freezes both gaps; a late fill of 4 is rejected and
        # sealed_psd contains window 0 only.
        self.init()
        r1 = self.write_request("r1.json", request(
            "r1", [{"index": i, "value": v} for i, v in enumerate(
                [0, 1, 0, -1, 0, 1, 0, -1])], 2))
        self.apply(r1)
        self.apply(self.write_request("r2.json", request("r2", [{"index": 3, "value": -2}], 2)))
        after_correct = json.loads(self.report.read_text())
        self.assertEqual([w["status"] for w in after_correct["windows"]],
                         ["complete", "complete", "complete"])
        self.apply(self.write_request("r3.json", request("r3", [{"index": 4, "value": None}], 2)))
        after_delete = json.loads(self.report.read_text())
        self.assertEqual(
            [(w["start"], w["status"]) for w in after_delete["windows"]],
            [(0, "complete"), (2, "gap"), (4, "gap")],
        )
        self.assertEqual(after_delete["live_complete_count"], 1)
        self.apply(self.write_request("r4.json", request("r4", [], 8)))
        sealed = json.loads(self.report.read_text())
        self.assertEqual(sealed["sealed_gap_count"], 2)
        self.assertEqual(sealed["sealed_complete_count"], 1)
        self.assertEqual(sealed["live_complete_count"], 1)
        self.assertEqual(
            [round(b["psd"], 9) for b in sealed["sealed_psd"]],
            [0.0, 0.5625, 0.03125],
        )
        self.assertEqual(
            [round(b["psd"], 9) for b in sealed["live_psd"]],
            [0.0, 0.5625, 0.03125],
        )
        late = self.write_request("late.json", request("late", [{"index": 4, "value": 0}], 8))
        sealed_bytes = self.report.read_bytes()
        self.apply(late, expect=2)
        self.assertEqual(self.report.read_bytes(), sealed_bytes)
        self.assertEqual(
            json.loads(self.report.read_text())["sealed_gap_count"], 2
        )

    def test_mixed_valid_and_out_of_bounds_batch_rolls_back_entirely(self):
        self.init()
        good = self.write_request("good.json", request(
            "good", [{"index": i, "value": 1.0} for i in range(4)], 0))
        self.apply(good)
        # Advance the seal so index 1 is frozen.
        self.apply(self.write_request("seal.json", request("seal", [], 2)))
        report_before = self.report.read_bytes()
        # One legal write and one sealed-frontier violation: whole batch fails.
        mixed = self.write_request("mixed.json", request(
            "mixed", [{"index": 5, "value": 1.0}, {"index": 1, "value": 2.0}], 2))
        self.apply(mixed, expect=2)
        status = self.status()
        self.assertEqual(status["committed_generation"], 2)
        self.assertEqual(self.report.read_bytes(), report_before)
        # Even the legal write from the rejected batch never landed.
        report = json.loads(self.report.read_text())
        self.assertEqual([w["status"] for w in report["windows"]], ["complete"])
        # Same for a duplicate-index batch.
        dup = self.write_request("dup.json", request(
            "dup", [{"index": 5, "value": 1.0}, {"index": 5, "value": 2.0}], 2))
        self.apply(dup, expect=2)
        self.assertEqual(self.status()["committed_generation"], 2)

    def test_deleting_highest_index_keeps_horizon(self):
        self.init()
        r1 = self.write_request("r1.json", request(
            "r1", [{"index": i, "value": float(i)} for i in range(8)], 0))
        self.apply(r1)
        r2 = self.write_request("r2.json", request("r2", [{"index": 7, "value": None}], 0))
        self.apply(r2)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["horizon"], 8)
        self.assertEqual(
            [w["status"] for w in report["windows"]], ["complete", "complete", "gap"]
        )

    def test_index_order_does_not_matter(self):
        self.init()
        ascending = self.write_request("a.json", request(
            "a", [{"index": i, "value": float(i)} for i in range(6)], 0))
        self.apply(ascending)
        first = self.report.read_bytes()
        second_root = Path(self.temp.name) / "clone"
        second_root.mkdir()
        db2 = second_root / "wb.db"
        report2 = second_root / "report.json"
        self.run_cli("init", "--db", db2, "--config", self.config_path, "--report", report2)
        shuffled = second_root / "b.json"
        shuffled.write_text(json.dumps(request(
            "b", [{"index": i, "value": float(i)} for i in range(5, -1, -1)], 0)))
        self.run_cli("apply", "--db", db2, "--source", shuffled)
        self.assertEqual(
            json.loads(report2.read_text())["windows"],
            json.loads(first)["windows"],
        )

    def test_replay_and_old_request_after_newer_generation(self):
        self.init()
        r1 = self.write_request("r1.json", request("r1", [{"index": 0, "value": 1}], 0))
        r2 = self.write_request("r2.json", request("r2", [{"index": 1, "value": 2}], 0))
        self.apply(r1)
        self.apply(r2)
        self.assertEqual(self.status()["committed_generation"], 2)
        # Identical replay of the older request: no new generation.
        proc = self.apply(r1)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["replay"])
        self.assertEqual(payload["replayed_generation"], 1)
        self.assertEqual(self.status()["committed_generation"], 2)
        latest = self.report.read_bytes()
        self.assertEqual(json.loads(latest)["generation"], 2)
        # Same id, different bytes is rejected even after newer generations.
        conflict = self.write_request("r1b.json", request(
            "r1", [{"index": 0, "value": 9}], 0))
        self.apply(conflict, expect=2)
        self.assertEqual(self.status()["committed_generation"], 2)
        self.assertEqual(self.report.read_bytes(), latest)

    def test_invalid_request_preserves_report_bytes(self):
        self.init()
        empty_report = self.report.read_bytes()
        r1 = self.write_request("r1.json", request("r1", [{"index": 0, "value": 1}], 0))
        self.apply(r1)
        good_report = self.report.read_bytes()
        for body in [
            "{}",
            '{"request_id": "x", "writes": [{"index": 0, "value": 1}], "seal": 0, "z": 1}',
            '{"request_id": "x", "writes": [{"index": 0, "value": NaN}], "seal": 0}',
            '{"request_id": "x", "writes": [{"index": true, "value": 1}], "seal": 0}',
        ]:
            bad = self.root / "bad.json"
            bad.write_text(body)
            self.apply(bad, expect=2)
            self.assertEqual(self.report.read_bytes(), good_report)
        # A database with no successful apply at all keeps the gen-0 bytes.
        fresh_root = Path(self.temp.name) / "fresh"
        fresh_root.mkdir()
        db2, report2 = fresh_root / "wb.db", fresh_root / "report.json"
        self.run_cli("init", "--db", db2, "--config", self.config_path, "--report", report2)
        zero_bytes = report2.read_bytes()
        bad = fresh_root / "bad.json"
        bad.write_text('{"request_id": "x", "writes": [], "seal": -1}')
        self.run_cli("apply", "--db", db2, "--source", bad, expect=2)
        self.assertEqual(report2.read_bytes(), zero_bytes)

    # -- path safety ---------------------------------------------------------

    def test_distinct_files_and_symlink_report_rejected(self):
        same = self.root / "same.json"
        same.write_text(json.dumps(CONFIG))
        self.run_cli("init", "--db", same, "--config", self.config_path,
                     "--report", self.report, expect=2)
        self.run_cli("init", "--db", self.db, "--config", same,
                     "--report", same, expect=2)
        link = self.root / "link.json"
        link.symlink_to(self.root / "target.json")
        self.run_cli("init", "--db", self.db, "--config", self.config_path,
                     "--report", link, expect=2)
        self.assertFalse(self.db.exists())

    def test_apply_source_must_differ_from_db_and_report(self):
        self.init()
        self.apply(self.db, expect=2)
        self.apply(self.report, expect=2)

    def test_report_path_persisted_absolute_across_working_directory(self):
        self.init()
        r1 = self.write_request("r1.json", request("r1", [{"index": 0, "value": 1}], 0))
        other_cwd = Path(self.temp.name) / "cwd"
        other_cwd.mkdir()
        # Relative DB path from a different working directory still publishes
        # to the absolute report path captured at init time.
        rel_db = Path(os.path.relpath(self.db, other_cwd))
        rel_src = Path(os.path.relpath(r1, other_cwd))
        self.run_cli("apply", "--db", rel_db, "--source", rel_src, cwd=other_cwd)
        self.assertTrue(self.report.exists())
        self.assertEqual(json.loads(self.report.read_text())["generation"], 1)

    # -- crash injection and recovery ---------------------------------------

    def crash_apply(self, source, hook):
        env = dict(self.env)
        env["SIGSPECTRUM_WB_CRASH"] = hook
        proc = subprocess.run(
            [sys.executable, *MODULE, "apply", "--db", self.db, "--source", source],
            capture_output=True, text=True, env=env,
        )
        self.assertNotEqual(proc.returncode, 0)
        return proc

    def test_crash_after_commit_then_recovery_without_source(self):
        self.init()
        r1 = self.write_request("r1.json", request(
            "r1", [{"index": i, "value": float(i)} for i in range(8)], 2))
        self.crash_apply(r1, "after_commit")
        # Database committed, file still holds generation 0.
        self.assertEqual(json.loads(self.report.read_text())["generation"], 0)
        # Remove the source file: recovery must rely on stored bytes only.
        r1.unlink()
        status = self.status()
        self.assertEqual(status["committed_generation"], 1)
        self.assertEqual(status["published_generation"], 1)
        self.assertIsNone(status["pending"])
        report = json.loads(self.report.read_text())
        self.assertEqual(report["generation"], 1)
        self.assertEqual(report["horizon"], 8)

    def test_crash_after_replace_then_recovery(self):
        self.init()
        r1 = self.write_request("r1.json", request("r1", [{"index": 0, "value": 1}], 0))
        self.crash_apply(r1, "after_replace")
        # File already replaced, confirmation marker is still behind.
        self.assertEqual(json.loads(self.report.read_text())["generation"], 1)
        status = self.status()
        self.assertEqual(status["committed_generation"], 1)
        self.assertEqual(status["published_generation"], 1)
        self.assertIsNone(status["pending"])

    def test_recovery_runs_before_new_request_and_old_request_handling(self):
        self.init()
        r1 = self.write_request("r1.json", request(
            "r1", [{"index": i, "value": float(i)} for i in range(8)], 2))
        r2 = self.write_request("r2.json", request(
            "r2", [{"index": 3, "value": -2.0}], 2))
        self.crash_apply(r1, "after_commit")
        r1.unlink()

        # An invalid new request still triggers recovery of r1's report first.
        bad = self.write_request("bad.json", request(
            "bad", [{"index": 0, "value": 1}], 2))  # index below seal
        proc = self.apply(bad, expect=2)
        # The old generation-1 report got published despite the rejected request.
        report = json.loads(self.report.read_text())
        self.assertEqual(report["generation"], 1)
        status = self.status()
        self.assertEqual(status["published_generation"], 1)

        # Replay of the old request without its source is impossible, but an
        # identical-bytes re-created source replays without a new generation.
        r1.write_text(json.dumps(request(
            "r1", [{"index": i, "value": float(i)} for i in range(8)], 2)))
        proc = self.apply(r1)
        self.assertTrue(json.loads(proc.stdout)["replay"])
        self.assertEqual(self.status()["committed_generation"], 1)

        # Then the next genuinely new request proceeds normally.
        self.apply(r2)
        status = self.status()
        self.assertEqual(status["committed_generation"], 2)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["generation"], 2)
        self.assertEqual(report["windows"][0]["status"], "complete")

    def test_repeated_crash_cycles_converge_without_double_counting(self):
        self.init()
        # Three requests jointly fill indexes 0..3 (window 0's full frame).
        batches = [
            ("r0", [{"index": 0, "value": 1.0}, {"index": 1, "value": 2.0}]),
            ("r1", [{"index": 2, "value": 3.0}]),
            ("r2", [{"index": 3, "value": 4.0}]),
        ]
        sources = [self.write_request("%s.json" % rid, request(rid, writes, 0))
                   for rid, writes in batches]
        self.apply(sources[0])
        # Crash while committing the second request, then recover.
        self.crash_apply(sources[1], "after_commit")
        self.status()
        # Crash again on the third request.
        self.crash_apply(sources[2], "after_replace")
        status = self.status()
        self.assertEqual(status["committed_generation"], 3)
        self.assertEqual(status["published_generation"], 3)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["generation"], 3)
        self.assertEqual(report["windows"][0]["status"], "complete")
        self.assertEqual(report["horizon"], 4)

    def test_seal_only_request_creates_sealed_gap_windows(self):
        # No writes at all: advancing the seal must grow the horizon so the
        # whole windows under it are listed and frozen as sealed gaps.
        self.init()
        seal_req = self.write_request("seal.json", request("seal", [], 8))
        self.apply(seal_req)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["horizon"], 8)
        self.assertEqual(report["seal"], 8)
        self.assertEqual(
            [(w["start"], w["status"], w["sealed"]) for w in report["windows"]],
            [(0, "gap", True), (2, "gap", True), (4, "gap", True)],
        )
        self.assertEqual(report["sealed_gap_count"], 3)
        self.assertEqual(report["sealed_complete_count"], 0)
        self.assertIsNone(report["sealed_psd"])
        self.assertIsNone(report["live_psd"])
        # A later write inside the sealed region is rejected and cannot heal it.
        late = self.write_request("late.json", request("late", [{"index": 0, "value": 1}], 8))
        self.apply(late, expect=2)
        self.assertEqual(json.loads(self.report.read_text())["sealed_gap_count"], 3)

    def test_horizon_also_advances_from_partial_seal(self):
        self.init()
        # Two written points, then seal 4: window start 0 exists and is a gap.
        req = self.write_request("r.json", request(
            "r", [{"index": 0, "value": 1}, {"index": 1, "value": 2}], 4))
        self.apply(req)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["horizon"], 4)
        self.assertEqual(
            [(w["start"], w["status"], w["sealed"]) for w in report["windows"]],
            [(0, "gap", True)],
        )

    def test_init_on_pending_database_recovers_then_refuses_with_old_path(self):
        self.init()
        r1 = self.write_request("r1.json", request("r1", [{"index": 0, "value": 1}], 0))
        self.crash_apply(r1, "after_commit")
        self.assertEqual(json.loads(self.report.read_text())["generation"], 0)
        new_report = self.root / "must-not-be-used.json"
        proc = self.run_cli("init", "--db", self.db, "--config", self.config_path,
                            "--report", new_report, expect=2)
        payload = json.loads(proc.stdout)
        # Recovery of the previous generation happens before the refusal.
        self.assertEqual(payload["pending"], None)
        self.assertEqual(payload["published_generation"], 1)
        self.assertEqual(json.loads(self.report.read_text())["generation"], 1)
        # The new config/path never replaces the stored destination.
        self.assertFalse(new_report.exists())
        self.assertEqual(payload["report_path"], str(self.report.resolve()))

    def test_pending_generation_blocks_and_identical_retry_does_not_advance(self):
        self.init()
        r1 = self.write_request("r1.json", request(
            "r1", [{"index": i, "value": float(i)} for i in range(8)], 2))
        self.crash_apply(r1, "after_commit")
        # While generation 1 is pending, an identical-bytes retry of r1 must be
        # recognized as a replay (no second generation) once recovery completes.
        proc = self.apply(r1)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["replay"])
        self.assertEqual(payload["replayed_generation"], 1)
        self.assertEqual(self.status()["committed_generation"], 1)
        self.assertIsNone(self.status()["pending"])

    def test_faulty_run_matches_fault_free_run_byte_for_byte(self):
        # Same request sequence in two databases; one suffers both crash
        # points and source removal, the other runs cleanly.  Final report
        # bytes, generations and counts must be identical.
        sequence = [
            ("r1", [{"index": i, "value": v} for i, v in enumerate(
                [0, 1, 0, -1, 0, 1, 0, -1])], 2),
            ("r2", [{"index": 3, "value": -2}], 2),
            ("r3", [{"index": 4, "value": None}], 2),
            ("r4", [], 8),
        ]

        def fault_free():
            clean = Path(self.temp.name) / "clean"
            clean.mkdir()
            db, rep = clean / "wb.db", clean / "report.json"
            self.run_cli("init", "--db", db, "--config", self.config_path, "--report", rep)
            for i, (rid, writes, seal) in enumerate(sequence):
                src = clean / ("%s.json" % rid)
                src.write_text(json.dumps(request(rid, writes, seal)))
                self.run_cli("apply", "--db", db, "--source", src)
            status = json.loads(self.run_cli("status", "--db", db).stdout)
            return status, rep.read_bytes()

        # Faulty run on the main database.
        self.init()
        for i, (rid, writes, seal) in enumerate(sequence):
            src = self.write_request("%s.json" % rid, request(rid, writes, seal))
            if i == 1:
                self.crash_apply(src, "after_commit")
                src.unlink()  # source gone before recovery
                src.write_text(json.dumps(request(rid, writes, seal)))
                self.status()
            elif i == 3:
                self.crash_apply(src, "after_replace")
            else:
                self.apply(src)
        faulty_status = self.status()
        clean_status, clean_bytes = fault_free()
        self.assertEqual(faulty_status["committed_generation"],
                         clean_status["committed_generation"])
        self.assertEqual(faulty_status["published_generation"],
                         clean_status["published_generation"])
        faulty_report = json.loads(self.report.read_text())
        clean_report = json.loads(clean_bytes)
        self.assertEqual(faulty_report["generation"], clean_report["generation"])
        self.assertEqual(faulty_report["windows"], clean_report["windows"])
        self.assertEqual(faulty_report["live_psd"], clean_report["live_psd"])
        self.assertEqual(faulty_report["sealed_psd"], clean_report["sealed_psd"])
        self.assertEqual(faulty_report["sealed_gap_count"],
                         clean_report["sealed_gap_count"])
        self.assertEqual(self.report.read_bytes(), clean_bytes)

    def test_init_publication_failure_leaves_recoverable_pending_generation(self):
        # Block report creation by making a file occupy the parent directory.
        blocker = self.root / "blocker"
        blocker.write_text("x")
        blocked_report = blocker / "report.json"
        proc = self.run_cli("init", "--db", self.db, "--config", self.config_path,
                            "--report", blocked_report, expect=2)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["committed_generation"], 0)
        self.assertEqual(payload["pending"], 0)
        # The database exists and refuses re-init while carrying the pending gen.
        self.assertTrue(self.db.exists())
        blocker.unlink()
        status = self.status()
        self.assertIsNone(status["pending"])
        # Recovery published to the path originally captured at init time.
        recovered = blocker / "report.json"
        self.assertEqual(json.loads(recovered.read_text())["generation"], 0)

    def test_publish_failure_status_reports_pending_and_exit_2(self):
        self.init()
        r1 = self.write_request("r1.json", request("r1", [{"index": 0, "value": 1}], 0))
        # Make the report path unusable for replacement after commit.
        os.unlink(self.report)
        self.report.mkdir()
        proc = self.apply(r1, expect=2)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["pending"], 1)
        self.assertEqual(payload["committed_generation"], 1)
        self.assertEqual(payload["published_generation"], 0)
        # status must not lie about success.
        proc = self.run_cli("status", "--db", self.db, expect=2)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["pending"], 1)
        # Repair the destination and confirm recovery publishes generation 1.
        self.report.rmdir()
        status = self.status()
        self.assertEqual(status["pending"], None)
        self.assertEqual(json.loads(self.report.read_text())["generation"], 1)


if __name__ == "__main__":
    unittest.main()
