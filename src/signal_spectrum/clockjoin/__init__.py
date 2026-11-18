"""Offline multi-instrument clock joining over published waveform packages.

``signal-clock-join`` links several already published ``signal-waveform-fetch``
packages plus cross-instrument timing anchors into one traceable, common-clock
event dataset.  It consumes only complete local packages (schema_version=1
``manifest.json`` + verified ``raw.bin``), uses exact rational arithmetic for
every timing/feasibility decision, and never treats per-relation feasibility
as whole-bundle feasibility.
"""
