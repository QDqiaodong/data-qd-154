"""Offline threshold-excursion archive over published waveform packages.

This subpackage builds a long-term, self-contained, immutable query archive
(``signal-excursions build``) from already published ``signal-waveform-fetch``
packages and answers threshold-crossing queries (``signal-excursions query``)
that locate maximal contiguous runs of out-of-limit samples.  It never
connects to a device, never reads ``samples.ndjson`` as a source of truth and
never touches the Welch workbench or the acquisition staging machinery.
"""
