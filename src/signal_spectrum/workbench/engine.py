"""Pure validation and Welch PSD math for the sampling workbench.

Nothing in this module touches the filesystem or SQLite; all state is passed
in as plain values so the rules are easy to test on their own.

Sample state
------------
Samples are addressed by absolute, non-negative integer indexes.  The state
layer maps each *written* index to either a finite number or to a tombstone
(``None``): a write of ``null`` is an accepted deletion, including deletion of
an index that was never written.  ``horizon`` is the high-water mark of
``max(accepted write index) + 1`` and ``seal`` and never retreats.

Spectrum layer
--------------
Windows start at ``s = m * H`` (``m >= 0``) and span ``N`` points.  Only whole
windows with ``s + N <= horizon`` are reported.  A window is ``complete`` when
every point currently holds a finite value, otherwise it is a ``gap``; it is
``sealed`` when ``s + N <= seal``.  Gap windows are never zero-filled and never
enter a PSD average.
"""
import cmath
import math

MAX_INDEX = 100_000
MAX_BATCH_WRITES = 20_000
WINDOWS = ("rectangular", "hann")


# --------------------------------------------------------------------------
# Strict validation (booleans are never accepted as integers)
# --------------------------------------------------------------------------

def _is_int(value):
    return type(value) is int


def _is_finite_number(value):
    return type(value) in (int, float) and math.isfinite(value) and abs(value) <= 10**9


def validate_config(config):
    """Validate an init config object and return a normalized dict.

    Exactly the keys sample_rate, frame_size, hop, window, remove_mean are
    allowed.  frame_size is an even integer in 4..128, hop in 1..frame_size,
    sample_rate an integer in 1..10**9, window one of the legacy enums and
    remove_mean a strict boolean.
    """
    if not isinstance(config, dict) or set(config) != {
        "sample_rate", "frame_size", "hop", "window", "remove_mean",
    }:
        raise ValueError("config must contain exactly sample_rate, frame_size, hop, window, remove_mean")
    sample_rate = config["sample_rate"]
    frame_size = config["frame_size"]
    hop = config["hop"]
    window = config["window"]
    remove_mean = config["remove_mean"]
    if not _is_int(sample_rate) or not 1 <= sample_rate <= 10**9:
        raise ValueError("sample_rate must be an integer in 1..10^9")
    if not _is_int(frame_size) or not 4 <= frame_size <= 128 or frame_size % 2 != 0:
        raise ValueError("frame_size must be an even integer in 4..128")
    if not _is_int(hop) or not 1 <= hop <= frame_size:
        raise ValueError("hop must be an integer in 1..frame_size")
    if window not in WINDOWS:
        raise ValueError("window must be rectangular or hann")
    if type(remove_mean) is not bool:
        raise ValueError("remove_mean must be a boolean")
    return {
        "sample_rate": sample_rate,
        "frame_size": frame_size,
        "hop": hop,
        "window": window,
        "remove_mean": remove_mean,
    }


def validate_request(request, *, old_seal):
    """Validate an apply request against the current seal.

    Returns ``(writes, seal)`` where writes is a list of ``(index, value)``
    pairs (value is float or ``None``).  Validation is all-or-nothing: a
    rejected request changes nothing.
    """
    if not isinstance(request, dict) or set(request) != {"request_id", "writes", "seal"}:
        raise ValueError("request must contain exactly request_id, writes, seal")
    request_id = request["request_id"]
    if not isinstance(request_id, str) or not request_id or request_id != request_id.strip() \
            or len(request_id) > 128 or any(ord(c) < 32 for c in request_id):
        raise ValueError("request_id must be a nonblank identifier of <=128 characters")
    seal = request["seal"]
    if not _is_int(seal) or not 0 <= seal <= MAX_INDEX:
        raise ValueError("seal must be an integer in 0..100000")
    if seal < old_seal:
        raise ValueError("seal must be monotonic")
    writes = request["writes"]
    if not isinstance(writes, list) or len(writes) > MAX_BATCH_WRITES:
        raise ValueError("writes must be an array of at most 20000 entries")
    seen = set()
    normalized = []
    for entry in writes:
        if not isinstance(entry, dict) or set(entry) != {"index", "value"}:
            raise ValueError("each write must contain exactly index and value")
        index = entry["index"]
        value = entry["value"]
        if not _is_int(index) or not 0 <= index <= MAX_INDEX:
            raise ValueError("write index must be an integer in 0..100000")
        if index in seen:
            raise ValueError("duplicate write index in batch: %d" % index)
        seen.add(index)
        if index < old_seal:
            raise ValueError("write index %d is below the sealed frontier %d" % (index, old_seal))
        if value is None:
            normalized.append((index, None))
        elif _is_finite_number(value):
            normalized.append((index, float(value)))
        else:
            raise ValueError("write value must be a finite number in [-10^9, 10^9] or null")
    return request_id, normalized, seal


# --------------------------------------------------------------------------
# Window classification and Welch PSD
# --------------------------------------------------------------------------

def window_weights(n, window):
    """Legacy symmetric window: all-ones rectangular or symmetric Hann."""
    if window == "rectangular":
        return [1.0] * n
    return [0.5 - 0.5 * math.cos(2.0 * math.pi * i / (n - 1)) for i in range(n)]


def window_starts(frame_size, hop, horizon):
    """Ordered starts ``s = m*H`` of every whole window under the horizon."""
    if horizon < frame_size:
        return []
    return list(range(0, horizon - frame_size + 1, hop))


def classify_windows(samples, tombstone, frame_size, hop, horizon, seal):
    """Classify every reportable window.

    Returns a list of dicts ordered by start with start, end, complete and
    sealed flags.  ``samples`` maps index to float, ``tombstone`` holds the
    deleted indexes.
    """
    windows = []
    for start in window_starts(frame_size, hop, horizon):
        end = start + frame_size
        complete = all(
            start + offset not in tombstone and start + offset in samples
            for offset in range(frame_size)
        )
        windows.append({
            "start": start,
            "end": end,
            "complete": complete,
            "sealed": end <= seal,
        })
    return windows


def _twiddle_table(n):
    return [
        (math.cos(-2.0 * math.pi * k / n), math.sin(-2.0 * math.pi * k / n))
        for k in range(n // 2 + 1)
    ]


def window_psd(values, config, weights, sum_w2, twiddles):
    """One-sided PSD bins for a single complete window.

    The window mean is removed when configured, then the symmetric window is
    applied.  PSD[k] = multiplicity * |DFT[k]|^2 / (sample_rate * sum(w^2)),
    with multiplicity 1 for DC and the even-N Nyquist bin and 2 otherwise.
    Gap windows never reach this function and nothing is zero-filled.
    """
    n = config["frame_size"]
    if config["remove_mean"]:
        mean = math.fsum(values) / n
        centered = [x - mean for x in values]
    else:
        centered = list(values)
    windowed = [x * w for x, w in zip(centered, weights)]
    rate = config["sample_rate"]
    bins = []
    for k in range(n // 2 + 1):
        cos_k, sin_k = twiddles[k]
        # exp(-2*pi*i*k*i/n) recomputed per sample via angle accumulation.
        re = 0.0
        im = 0.0
        angle_cos = 1.0
        angle_sin = 0.0
        for v in windowed:
            re += v * angle_cos
            im += v * angle_sin
            angle_cos, angle_sin = (
                angle_cos * cos_k - angle_sin * sin_k,
                angle_cos * sin_k + angle_sin * cos_k,
            )
        magnitude_sq = re * re + im * im
        multiplicity = 1 if k == 0 or k == n // 2 else 2
        bins.append({
            "index": k,
            "frequency_hz": k * rate / n,
            "psd": multiplicity * magnitude_sq / (rate * sum_w2),
        })
    return bins


def build_report(*, generation, request_id, config, samples, tombstone, horizon, seal):
    """Rebuild the full deterministic report for one committed generation."""
    n = config["frame_size"]
    hop = config["hop"]
    weights = window_weights(n, config["window"])
    sum_w2 = math.fsum(w * w for w in weights)
    twiddles = _twiddle_table(n)

    windows = classify_windows(samples, tombstone, n, hop, horizon, seal)
    live_sums = [0.0] * (n // 2 + 1)
    sealed_sums = [0.0] * (n // 2 + 1)
    live_count = 0
    sealed_complete_count = 0
    sealed_gap_count = 0

    window_entries = []
    for entry in windows:
        start = entry["start"]
        if entry["sealed"] and not entry["complete"]:
            sealed_gap_count += 1
        if entry["complete"]:
            values = [samples[start + offset] for offset in range(n)]
            bins = window_psd(values, config, weights, sum_w2, twiddles)
            for k, bin_entry in enumerate(bins):
                live_sums[k] += bin_entry["psd"]
                if entry["sealed"]:
                    sealed_sums[k] += bin_entry["psd"]
            live_count += 1
            if entry["sealed"]:
                sealed_complete_count += 1
            window_entries.append({
                "start": start,
                "end": entry["end"],
                "status": "complete",
                "sealed": entry["sealed"],
            })
        else:
            window_entries.append({
                "start": start,
                "end": entry["end"],
                "status": "gap",
                "sealed": entry["sealed"],
            })

    def average(sums, count):
        if count == 0:
            return None
        rate = config["sample_rate"]
        return [
            {
                "index": k,
                "frequency_hz": k * rate / n,
                "psd": sums[k] / count,
            }
            for k in range(n // 2 + 1)
        ]

    return {
        "schema_version": 1,
        "generation": generation,
        "request_id": request_id,
        "config": config,
        "seal": seal,
        "horizon": horizon,
        "windows": window_entries,
        "live_complete_count": live_count,
        "sealed_complete_count": sealed_complete_count,
        "sealed_gap_count": sealed_gap_count,
        "live_psd": average(live_sums, live_count),
        "sealed_psd": average(sealed_sums, sealed_complete_count),
    }
