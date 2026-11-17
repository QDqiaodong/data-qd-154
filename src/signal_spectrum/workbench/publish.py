"""Atomic report-file publication and crash-recovery hooks.

Publication is the only step that happens *after* the SQLite commit.  The
report bytes live in the database, so recovery never needs the original
request input: any command first republishes the committed generation that the
file layer has not confirmed yet.

Test hooks (environment variables) inject the two termination points that a
real crash could hit; they are only meant for the bundled tests:

* ``SIGSPECTRUM_WB_CRASH=after_commit``  -- committed, file not replaced
* ``SIGSPECTRUM_WB_CRASH=after_replace`` -- file replaced, confirmation missing
"""
import os
import tempfile
from pathlib import Path

CRASH_AFTER_COMMIT = "after_commit"
CRASH_AFTER_REPLACE = "after_replace"


def _crash_hook(point):
    if os.environ.get("SIGSPECTRUM_WB_CRASH") == point:
        os._exit(10 if point == CRASH_AFTER_COMMIT else 11)


def publish_report(store, generation):
    """Publish one committed generation to the fixed report path.

    The destination is created with a sibling temporary file and atomically
    replaced with ``os.replace``; symlink destinations are refused and a
    directory in the way makes publication fail so the pending contract
    applies instead of corrupting anything.
    """
    dest = Path(store.report_path)
    if dest.is_symlink():
        raise ValueError("report path cannot be a symlink: %s" % dest)
    if dest.exists() and dest.is_dir():
        raise IsADirectoryError("report path is a directory: %s" % dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    content = store.report_content(generation)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=dest.parent, delete=False
        ) as stream:
            temp_name = stream.name
            stream.write(content)
        _crash_hook(CRASH_AFTER_COMMIT)
        os.replace(temp_name, dest)
        temp_name = None
        _crash_hook(CRASH_AFTER_REPLACE)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
    store.mark_published(generation)


def recover_pending(store):
    """Republish any committed generation whose file publication is pending."""
    pending = store.pending_generation
    if pending is not None:
        publish_report(store, pending)
    return pending
