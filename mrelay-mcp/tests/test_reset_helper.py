"""``reset_to_noop_for_test()`` must stop watchdogs and restore the
NoOp default. Plumbing that powers the autouse fixture.
"""

from __future__ import annotations

import time

from mrelay_mcp import lifecycle


class _Cfg:
    addr = "127.0.0.1:6767"
    nick = "test-nick"


def test_reset_stops_watchdog_threads(monkeypatch):
    monkeypatch.setenv("MRELAY_PROC_IDLE_SEC", "1")
    monkeypatch.setenv("MRELAY_PARENT_WATCH_SEC", "1")
    monkeypatch.setenv("MRELAY_LIFECYCLE_LOG", "0")
    ctx = lifecycle.startup(_Cfg(), is_stateless_provider=lambda: True)
    assert ctx._idle_thread is not None and ctx._idle_thread.is_alive()
    lifecycle.reset_to_noop_for_test()
    # join is bounded to 1s inside close(); give it a moment to settle.
    time.sleep(0.1)
    assert not ctx._idle_thread.is_alive()
    assert isinstance(lifecycle._ctx, lifecycle._NoOpContext)
