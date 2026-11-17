"""SQLite commit layer for the sampling workbench.

Commit protocol
---------------
Every accepted request is written in one SQLite transaction: the request
identity, revised samples/tombstones, new frontier, the next generation and the
fully serialized report of that generation all become visible together.  The
report file is published *after* the transaction commits.  A second
``published_generation`` marker records how far file publication has reached,
so a process killed between commit and file replacement, or between
replacement and confirmation, is repaired on the next command by republishing
the report bytes stored inside the database -- the original request file is
not needed.
"""
import json
import os
import sqlite3

from .engine import build_report

SCHEMA_VERSION = 1


class StoreError(Exception):
    pass


class Store:
    def __init__(self, connection, path):
        self.conn = connection
        self.path = path
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._meta = dict(self.conn.execute("SELECT key, value FROM meta").fetchall())

    # -- connection lifecycle ------------------------------------------------

    @classmethod
    def create(cls, db_path, config, report_path):
        """Create a brand-new database.  Existing files are never overwritten."""
        if os.path.exists(db_path):
            raise StoreError("database already exists: %s" % db_path)
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE samples (
                    sample_index INTEGER PRIMARY KEY,
                    value REAL,
                    deleted INTEGER NOT NULL CHECK (deleted IN (0, 1))
                );
                CREATE TABLE requests (
                    request_id TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL,
                    generation INTEGER NOT NULL
                );
                CREATE TABLE reports (
                    generation INTEGER PRIMARY KEY,
                    request_id TEXT,
                    content TEXT NOT NULL
                );
                """
            )
            conn.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                [
                    ("schema_version", str(SCHEMA_VERSION)),
                    ("config", json.dumps(config, sort_keys=True)),
                    ("report_path", report_path),
                    ("seal", "0"),
                    ("horizon", "0"),
                    ("committed_generation", "0"),
                    # -1 means even the generation-0 report has not been
                    # confirmed published yet, so a failed init still leaves a
                    # recoverable pending generation.
                    ("published_generation", "-1"),
                ],
            )
            report = build_report(
                generation=0,
                request_id=None,
                config=config,
                samples={},
                tombstone=set(),
                horizon=0,
                seal=0,
            )
            conn.execute(
                "INSERT INTO reports(generation, request_id, content) VALUES (0, NULL, ?)",
                (json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False),),
            )
            conn.commit()
        except BaseException:
            conn.close()
            # We created this path ourselves a few lines earlier (existence was
            # checked on entry); remove the half-initialized files so a fixed
            # init can be retried instead of being blocked by a broken DB.
            for suffix in ("", "-wal", "-shm"):
                leftover = db_path + suffix
                if os.path.exists(leftover):
                    os.unlink(leftover)
            raise
        return cls(conn, db_path)

    @classmethod
    def open(cls, db_path):
        if not os.path.exists(db_path):
            raise StoreError("database does not exist: %s" % db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        return cls(conn, db_path)

    def close(self):
        self.conn.close()

    # -- meta helpers --------------------------------------------------------

    @property
    def config(self):
        return json.loads(self._meta["config"])

    @property
    def report_path(self):
        return self._meta["report_path"]

    @property
    def seal(self):
        return int(self._meta["seal"])

    @property
    def horizon(self):
        return int(self._meta["horizon"])

    @property
    def committed_generation(self):
        return int(self._meta["committed_generation"])

    @property
    def published_generation(self):
        return int(self._meta["published_generation"])

    @property
    def pending_generation(self):
        """Generation awaiting file publication, or ``None``."""
        if self.committed_generation > self.published_generation:
            return self.committed_generation
        return None

    def refresh_meta(self):
        self._meta = dict(self.conn.execute("SELECT key, value FROM meta").fetchall())

    # -- request identity ----------------------------------------------------

    def lookup_request(self, request_id):
        row = self.conn.execute(
            "SELECT sha256, generation FROM requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        return {"sha256": row[0], "generation": row[1]}

    # -- state ---------------------------------------------------------------

    def load_state(self):
        samples = {}
        tombstone = set()
        for index, value, deleted in self.conn.execute(
            "SELECT sample_index, value, deleted FROM samples"
        ).fetchall():
            if deleted:
                tombstone.add(index)
            else:
                samples[index] = value
        return samples, tombstone

    def report_content(self, generation):
        row = self.conn.execute(
            "SELECT content FROM reports WHERE generation = ?", (generation,)
        ).fetchone()
        if row is None:
            raise StoreError("unknown generation: %d" % generation)
        return row[0]

    # -- commit --------------------------------------------------------------

    def commit_request(self, request_id, sha256, writes, new_seal):
        """Validate-free commit: caller has validated everything.

        Applies the writes to the state layer, advances the monotonic seal and
        horizon, rebuilds the report for the next generation and stores all of
        it atomically.  Returns the new generation and the report object.
        """
        config = self.config
        old_horizon = self.horizon
        old_seal = self.seal
        samples, tombstone = self.load_state()

        max_index = -1
        for index, value in writes:
            max_index = max(max_index, index)
            if value is None:
                tombstone.add(index)
                samples.pop(index, None)
            else:
                tombstone.discard(index)
                samples[index] = value
        new_horizon = max(old_horizon, max_index + 1, new_seal)
        new_generation = self.committed_generation + 1

        report = build_report(
            generation=new_generation,
            request_id=request_id,
            config=config,
            samples=samples,
            tombstone=tombstone,
            horizon=new_horizon,
            seal=new_seal,
        )
        content = json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False)

        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.executemany(
                "INSERT INTO samples(sample_index, value, deleted) VALUES (?, ?, ?) "
                "ON CONFLICT(sample_index) DO UPDATE SET value=excluded.value, deleted=excluded.deleted",
                [
                    (index, None if value is None else value, 1 if value is None else 0)
                    for index, value in writes
                ],
            )
            self.conn.execute(
                "INSERT INTO requests(request_id, sha256, generation) VALUES (?, ?, ?)",
                (request_id, sha256, new_generation),
            )
            self.conn.execute(
                "INSERT INTO reports(generation, request_id, content) VALUES (?, ?, ?)",
                (new_generation, request_id, content),
            )
            self.conn.executemany(
                "UPDATE meta SET value=? WHERE key=?",
                [
                    (str(new_seal), "seal"),
                    (str(new_horizon), "horizon"),
                    (str(new_generation), "committed_generation"),
                ],
            )
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        self.refresh_meta()
        return new_generation, report

    def mark_published(self, generation):
        if generation < self.published_generation:
            raise StoreError("published generation cannot move backwards")
        self.conn.execute(
            "UPDATE meta SET value=? WHERE key='published_generation'", (str(generation),)
        )
        self.conn.commit()
        self.refresh_meta()
