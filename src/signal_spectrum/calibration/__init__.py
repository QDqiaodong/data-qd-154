"""Offline reference-step calibration over published waveform packages.

``signal-calibration`` derives, from one already published
``signal-waveform-fetch`` package and one reference-step trial, the increasing
("up") and decreasing ("down") calibration curves of every channel and the
reference-value ranges a future reading may correspond to.  It reads only the
real ``manifest.json`` + verified ``raw.bin`` payload, streams the raw bytes
with the existing endianness / interleave / ``raw*scale + offset`` semantics,
and performs every mean, fit, inverse and tolerance comparison in exact
rational arithmetic -- never with an epsilon that turns a gap into contact.
"""
