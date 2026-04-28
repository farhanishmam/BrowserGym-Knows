"""Helper scripts shipped with the KNOWS BrowserGym benchmark.

Modules in this package automate setup and authentication tasks that the
benchmark scripts orchestrate (notably the per-worker Google sign-in flow).
The package is kept importable from Ray worker subprocesses by pinning the
repo root onto ``PYTHONPATH`` in :mod:`benchmarks._common` and
:mod:`benchmark`.
"""
