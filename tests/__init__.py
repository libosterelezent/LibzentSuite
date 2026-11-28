"""Test package.

Historically this module isolated the process-wide self-command hub
between tests (a snapshot/restore of its late-bound seams wrapped
every ``TestCase.run``); the hub retired 2026-11-28 (see
dump/README.md) and with it the cross-test seam leakage it could
cause — nothing needs isolating now. Kept as the package marker so
test modules import as ``tests.unit.test_*``.
"""
