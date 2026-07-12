# Task

The user-registry tests passed with their hand-written connection fake, but the release failed when the same code met SQLite. Rework the tests so the persistence boundary is exercised credibly, including the behaviors callers rely on.

Keep `registry.py` unchanged and use only Python's standard library.
