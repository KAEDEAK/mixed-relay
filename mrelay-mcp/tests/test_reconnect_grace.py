"""Grace-period boundary for ``_reconnect_pending``.

Auto-dropping the flag only after the grace window expires is what
prevents non-poll callers (e.g. ``mr_channels``) from pinning the
process forever. These tests sit on the boundary so a future refactor
cannot silently widen or narrow it.
"""

from __future__ import annotations

import time

from mrelay_mcp.server import BridgeConfig, BridgeSession


class _FakeClient:
    def __init__(self):
        self._joined_channels = set()
        self._status_subs = set()
        self._last_profile = None
        self._last_status = None
        self._queue = []
        self._broken = False


def _session(grace_sec: float) -> BridgeSession:
    cfg = BridgeConfig()
    cfg.reconnect_grace_sec = grace_sec
    sess = BridgeSession(cfg)
    sess._client = _FakeClient()
    return sess


def test_warn_log_emitted_once_on_drop(capsys):
    sess = _session(grace_sec=0.0001)
    sess._reconnect_pending = True
    sess._reconnect_pending_at = time.monotonic() - 5.0
    assert sess.is_stateless() is True
    assert sess.is_stateless() is True  # no pending flag left, no log
    err = capsys.readouterr().err
    assert err.count("event=reconnect_event_dropped") == 1


def test_grace_clock_overwritten_on_consecutive_reconnects():
    sess = _session(grace_sec=60.0)
    sess._reconnect_pending = True
    sess._reconnect_pending_at = time.monotonic() - 30.0
    first_anchor = sess._reconnect_pending_at
    # Simulate a second reconnect re-anchoring the clock (= what get()
    # does on every need_new transition).
    sess._reconnect_pending_at = time.monotonic()
    assert sess._reconnect_pending_at > first_anchor


def test_drop_does_not_fire_within_grace(capsys):
    sess = _session(grace_sec=60.0)
    sess._reconnect_pending = True
    sess._reconnect_pending_at = time.monotonic()
    assert sess.is_stateless() is False
    assert sess._reconnect_pending is True
    assert "event=reconnect_event_dropped" not in capsys.readouterr().err
