"""Single error type for ``signal-clock-join``: every failure exits 2."""


class ClockJoinError(ValueError):
    """A validation, verification, feasibility or I/O failure."""
