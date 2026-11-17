"""Staging directory, durable checkpoints and atomic package publication.

On-disk layout for an acquisition targeting ``DIR``::

    DIR.staging/
        raw.bin            committed, block-validated raw prefix
        samples.ndjson     created during the final decode pass
        analysis.json      only when analysis export is requested
        checkpoint.json    durable, fsynced acquisition state
        _download.tmp      never committed; truncated away on every restart

Publication renames the whole staging directory atomically to ``DIR``.
Published directories are never overwritten and failed runs leave an
identifiable ``*.staging`` directory behind.

The only durability promise is process-termination recovery (SIGKILL/crash),
matching the rest of the project; power-loss or concurrent collectors are out
of scope.
"""
import hashlib
import json
import os
from pathlib import Path

from .protocol import CheckpointError

_CHECKPOINT_NAME = "checkpoint.json"
_RAW_NAME = "raw.bin"
_SAMPLES_NAME = "samples.ndjson"
_ANALYSIS_NAME = "analysis.json"
_MANIFEST_NAME = "manifest.json"
_TMP_NAME = "_download.tmp"
_STAGING_SUFFIX = ".staging"
_HASH_BUFFER = 65536


def staging_for(output_dir):
    output_dir = Path(output_dir)
    return output_dir.with_name(output_dir.name + _STAGING_SUFFIX)


def is_published(output_dir):
    output_dir = Path(output_dir)
    return output_dir.is_dir() and (output_dir / _MANIFEST_NAME).is_file()


def write_durable_json(path, payload):
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True, ensure_ascii=False,
                  allow_nan=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    # fsync the directory so the rename itself is durable.
    try:
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


class Staging:
    """Filesystem state for one acquisition attempt."""

    def __init__(self, output_dir):
        self.output_dir = Path(output_dir)
        self.path = staging_for(self.output_dir)
        self.raw_path = self.path / _RAW_NAME
        self.samples_path = self.path / _SAMPLES_NAME
        self.analysis_path = self.path / _ANALYSIS_NAME
        self.manifest_path = self.path / _MANIFEST_NAME
        self.tmp_path = self.path / _TMP_NAME
        self.checkpoint_path = self.path / _CHECKPOINT_NAME

    # -- lifecycle ---------------------------------------------------------

    def prepare_fresh(self):
        if is_published(self.output_dir):
            raise CheckpointError(
                "output directory already published; refusing to overwrite: %s"
                % self.output_dir)
        if self.path.exists():
            raise CheckpointError(
                "staging directory already exists; use --resume or choose a "
                "new output: %s" % self.path)
        self.path.mkdir(parents=True)
        self.raw_path.touch()
        self._remove_derived()

    def open_resume(self):
        if is_published(self.output_dir):
            raise CheckpointError(
                "output directory already published; refusing to overwrite: %s"
                % self.output_dir)
        if not self.path.is_dir() or not self.checkpoint_path.is_file():
            raise CheckpointError(
                "no resumable staging directory found at %s" % self.path)
        if not self.raw_path.is_file():
            raise CheckpointError("staging is missing raw.bin")
        self._remove_derived()
        if self.tmp_path.exists():
            # Bytes that never reached a checkpoint are uncommitted noise.
            self.tmp_path.unlink()

    def truncate_to_committed(self, committed_offset):
        """Drop any raw bytes written after the last durable checkpoint.

        A crash between raw append and checkpoint leaves a tail that was never
        confirmed; it must never be mistaken for a completed block.
        """
        current = self.raw_path.stat().st_size
        if current < committed_offset:
            raise CheckpointError(
                "raw.bin shrank below the committed offset")
        if current > committed_offset:
            with open(self.raw_path, "r+b") as stream:
                stream.truncate(committed_offset)
                stream.flush()
                os.fsync(stream.fileno())

    def _remove_derived(self):
        for name in (_SAMPLES_NAME, _ANALYSIS_NAME, _MANIFEST_NAME):
            path = self.path / name
            if path.exists():
                path.unlink()

    # -- checkpoints -------------------------------------------------------

    def write_checkpoint(self, payload):
        write_durable_json(self.checkpoint_path, payload)

    def read_checkpoint(self):
        try:
            with open(self.checkpoint_path, "rb") as stream:
                raw = stream.read(65537)
        except OSError as error:
            raise CheckpointError("cannot read checkpoint: " + str(error))
        if len(raw) > 65536:
            raise CheckpointError("checkpoint exceeds 64 KiB")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CheckpointError("corrupt checkpoint: " + str(error))

    def verify_prefix(self, expected_offset, expected_sha):
        """Re-hash the committed prefix; tamper is always fatal.

        Only the first ``expected_offset`` bytes are validated so a
        post-checkpoint crash tail can be truncated afterwards instead of
        being treated as tampering.
        """
        actual_size = self.raw_path.stat().st_size
        if actual_size < expected_offset:
            raise CheckpointError(
                "raw.bin size %d is shorter than committed offset %d"
                % (actual_size, expected_offset))
        hasher = hashlib.sha256()
        remaining = expected_offset
        with open(self.raw_path, "rb") as stream:
            while remaining:
                chunk = stream.read(min(_HASH_BUFFER, remaining))
                if not chunk:
                    raise CheckpointError(
                        "raw prefix ended before the committed offset")
                hasher.update(chunk)
                remaining -= len(chunk)
        if hasher.hexdigest() != expected_sha:
            raise CheckpointError(
                "committed raw prefix SHA256 mismatch; local prefix was "
                "tampered with")
        return hasher.hexdigest()

    # -- block commitment --------------------------------------------------

    def append_block(self, download_path, block_length):
        """Append a fully verified block and fsync it before checkpointing."""
        with open(download_path, "rb") as source, \
                open(self.raw_path, "ab") as target:
            remaining = block_length
            while remaining:
                chunk = source.read(min(_HASH_BUFFER, remaining))
                if not chunk:
                    raise CheckpointError("download temp ended prematurely")
                target.write(chunk)
                remaining -= len(chunk)
            target.flush()
            os.fsync(target.fileno())

    def raw_size(self):
        return self.raw_path.stat().st_size

    def remove_tmp(self):
        if self.tmp_path.exists():
            self.tmp_path.unlink()

    # -- publication -------------------------------------------------------

    def write_manifest(self, manifest):
        write_durable_json(self.manifest_path, manifest)

    def write_analysis(self, payload):
        write_durable_json(self.analysis_path, payload)

    def publish(self):
        if is_published(self.output_dir):
            raise CheckpointError(
                "output directory already published; refusing to overwrite: %s"
                % self.output_dir)
        if self.output_dir.exists():
            raise CheckpointError(
                "output path exists without a manifest; refusing to replace: %s"
                % self.output_dir)
        # Only package artefacts are published; checkpoints and temp files are
        # acquisition machinery, not traceable data.
        if self.checkpoint_path.exists():
            self.checkpoint_path.unlink()
        if self.tmp_path.exists():
            self.tmp_path.unlink()
        self.output_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.path, self.output_dir)
