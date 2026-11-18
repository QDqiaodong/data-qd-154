"""Offline multi-channel waveform resampling onto a common time grid.

``signal-resample`` reads one already published, fully verified
``signal-waveform-fetch`` package (schema_version=1 ``manifest.json`` plus a
verified ``raw.bin``), and produces a per-channel derived dataset on a new
sample-time grid ``t = m / target_rate``.  Every output point is computed from
a fixed radius-16, Blackman-windowed sinc low-pass kernel evaluated at the
*exact* rational source coordinate ``m * source_rate / target_rate``; no
floating-point phase accumulation is ever used.

It never touches ``samples.ndjson``, never contacts a device and never
modifies the source package.  The source workload is streamed: only a bounded
neighbourhood of ``2R+1`` points per channel and a fixed-size output block are
resident, so memory does not scale with the payload or the channel count.
"""
