"""``BridgeSession.is_stateless()`` is the gate the idle watchdog uses to
decide whether to recycle a process. We pin every stateful signal —
including the session-level ``_reconnect_pending`` with its grace period
— so a future refactor cannot silently kill resident sessions.

These tests construct ``BridgeSession`` instances directly and poke the
internal state. We never open a real socket because we want
deterministic AND/OR coverage of the predicate, not a wire test.
"""

from __future__ import annotations

import time
from collections import deque

from mrelay_mcp.server import BridgeConfig, BridgeSession


class _FakeClient:
    """Stand-in for ``MixedRelayClient`` that exposes only the attributes
    ``is_stateless()`` looks at, so we can drive the predicate without
    touching the wire."""

    def __init__(
        self,
        joined=None,
        subs=None,
        last_profile=None,
        last_status=None,
        queue=None,
    ):
        self._joined_channels = set(joined or [])
        self._status_subs = set(subs or [])
        self._last_profile = last_profile
        self._last_status = last_status
        self._queue = deque(queue or [])
        self._broken = False


def _session(grace_sec: float = 60.0) -> BridgeSession:
    cfg = BridgeConfig()
    cfg.reconnect_grace_sec = grace_sec
    return BridgeSession(cfg)


# ----- live client stateful signals -----


def test_joined_channels_makes_stateful():
    sess = _session()
    sess._client = _FakeClient(joined=["#lobby"])
    assert sess.is_stateless() is False


def test_status_subs_makes_stateful():
    sess = _session()
    sess._client = _FakeClient(subs=["kaede"])
    assert sess.is_stateless() is False


def test_last_profile_makes_stateful():
    sess = _session()
    sess._client = _FakeClient(last_profile={"node": {"name": "x"}})
    assert sess.is_stateless() is False


def test_last_status_makes_stateful():
    sess = _session()
    sess._client = _FakeClient(last_status={"intent": "running"})
    assert sess.is_stateless() is False


def test_unread_queue_makes_stateful():
    sess = _session()
    sess._client = _FakeClient(queue=["frame1"])
    assert sess.is_stateless() is False


def test_all_empty_is_stateless():
    sess = _session()
    sess._client = _FakeClient()
    assert sess.is_stateless() is True


# ----- session snapshot fallback (= _client is None during transient
# reconnect; the snapshot is what get() will replay) -----


def test_client_none_and_empty_snapshot_is_stateless():
    sess = _session()
    sess._client = None
    assert sess.is_stateless() is True


def test_client_none_with_pinned_snapshot_is_stateful():
    sess = _session()
    sess._client = None
    sess._joined_channels = {"#lobby"}
    assert sess.is_stateless() is False


# ----- _reconnect_pending grace period -----


def test_reconnect_pending_within_grace_is_stateful():
    sess = _session(grace_sec=60.0)
    sess._client = _FakeClient()
    sess._reconnect_pending = True
    sess._reconnect_pending_at = time.monotonic()
    assert sess.is_stateless() is False


def test_reconnect_pending_beyond_grace_drops_when_otherwise_stateless(capsys):
    sess = _session(grace_sec=0.0001)
    sess._client = _FakeClient()
    sess._reconnect_pending = True
    sess._reconnect_pending_at = time.monotonic() - 5.0
    assert sess.is_stateless() is True
    assert sess._reconnect_pending is False, "drop must clear the flag"
    err = capsys.readouterr().err
    assert "event=reconnect_event_dropped" in err
    assert "reason=grace_expired_stateless" in err


def test_reconnect_pending_beyond_grace_preserved_when_joined(capsys):
    """plan-8 §0 reflection #1: stateful resident must not lose MRRECONNECT
    visibility even after grace, because their later mr_poll still needs
    to see the event."""
    sess = _session(grace_sec=0.0001)
    sess._client = _FakeClient(joined=["#lobby"])
    sess._reconnect_pending = True
    sess._reconnect_pending_at = time.monotonic() - 5.0
    assert sess.is_stateless() is False
    assert sess._reconnect_pending is True, "stateful resident must keep flag"
    err = capsys.readouterr().err
    assert "event=reconnect_event_dropped" not in err


def test_reconnect_pending_beyond_grace_preserved_when_subscribed(capsys):
    sess = _session(grace_sec=0.0001)
    sess._client = _FakeClient(subs=["kaede"])
    sess._reconnect_pending = True
    sess._reconnect_pending_at = time.monotonic() - 5.0
    assert sess.is_stateless() is False
    assert sess._reconnect_pending is True
    assert "event=reconnect_event_dropped" not in capsys.readouterr().err


def test_reconnect_pending_beyond_grace_preserved_when_queue_non_empty(capsys):
    """plan-8 review-1 残留メモ: queue も other stateful signal の一員。"""
    sess = _session(grace_sec=0.0001)
    sess._client = _FakeClient(queue=["frame1"])
    sess._reconnect_pending = True
    sess._reconnect_pending_at = time.monotonic() - 5.0
    assert sess.is_stateless() is False
    assert sess._reconnect_pending is True
    assert "event=reconnect_event_dropped" not in capsys.readouterr().err


def test_reconnect_pending_with_snapshot_stateful_preserved(capsys):
    """``c is None`` + pinned snapshot must also preserve the flag."""
    sess = _session(grace_sec=0.0001)
    sess._client = None
    sess._joined_channels = {"#lobby"}
    sess._reconnect_pending = True
    sess._reconnect_pending_at = time.monotonic() - 5.0
    assert sess.is_stateless() is False
    assert sess._reconnect_pending is True
    assert "event=reconnect_event_dropped" not in capsys.readouterr().err
