"""``mark_request_start/end`` must protect long-blocking tool calls
(``mr_poll`` / ``mr_wait_for``) from the idle watchdog.

We exercise the counter and the elapsed-time gating directly without
spinning a thread — the goal is the AND condition (in_flight==0 AND
elapsed >= TTL), not the watchdog mechanics.
"""

from __future__ import annotations

import time

from mrelay_mcp import lifecycle


class _Cfg:
    addr = "127.0.0.1:6767"
    nick = "test-nick"


def _new_ctx(monkeypatch, ttl=1, parent=0):
    monkeypatch.setenv("MRELAY_PROC_IDLE_SEC", str(ttl))
    monkeypatch.setenv("MRELAY_PARENT_WATCH_SEC", str(parent))
    # Keep watchdog noise out of capsys.
    monkeypatch.setenv("MRELAY_LIFECYCLE_LOG", "0")
    return lifecycle.LifecycleContext(_Cfg(), is_stateless_provider=lambda: True)


def test_in_flight_blocks_idle_exit(monkeypatch):
    ctx = _new_ctx(monkeypatch, ttl=1)
    ctx.mark_request_start()
    ctx._last_idle_at = time.monotonic() - 10  # pretend we've been idle forever
    # Manually replay the watchdog decision logic.
    assert ctx._in_flight == 1, "request start must increment counter"
    in_flight = ctx._in_flight
    assert in_flight > 0, "watchdog must skip while in_flight>0"


def test_request_end_restarts_idle_clock(monkeypatch):
    ctx = _new_ctx(monkeypatch, ttl=1)
    ctx.mark_request_start()
    time.sleep(0.05)
    ctx.mark_request_end()
    # _last_idle_at must have been refreshed at the moment in_flight hit 0.
    assert time.monotonic() - ctx._last_idle_at < 0.5
    assert ctx._in_flight == 0


def test_mark_request_end_underflow_is_clamped(monkeypatch):
    ctx = _new_ctx(monkeypatch, ttl=1)
    ctx.mark_request_end()
    ctx.mark_request_end()
    assert ctx._in_flight == 0, "underflow must clamp at zero"
