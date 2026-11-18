"""Single error type for ``signal-resample``: every failure exits 2."""


class ResampleError(ValueError):
    """A validation, package-verification, limit or I/O failure."""
