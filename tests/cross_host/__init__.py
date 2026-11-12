"""The opt-in cross-host smoke (plan Step 3): ``run_smoke.py`` and ``check_store.py``.

Neither file matches pytest's ``test_*.py`` pattern, so the default suite never runs a
real host. ``tests/test_cross_host_checker.py`` covers both without models or the real
store. This file makes the folder one package, so mypy and pytest name each module the
same way (``cross_host.<name>``).
"""
