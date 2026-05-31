"""``MRELAY_PROC_HARD_IDLE_SEC`` recycles the process even when the
bridge is stateful (= ``is_stateless()`` is False forever because a
channel has been joined).

This is the path that fixes the codex / claude leak: those clients auto
join ``#lobby`` on startup, so the original soft idle path was never
allowed to fire. The hard idle ignores the predicate and exits anyway
once the MCP host has stopped calling for the configured duration.

We drive ``_idle_loop`` directly with a fake ``_stop_event`` rather than
running it as a thread, so the AND-condition logic (``in_flight==0`` AND
``elapsed >= hard_idle_sec``) is pinned without real-time flakiness.
"""

from __future__ import annotations

import time

import pytest

from mrelay_mcp import lifecycle


class _Cfg:
    addr = "127.0.0.1:6767"
    nick = "test-nick"
    user = "test-nick"
    kind = "agent"


def _build_ctx(monkeypatch, *, idle_ttl="0", hard_idle="0", stateless=True):
    monkeypatch.setenv("MRELAY_PROC_IDLE_SEC", idle_ttl)
    monkeypatch.setenv("MRELAY_PROC_HARD_IDLE_SEC", hard_idle)
    monkeypatch.setenv("MRELAY_PARENT_WATCH_SEC", "0")
    monkeypatch.setenv("MRELAY_NATIVE_PARENT_WAIT", "0")
    monkeypatch.setenv("MRELAY_SUPERSEDE_OLDER", "0")
    monkeypatch.setenv("MRELAY_LIFECYCLE_LOG", "0")
    ctx = lifecycle.LifecycleContext(
        _Cfg(), is_stateless_provider=lambda: stateless
    )
    # Make the wait() at the top of each iteration effectively instant.
    ctx._poll_interval = 0.01
    return ctx


def _patch_os_exit(monkeypatch):
    calls = []

    def fake_exit(code):
        calls.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(lifecycle.os, "_exit", fake_exit)
    return calls


def test_hard_idle_fires_even_when_stateful(monkeypatch):
    """Channel joined, soft idle disabled — the unconditional hard
    path must still recycle the process."""
    ctx = _build_ctx(
        monkeypatch, idle_ttl="0", hard_idle="1", stateless=False
    )
    calls = _patch_os_exit(monkeypatch)
    # Pretend we have been idle for longer than hard_idle_sec.
    ctx._last_idle_at = time.monotonic() - 5.0
    with pytest.raises(SystemExit):
        ctx._idle_loop()
    assert calls == [0]


def test_hard_idle_reason_is_distinct(monkeypatch):
    """The shutdown reason for the hard path must be
    ``hard_idle_timeout`` (not ``idle_timeout``) so triage logs can tell
    the two cases apart."""
    monkeypatch.setenv("MRELAY_LIFECYCLE_LOG", "1")
    ctx = _build_ctx(
        monkeypatch, idle_ttl="0", hard_idle="1", stateless=False
    )
    # MRELAY_LIFECYCLE_LOG was set AFTER ctx construction read the env;
    # flip the flag on the context directly.
    ctx._lifecycle_log_enabled = True
    _patch_os_exit(monkeypatch)
    ctx._last_idle_at = time.monotonic() - 5.0
    import sys
    import io

    captured = io.StringIO()
    monkeypatch.setattr(sys, "stderr", captured)
    with pytest.raises(SystemExit):
        ctx._idle_loop()
    assert "reason=hard_idle_timeout" in captured.getvalue()


def test_hard_idle_respects_in_flight(monkeypatch):
    """A long-blocking ``mr_wait_for`` must not be killed by hard idle
    while the request is still in flight."""
    ctx = _build_ctx(
        monkeypatch, idle_ttl="0", hard_idle="1", stateless=False
    )

    def explode(code):
        raise AssertionError(f"unexpected exit {code}")

    monkeypatch.setattr(lifecycle.os, "_exit", explode)
    ctx.mark_request_start()
    ctx._last_idle_at = time.monotonic() - 5.0

    # Stop the loop after one iteration so the test terminates cleanly
    # without an exit firing.
    original_wait = ctx._stop_event.wait

    seen = {"n": 0}

    def stop_after_one(timeout):
        seen["n"] += 1
        if seen["n"] >= 1:
            ctx._stop_event.set()
        return original_wait(timeout)

    monkeypatch.setattr(ctx._stop_event, "wait", stop_after_one)
    ctx._idle_loop()
    assert seen["n"] >= 1


def test_soft_idle_still_gated_by_stateless(monkeypatch):
    """Hard path being added must not weaken the original gate — when
    only the soft TTL is configured, a stateful bridge must still
    survive the watchdog."""
    ctx = _build_ctx(
        monkeypatch, idle_ttl="1", hard_idle="0", stateless=False
    )

    def explode(code):
        raise AssertionError(f"unexpected exit {code}")

    monkeypatch.setattr(lifecycle.os, "_exit", explode)
    ctx._last_idle_at = time.monotonic() - 5.0

    seen = {"n": 0}
    original_wait = ctx._stop_event.wait

    def stop_after_one(timeout):
        seen["n"] += 1
        if seen["n"] >= 1:
            ctx._stop_event.set()
        return original_wait(timeout)

    monkeypatch.setattr(ctx._stop_event, "wait", stop_after_one)
    ctx._idle_loop()
    assert seen["n"] >= 1


def test_both_zero_disables_loop(monkeypatch):
    """Belt and suspenders: with both knobs at zero, the loop must
    return immediately without spinning."""
    ctx = _build_ctx(
        monkeypatch, idle_ttl="0", hard_idle="0", stateless=True
    )

    def explode(code):
        raise AssertionError(f"unexpected exit {code}")

    monkeypatch.setattr(lifecycle.os, "_exit", explode)
    # The loop must return immediately because the guard at the top
    # sees both knobs at zero.
    ctx._idle_loop()
