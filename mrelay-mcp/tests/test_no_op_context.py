"""Without ``startup()``, the module-level helpers must be safe no-ops.

The existing ``tests/test_validation_and_errors.py`` calls ``server._run()``
directly without ever booting the bridge, so ``lifecycle.mark_request_*``
inside ``_run()`` must not raise. This file pins that contract.
"""

from __future__ import annotations

from mrelay_mcp import lifecycle


def test_mark_request_start_is_safe_without_startup():
    lifecycle.mark_request_start()  # must not raise


def test_mark_request_end_is_safe_without_startup():
    lifecycle.mark_request_end()  # must not raise


def test_shutdown_returns_false_without_startup():
    assert lifecycle.shutdown("normal") is False


def test_reset_to_noop_is_idempotent():
    lifecycle.reset_to_noop_for_test()
    lifecycle.reset_to_noop_for_test()  # must not raise on the second call
