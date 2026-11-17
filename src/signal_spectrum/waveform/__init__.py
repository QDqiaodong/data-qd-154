"""Traceable raw-waveform acquisition over HTTP (signal-waveform-fetch).

This subpackage is intentionally independent of the Welch workbench: it never
opens a workbench database, writes requests/generations, seals anything or
computes PSDs.  Its only job is to turn one complete raw capture on a remote
HTTP measurement device into a locally verifiable data package.
"""
