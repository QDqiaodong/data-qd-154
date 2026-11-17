"""signal-spectrum-workbench: init / apply / status command line.

Order of operations on every command
------------------------------------
1. Open (or create) the SQLite database.
2. Recover any generation that was committed but never confirmed as published,
   using only bytes stored in the database -- the original request file need
   not exist anymore.
3. Only then handle the new request.  Recovery of an older pending generation
   may therefore replace the report file even when the new request itself is
   invalid; an invalid request never touches samples, requests or generations.
"""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from ..common import read
from . import engine
from .publish import publish_report, recover_pending
from .store import Store, StoreError

DESCRIPTION = "SQLite-persisted continuous-sampling Welch workbench"
# Validation/usage errors and publication failures both use exit code 2;
# nothing in the workbench is a successful-but-partial operation.
EXIT_FAILURE = 2


def _abs(path):
    return os.path.abspath(os.fspath(path))


def _same_target(left, right):
    left_abs, right_abs = _abs(left), _abs(right)
    if left_abs == right_abs:
        return True
    if os.path.exists(left_abs) and os.path.exists(right_abs):
        try:
            return os.path.samefile(left_abs, right_abs)
        except OSError:
            return False
    return False


def _status_payload(store):
    return {
        "committed_generation": store.committed_generation,
        "published_generation": store.published_generation,
        "pending": store.pending_generation,
        "seal": store.seal,
        "horizon": store.horizon,
        "report_path": store.report_path,
    }


def _fail(message, payload=None):
    print(message, file=sys.stderr)
    if payload is not None:
        print(json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return EXIT_FAILURE


def _run_init(args):
    db_path = _abs(args.db)
    config_path = _abs(args.config)
    report_path = _abs(args.report)

    if _same_target(db_path, config_path) or _same_target(db_path, report_path) \
            or _same_target(config_path, report_path):
        return _fail("database, config and report must be distinct files")
    if os.path.islink(report_path):
        return _fail("report path cannot be a symlink: %s" % report_path)

    # Opening an existing database never overwrites it: recover the pending
    # generation under the stored config, then refuse to re-initialize.
    if os.path.exists(db_path):
        store = Store.open(db_path)
        try:
            recover_pending(store)
        except (OSError, ValueError, StoreError) as error:
            payload = _status_payload(store)
            return _fail("recovery failed: %s" % error, payload)
        payload = _status_payload(store)
        return _fail("database already exists; init refuses to overwrite: %s" % db_path, payload)

    try:
        config_data, _ = read(config_path)
        config = engine.validate_config(config_data)
    except (ValueError, TypeError, KeyError, OSError, OverflowError, RecursionError) as error:
        return _fail(str(error))

    try:
        store = Store.create(db_path, config, report_path)
        try:
            publish_report(store, 0)
        except (OSError, ValueError, StoreError) as error:
            # The generation-0 commit exists; keep the pending contract so the
            # next command repairs the file without recreating the database.
            payload = _status_payload(store)
            return _fail("initial publication failed: %s" % error, payload)
        payload = _status_payload(store)
        payload["config"] = config
    finally:
        store.close()
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


def _run_apply(args):
    db_path = _abs(args.db)
    source_path = _abs(args.source)
    try:
        store = Store.open(db_path)
    except (StoreError, OSError) as error:
        return _fail(str(error))

    try:
        # Recovery always precedes request handling and never depends on the
        # request file.
        try:
            recover_pending(store)
        except (OSError, ValueError, StoreError) as error:
            return _fail("recovery failed: %s" % error, _status_payload(store))

        if _same_target(db_path, source_path) or _same_target(store.report_path, source_path):
            return _fail("database, request and report must be distinct files")

        request_data, digest = read(source_path)
        request_id = request_data.get("request_id") if isinstance(request_data, dict) else None
        if not isinstance(request_id, str):
            # No usable identifier: fall through to full strict validation,
            # which will reject the malformed request before any commit.
            existing = None
        else:
            existing = store.lookup_request(request_id)

        if existing is not None:
            if existing["sha256"] != digest:
                return _fail("request_id %s was already committed with different bytes"
                             % request_id)
            # Identical retry: no new generation, no report overwrite, even if
            # newer generations have since been committed.
            payload = _status_payload(store)
            payload["request_id"] = request_id
            payload["replayed_generation"] = existing["generation"]
            payload["replay"] = True
            print(json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False))
            return 0

        request_id, writes, new_seal = engine.validate_request(
            request_data, old_seal=store.seal
        )
        generation, report = store.commit_request(request_id, digest, writes, new_seal)
        try:
            publish_report(store, generation)
        except (OSError, ValueError, StoreError) as error:
            return _fail("publication failed; generation %d is pending: %s"
                         % (generation, error), _status_payload(store))
        payload = _status_payload(store)
        payload["request_id"] = request_id
        payload["generation"] = generation
        payload["replay"] = False
        payload["live_complete_count"] = report["live_complete_count"]
        payload["sealed_complete_count"] = report["sealed_complete_count"]
        payload["sealed_gap_count"] = report["sealed_gap_count"]
    finally:
        store.close()
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


def _run_status(args):
    db_path = _abs(args.db)
    try:
        store = Store.open(db_path)
    except (StoreError, OSError) as error:
        return _fail(str(error))
    try:
        try:
            recover_pending(store)
        except (OSError, ValueError, StoreError) as error:
            # Do not claim success: the pending state is reported honestly and
            # the command exits non-zero.
            return _fail("recovery failed: %s" % error, _status_payload(store))
        payload = _status_payload(store)
        payload["config"] = store.config
    finally:
        store.close()
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init", help="create a workbench database and empty report")
    init_parser.add_argument("--db", type=Path, required=True)
    init_parser.add_argument("--config", type=Path, required=True)
    init_parser.add_argument("--report", type=Path, required=True)
    init_parser.set_defaults(handler=_run_init)

    apply_parser = sub.add_parser("apply", help="apply one validated request batch")
    apply_parser.add_argument("--db", type=Path, required=True)
    apply_parser.add_argument("--source", type=Path, required=True)
    apply_parser.set_defaults(handler=_run_apply)

    status_parser = sub.add_parser("status", help="show committed/published generations")
    status_parser.add_argument("--db", type=Path, required=True)
    status_parser.set_defaults(handler=_run_status)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError, sqlite3.Error, StoreError) as error:
        # Last-resort guard so any contract violation still exits 2 instead of
        # producing a raw traceback with exit code 1.
        print(str(error), file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
