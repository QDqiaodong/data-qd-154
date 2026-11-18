"""Single error type for ``signal-calibration``: every failure exits 2."""


class CalibrationError(ValueError):
    """A validation, package-verification or I/O failure."""
