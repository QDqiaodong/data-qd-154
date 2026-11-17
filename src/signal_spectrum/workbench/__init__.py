"""SQLite-persisted continuous-sampling Welch workbench.

The package adds a revisable, sealable sample-state layer on top of the
bounded real-signal spectrum math.  ``engine`` holds the pure validation and
PSD math, ``store`` the SQLite commit protocol and ``cli`` the
``init/apply/status`` entry point with crash recovery.
"""
