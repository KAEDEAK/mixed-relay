"""Regression test for feedback-3: bridge idle reconnect must preserve
channel membership + status subscriptions, and say() must surface ERROR
404 synchronously rather than silently succeeding when we're not in the
channel anymore.

Requires a running v003 mrelayd on 127.0.0.1:6767. Run with:
    cd mrelay-mcp && python -m pytest tests/test_reconnect_rejoin.py -v
"""
from __future__ import annotations

import os
import socket
import sys
import time

# Make the in-tree module importable when run without install.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..")))

from mrelay_mcp.client import MixedRelayClient, RemoteError  # noqa: E402


ADDR = os.environ.get("MRELAY_ADDR", "127.0.0.1:6767")


def _server_up() -> bool:
    host, _, port = ADDR.partition(":")
    try:
        with socket.create_connection((host or "127.0.0.1", int(port or "6767")), timeout=0.5):
            return True
    except OSError:
        return False


def _unique(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000) % 1_000_000}"


def test_reconnect_restores_membership_and_say_works():
    if not _server_up():
        import pytest

        pytest.skip(f"no mrelayd at {ADDR}")
    nick = _unique("probe")
    ch = "#probe-rejoin-" + nick
    a = MixedRelayClient(addr=ADDR, nick=nick, user=nick, kind="agent")
    a.connect()
    try:
        a.join(ch)
        assert ch in a._joined_channels

        # Simulate an idle drop: kill the socket underneath the client.
        # The read loop will notice EOF and flip _broken=True. Calling
        # connect() again should replay membership transparently.
        try:
            a._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        a._sock.close()
        # Let the read loop observe EOF.
        time.sleep(0.1)
        assert a._broken is True

        a.connect()
        # After reconnect, _joined_channels must still contain ch and the
        # server must have processed our replayed JOIN, so a say() should
        # succeed (synchronous ack) rather than 404.
        assert ch in a._joined_channels
        a.say(ch, "hello after reconnect")  # must not raise
    finally:
        try:
            a.close()
        except Exception:
            pass


def test_say_raises_404_when_not_in_channel():
    """Negative test: say() to a channel we never joined must surface the
    ERROR 404 as a RemoteError, not silently succeed. This is what the old
    send-and-forget say() was hiding."""
    if not _server_up():
        import pytest

        pytest.skip(f"no mrelayd at {ADDR}")
    nick = _unique("probe")
    a = MixedRelayClient(addr=ADDR, nick=nick, user=nick, kind="agent")
    a.connect()
    try:
        raised = False
        try:
            a.say("#totally-not-joined-" + nick, "should fail")
        except RemoteError as e:
            if e.code == 404:
                raised = True
        assert raised, "say() to an unjoined channel must raise RemoteError 404"
    finally:
        try:
            a.close()
        except Exception:
            pass
