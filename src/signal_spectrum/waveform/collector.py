"""Acquisition session: consistency channels, checkpoints and restart budget.

Two provably consistent collection channels are implemented; the client picks
one based on device capability, but once a channel is chosen it fulfils every
identity obligation of that channel:

``lease``
    A snapshot is atomically pinned by ``POST /leases``.  Every ranged read
    goes through ``/leases/T/...`` with ``If-Match``, so the bytes are
    immutable until the lease expires.  An HTTP 410 invalidates the whole
    candidate (a *new* lease can never be spliced onto an old prefix) and is
    handled within the restart budget.

``etag``
    Used by ETag-only devices and selectable on lease-capable devices for
    testing.  Every block is a conditional read (``If-Match`` of the current
    strong ETag); after the last byte, metadata is fetched again and must be
    byte-for-byte identical under the same ETag, proving the device never
    rotated the capture during the transfer.

Any identity change (412, changed ETag/meta/layout) discards the entire
candidate: raw prefix, partial samples and derived values, then restarts from
zero under the new identity.  Restarts are a finite budget persisted in the
checkpoint so a repeatedly changing device produces a bounded failure.
"""
import hashlib
import os

from .decode import build_analysis, decode_stream, verify_analysis
from .httpio import ConnectionFailure, HttpDevice
from .protocol import (
    BudgetExceeded,
    CheckpointError,
    IdentityChanged,
    LeaseExpired,
    ProtocolError,
)

# Fixed, public budgets.  They can be tightened for tests via environment
# variables (integers); there is no setting that makes a budget infinite.
DEFAULT_BLOCK_RETRIES = 4
DEFAULT_RESTART_BUDGET = 3
DEFAULT_TIMEOUT = 10.0
# Never ask for more than this per block even when the device allows more;
# together with the 64 KiB read buffer the working set stays far below 1 MiB.
CLIENT_BLOCK_CAP = 262_144
CRASH_AFTER_RAW = "after_raw_append"
CRASH_AFTER_CHECKPOINT = "after_checkpoint"


def _env_int(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ProtocolError("environment variable %s must be an integer" % name)
    if value < 0:
        raise ProtocolError("environment variable %s must be non-negative" % name)
    return value


def _crash_hook(point):
    if os.environ.get("SIGSPECTRUM_WF_CRASH") == point:
        os._exit(12)


class SessionConfig:
    def __init__(self, url, block_retries, restart_budget, timeout,
                 force_channel=None):
        self.url = url
        self.block_retries = block_retries
        self.restart_budget = restart_budget
        self.timeout = timeout
        self.force_channel = force_channel


def _checkpoint_payload(staging, url, meta, etag, channel, lease_token,
                        offset, sha, restarts, analysis):
    return {
        "version": 1,
        "url": url,
        "meta": meta,
        "etag": etag,
        "channel": channel,
        "lease_token": lease_token,
        "committed_offset": offset,
        "prefix_sha256": sha,
        "restarts_used": restarts,
        "analysis": ({"channel": analysis[0], "count": analysis[1]}
                     if analysis is not None else None),
    }


def _reset_candidate(staging):
    """Discard every byte belonging to the previous identity."""
    if staging.raw_path.exists():
        staging.raw_path.unlink()
    staging.raw_path.touch()
    staging.remove_tmp()
    staging._remove_derived()


def _hash_meta(meta):
    import json
    return hashlib.sha256(json.dumps(
        meta, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _identity_fingerprint(meta):
    """Fingerprint of the metadata half of the joint ETag identity."""
    return _hash_meta(meta)


def _acquire_bytes(device, staging, cfg, meta, etag, channel, lease_token,
                   restarts):
    """Download all raw bytes for one identity, committing block by block."""
    total = meta["raw_bytes"]
    block_size = min(device_block_cap(device), CLIENT_BLOCK_CAP, total)
    block_size = max(1, block_size)
    hasher = hashlib.sha256()
    offset = staging.raw_size()
    if offset:
        # Rebuild the running hash from the verified committed prefix.
        for chunk in _iter_file(staging.raw_path, offset):
            hasher.update(chunk)

    while offset < total:
        end = min(offset + block_size - 1, total - 1)
        attempt = 0
        delivered = False
        while not delivered:
            sink = _BlockSink(staging.tmp_path, hashlib.sha256())
            try:
                _, (cr_start, cr_end, cr_total), length = device.read_range(
                    offset, end, etag, lease_token=lease_token, sink=sink)
            except IdentityChanged:
                sink.close()
                raise
            except LeaseExpired:
                sink.close()
                raise
            except (ConnectionFailure, ProtocolError, OSError) as error:
                sink.close()
                attempt += 1
                staging.remove_tmp()
                if attempt > cfg.block_retries:
                    raise BudgetExceeded(
                        "block %d-%d exhausted %d retries: %s"
                        % (offset, end, cfg.block_retries, error)) from None
                continue
            sink.close()
            if cr_total != total:
                raise ProtocolError(
                    "Content-Range total %d differs from meta raw_bytes %d"
                    % (cr_total, total))
            if length != end - offset + 1:
                raise ProtocolError("block delivered %d bytes, expected %d"
                                    % (length, end - offset + 1))
            staging.append_block(staging.tmp_path, length)
            _crash_hook(CRASH_AFTER_RAW)
            staging.remove_tmp()
            # Re-hash the bytes now on disk: the on-disk block digest must
            # match the digest computed while streaming (detecting corruption),
            # then fold the same single read into the whole-payload hash.
            disk_hasher = hashlib.sha256()
            for chunk in _iter_exact(staging.raw_path, offset, length):
                disk_hasher.update(chunk)
                hasher.update(chunk)
            if disk_hasher.hexdigest() != sink.block_hexdigest():
                raise ProtocolError(
                    "committed block digest disagrees with streamed block")
            offset += length
            payload = _checkpoint_payload(
                staging, cfg.url, meta, etag, channel, lease_token,
                offset, hasher.hexdigest(), restarts, _analysis_of(cfg))
            staging.write_checkpoint(payload)
            _crash_hook(CRASH_AFTER_CHECKPOINT)
            delivered = True

    final_sha = hasher.hexdigest()
    if final_sha != meta["sha256"]:
        raise ProtocolError(
            "full-payload SHA256 mismatch: device declared %s, computed %s"
            % (meta["sha256"], final_sha))
    return final_sha


def device_block_cap(device):
    cap = getattr(device, "_range_cap", None)
    return cap or CLIENT_BLOCK_CAP


def _analysis_of(cfg):
    return getattr(cfg, "analysis", None)


def _iter_file(path, limit, start=0):
    remaining = limit - start
    with open(path, "rb") as stream:
        stream.seek(start)
        while remaining:
            chunk = stream.read(min(65536, remaining))
            if not chunk:
                raise CheckpointError("raw prefix ended before expected offset")
            yield chunk
            remaining -= len(chunk)


def _iter_exact(path, start, length):
    remaining = length
    with open(path, "rb") as stream:
        stream.seek(start)
        while remaining:
            chunk = stream.read(min(65536, remaining))
            if not chunk:
                raise CheckpointError("committed block ended prematurely")
            yield chunk
            remaining -= len(chunk)


class _BlockSink:
    """Stream one unverified block to temp and hash it without retaining it."""

    def __init__(self, path, block_hasher):
        self._stream = open(path, "wb")
        self._hasher = block_hasher
        self.closed = False

    def write(self, chunk):
        self._stream.write(chunk)
        self._hasher.update(chunk)

    def block_hexdigest(self):
        return self._hasher.hexdigest()

    def close(self):
        if not self.closed:
            self.closed = True
            self._stream.close()


def _release_safely(device, token):
    if token:
        device.release_lease(token)


def run_acquisition(cfg, staging, resume=False):
    """Run one bounded acquisition and publish the package on success."""
    device = HttpDevice(cfg.url, cfg.timeout)
    capabilities = device.get_capabilities()
    device._range_cap = capabilities["max_range_bytes"]
    channel = _choose_channel(cfg, capabilities)

    restarts = 0
    need_revalidation = False
    if resume:
        if not staging.path.is_dir() or not staging.checkpoint_path.is_file():
            raise CheckpointError(
                "no resumable staging directory found at %s" % staging.path)
        state = staging.read_checkpoint()
        _validate_resume(state, cfg, channel, capabilities)
        restarts = state["restarts_used"]
        meta, etag = state["meta"], state["etag"]
        offset = state["committed_offset"]
        staging.open_resume()
        # Verify the committed prefix before trusting any byte, then drop an
        # unconfirmed tail left by a crash between raw append and checkpoint.
        staging.verify_prefix(offset, state["prefix_sha256"])
        staging.truncate_to_committed(offset)
        lease_token = state["lease_token"]
        active_channel = state["channel"]
        need_revalidation = True
    else:
        staging.prepare_fresh()
        meta, etag, lease_token, active_channel = _open_identity(
            device, channel)
        staging.write_checkpoint(_checkpoint_payload(
            staging, cfg.url, meta, etag, active_channel, lease_token,
            0, hashlib.sha256(b"").hexdigest(), 0, _analysis_of(cfg)))

    while True:
        try:
            if need_revalidation:
                # A resumed session must first prove the remote identity is
                # still the one recorded in its checkpoint.  Failure here has
                # the same semantics as a mid-transfer identity change.
                if active_channel == "lease":
                    lease_token = _revalidate_lease(
                        device, lease_token, meta, etag)
                else:
                    _revalidate_etag(device, meta, etag)
                need_revalidation = False
            final_sha = _acquire_bytes(
                device, staging, cfg, meta, etag, active_channel,
                lease_token, restarts)
            if active_channel == "etag":
                # Final conditional re-check: same ETag must mean same meta.
                _revalidate_etag(device, meta, etag)
            else:
                # Lease read-through keeps the snapshot immutable; re-read its
                # meta once more to prove the lease is still alive.
                _revalidate_lease(device, lease_token, meta, etag)
            return _publish(device, staging, cfg, capabilities, meta, etag,
                            active_channel, lease_token, final_sha)
        except (IdentityChanged, LeaseExpired) as change:
            _release_safely(device, lease_token if active_channel == "lease"
                            else None)
            restarts += 1
            if restarts > cfg.restart_budget:
                raise BudgetExceeded(
                    "device identity changed %d times; restart budget %d "
                    "exhausted" % (restarts, cfg.restart_budget)) from change
            _reset_candidate(staging)
            meta, etag, lease_token, active_channel = _open_identity(
                device, channel)
            need_revalidation = False
            staging.write_checkpoint(_checkpoint_payload(
                staging, cfg.url, meta, etag, active_channel, lease_token,
                0, hashlib.sha256(b"").hexdigest(), restarts,
                _analysis_of(cfg)))
        except Exception:
            # Handleable failure: release a pinned slot best effort, keep the
            # identifiable staging directory for a later --resume.
            _release_safely(device, lease_token if active_channel == "lease"
                            else None)
            raise


def _choose_channel(cfg, capabilities):
    if cfg.force_channel:
        if cfg.force_channel == "lease" and not capabilities["lease"]:
            raise ProtocolError("device does not offer leases")
        if cfg.force_channel not in ("lease", "etag"):
            raise ProtocolError("unknown consistency channel")
        return cfg.force_channel
    return "lease" if capabilities["lease"] else "etag"


def _open_identity(device, channel):
    if channel == "lease":
        token = device.create_lease()
        if token is None:  # capability changed between negotiation and use
            meta, etag = device.get_meta()
            return meta, etag, None, "etag"
        meta, etag = device.get_meta(lease_token=token)
        return meta, etag, token, "lease"
    meta, etag = device.get_meta()
    return meta, etag, None, "etag"


def _revalidate_etag(device, meta, etag):
    fresh_meta, fresh_etag = device.get_meta()
    if fresh_etag != etag or fresh_meta != meta:
        raise IdentityChanged(
            "capture identity changed: ETag/meta no longer match session")
    if _identity_fingerprint(fresh_meta) != _identity_fingerprint(meta):
        raise IdentityChanged(
            "device reused an ETag for different metadata/calibration")


def _revalidate_lease(device, token, meta, etag):
    if not token:
        raise ProtocolError("checkpoint references no lease token")
    fresh_meta, fresh_etag = device.get_meta(lease_token=token)
    if fresh_etag != etag or fresh_meta != meta:
        raise IdentityChanged("leased snapshot identity changed unexpectedly")
    if _identity_fingerprint(fresh_meta) != _identity_fingerprint(meta):
        raise IdentityChanged(
            "leased snapshot ETag was reused for different metadata")
    return token


def _validate_resume(state, cfg, negotiated_channel, capabilities):
    required = {"version", "url", "meta", "etag", "channel", "lease_token",
                "committed_offset", "prefix_sha256", "restarts_used",
                "analysis"}
    if not isinstance(state, dict) or set(state) != required:
        raise CheckpointError("checkpoint fields are incomplete or unknown")
    if state["version"] != 1 or state["url"] != cfg.url:
        raise CheckpointError("checkpoint is bound to a different device URL")
    if state["channel"] not in ("lease", "etag"):
        raise CheckpointError("checkpoint has unknown consistency channel")
    if state["channel"] == "lease" and not capabilities["lease"]:
        raise CheckpointError(
            "device no longer supports the leased channel; cannot splice a "
            "new ETag capture onto the old leased prefix")
    if negotiated_channel != state["channel"] and cfg.force_channel:
        raise CheckpointError(
            "resume channel %s differs from requested %s"
            % (state["channel"], cfg.force_channel))
    saved_analysis = state["analysis"]
    wanted = _analysis_of(cfg)
    wanted_norm = ({"channel": wanted[0], "count": wanted[1]}
                   if wanted is not None else None)
    if saved_analysis != wanted_norm:
        raise CheckpointError(
            "analysis parameters differ from the original acquisition")
    if type(state["restarts_used"]) is not int or state["restarts_used"] < 0:
        raise CheckpointError("checkpoint restart counter is invalid")
    if state["restarts_used"] > cfg.restart_budget:
        raise BudgetExceeded(
            "checkpoint already exhausted the restart budget")
    offset = state["committed_offset"]
    if type(offset) is not int or offset < 0 or offset > state["meta"]["raw_bytes"]:
        raise CheckpointError("checkpoint offset is invalid")


def _publish(device, staging, cfg, capabilities, meta, etag, channel,
             lease_token, final_sha):
    try:
        result = decode_stream(
            meta, staging.raw_path, staging.samples_path,
            analysis=_analysis_of(cfg))
        if result["expected_bytes"] != meta["raw_bytes"]:
            raise ProtocolError("decoded byte length disagrees with meta")
        analysis_payload = None
        if _analysis_of(cfg) is not None:
            channel_index, count = _analysis_of(cfg)
            analysis_payload = build_analysis(
                meta, channel_index, count, result["analysis_values"])
            verify_analysis(analysis_payload)
            staging.write_analysis(analysis_payload)

        manifest = {
            "schema_version": 1,
            "source_url": cfg.url,
            "capabilities": capabilities,
            "consistency_channel": channel,
            "etag": etag,
            "capture_id": meta["capture_id"],
            "config": {
                "sample_rate": meta["sample_rate"],
                "points": meta["points"],
                "dtype": meta["dtype"],
                "byte_order": meta["byte_order"],
            },
            "channels": meta["channels"],
            "raw_bytes": meta["raw_bytes"],
            "raw_sha256": final_sha,
            "meta_sha256": _hash_meta(meta),
            "analysis": ({"channel": _analysis_of(cfg)[0],
                          "count": _analysis_of(cfg)[1],
                          "file": "analysis.json"}
                         if _analysis_of(cfg) is not None else None),
        }
        staging.write_manifest(manifest)
        staging.publish()
    finally:
        _release_safely(device, lease_token if channel == "lease" else None)
    return manifest


def build_config(url, force_channel=None, analysis=None):
    block_retries = _env_int("SIGSPECTRUM_WF_BLOCK_RETRIES",
                             DEFAULT_BLOCK_RETRIES)
    restart_budget = _env_int("SIGSPECTRUM_WF_RESTARTS",
                              DEFAULT_RESTART_BUDGET)
    timeout = float(os.environ.get("SIGSPECTRUM_WF_TIMEOUT",
                                   str(DEFAULT_TIMEOUT)))
    cfg = SessionConfig(url, block_retries, restart_budget, timeout,
                        force_channel=force_channel)
    cfg.analysis = analysis
    return cfg
