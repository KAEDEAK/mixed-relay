"""Test-wide fixtures.

The autouse ``_reset_lifecycle`` fixture restores the module-global
``lifecycle._ctx`` to its import-time ``_NoOpContext`` after every test
so a test that calls ``lifecycle.startup()`` cannot leak its watchdog
threads — or its real ``_ctx`` — into a sibling test.
"""

from __future__ import annotations

import pytest

from mrelay_mcp import lifecycle


@pytest.fixture(autouse=True)
def _reset_lifecycle():
    yield
    lifecycle.reset_to_noop_for_test()
