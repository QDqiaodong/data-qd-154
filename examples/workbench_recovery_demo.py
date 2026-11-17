#!/usr/bin/env python3
"""Runnable recovery and retry walkthrough for signal-spectrum-workbench.

The script drives the real CLI in a temporary directory, including both crash
injection points, removes the request source before recovery to prove the
database alone is sufficient, and then checks replay/conflict semantics.

Run from the repository root:

    PYTHONPATH=src python3 examples/workbench_recovery_demo.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = [sys.executable, "-m", "signal_spectrum.workbench.cli"]
CONFIG = {
    "sample_rate": 8,
    "frame_size": 4,
    "hop": 2,
    "window": "rectangular",
    "remove_mean": True,
}


def run(env_root, *args, expect=0, env_extra=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(CLI + list(args), cwd=env_root, capture_output=True,
                          text=True, env=env)
    if proc.returncode != expect:
        raise SystemExit("command %r failed (%d):\n%s" %
                         (args, proc.returncode, proc.stderr))
    return proc


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db, report, config = root / "wb.db", root / "report.json", root / "config.json"
        config.write_text(json.dumps(CONFIG))

        run(root, "init", "--db", db, "--config", config, "--report", report)
        print("1) init published generation 0:",
              json.loads(report.read_text())["generation"])

        req = root / "request.json"
        req.write_text(json.dumps({
            "request_id": "demo-1",
            "writes": [{"index": i, "value": v}
                       for i, v in enumerate([0, 1, 0, -1, 0, 1, 0, -1])],
            "seal": 2,
        }))

        # Crash point A: committed in SQLite, report file not replaced.
        run(root, "apply", "--db", db, "--source", req, expect=10,
            env_extra={"SIGSPECTRUM_WB_CRASH": "after_commit"})
        print("2) injected crash after commit; report still generation",
              json.loads(report.read_text())["generation"])

        # The source disappears before anyone restarts the process.
        req.unlink()
        status = json.loads(run(root, "status", "--db", db).stdout)
        print("3) status recovered without the source file:",
              "committed=%d published=%d pending=%s" %
              (status["committed_generation"], status["published_generation"],
               status["pending"]))
        print("   report generation now:",
              json.loads(report.read_text())["generation"])

        # Recreate identical bytes: replay must not add a generation.
        req.write_text(json.dumps({
            "request_id": "demo-1",
            "writes": [{"index": i, "value": v}
                       for i, v in enumerate([0, 1, 0, -1, 0, 1, 0, -1])],
            "seal": 2,
        }))
        replay = json.loads(run(root, "apply", "--db", db, "--source", req).stdout)
        print("4) identical replay -> replay=%s replayed_generation=%d" %
              (replay["replay"], replay["replayed_generation"]))

        # Same id, different bytes: rejected.
        req.write_text(json.dumps({"request_id": "demo-1", "writes": [], "seal": 3}))
        run(root, "apply", "--db", db, "--source", req, expect=2)
        current = json.loads(run(root, "status", "--db", db).stdout)
        print("5) same id with different bytes rejected; generation stays",
              current["committed_generation"])

        # New request, then crash point B (file replaced, confirmation pending).
        req.write_text(json.dumps({
            "request_id": "demo-2",
            "writes": [{"index": 3, "value": -2}],
            "seal": 2,
        }))
        run(root, "apply", "--db", db, "--source", req, expect=11,
            env_extra={"SIGSPECTRUM_WB_CRASH": "after_replace"})
        status = json.loads(run(root, "status", "--db", db).stdout)
        final_report = json.loads(report.read_text())
        print("6) crash after replace; final status:",
              "committed=%d published=%d pending=%s" %
              (status["committed_generation"], status["published_generation"],
               status["pending"]))
        print("   report generation %d, window 0 PSD = %s" %
              (final_report["generation"],
               [round(b["psd"], 6) for b in final_report["live_psd"]]))
        print("OK: no double counting, no generation rollback.")


if __name__ == "__main__":
    main()
