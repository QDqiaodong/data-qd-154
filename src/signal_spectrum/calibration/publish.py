"""Atomic publication of the calibration certificate.

The certificate is a single JSON file.  Its complete bytes are written to a
temporary file in the destination directory, fsynced, and only then renamed
over the destination.  Any failure before the rename leaves an existing
certificate with its exact prior bytes.  The destination may not be a symbolic
link, a directory, or point at any input (the STEPS file or anything inside
the package directory).
"""
import json
import os
import tempfile
from pathlib import Path

from .errors import CalibrationError


def _fail(message):
    raise CalibrationError(message)


def _fsync_dir(path):
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def serialize(certificate):
    payload = json.dumps(certificate, sort_keys=True, ensure_ascii=False,
                         allow_nan=False, indent=2) + "\n"
    return payload.encode("utf-8")


def check_destination(cert_path, input_files, package_dir):
    """Reject a destination that is a symlink, a directory, any input file,
    or any path inside the input package directory."""
    cert_path = Path(cert_path)
    if cert_path.is_symlink():
        _fail("certificate path must not be a symbolic link: %s" % cert_path)
    if cert_path.is_dir():
        _fail("certificate path must not be a directory: %s" % cert_path)
    target = cert_path.resolve()
    package_root = Path(package_dir).resolve()
    if target == package_root or package_root in target.parents:
        _fail("certificate path must not point inside the input package: %s"
              % cert_path)
    for path in input_files:
        if target == Path(path).resolve():
            _fail("certificate path must not be an input: %s" % cert_path)
    return cert_path


def publish(cert_path, payload):
    """Write ``payload`` atomically to ``cert_path``.

    The old file keeps its bytes if the temporary write or fsync fails; the
    rename is the single commit point.
    """
    cert_path = Path(cert_path)
    parent = cert_path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise CalibrationError("cannot create certificate directory: %s"
                               % error) from None
    temp_name = None
    try:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=cert_path.name + ".", suffix=".tmp", dir=str(parent))
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, cert_path)
        temp_name = None
        _fsync_dir(parent)
    except OSError as error:
        raise CalibrationError("cannot publish certificate: %s"
                               % error) from None
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
