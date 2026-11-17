"""Single error type for the excursion archive: every failure exits 2."""


class ExcursionError(ValueError):
    """A validation, verification, archive-integrity or I/O failure."""
