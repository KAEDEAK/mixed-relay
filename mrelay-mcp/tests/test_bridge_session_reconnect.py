"""feedback-3 must regression: BridgeSession.get() must carry caller
intent (joined channels, status subs, profile, last status) across the
*new* MixedRelayClient instance it builds on idle / broken reconnect.

This is the test codex asked for — the previous test only exercised
MixedRelayClient.connect() on the same instance, which never tripped the
real bug because BridgeSession spawns a fresh client each reconnect.

Requires a running v003 mrelayd on 127.0.0.1:6767. Run with:
    cd mrelay-mcp && python -m pytest tests/test_bridge_session_reconnect.py -v
"""
from __future__ import annotations

import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..")))

from mrelay_mcp.client import RemoteError  # noqa: E402
from mrelay_mcp.server import BridgeConfig, BridgeSession  # noqa: E402


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


def _session(nick: str, idle: int = 1) -> BridgeSession:
    cfg = BridgeConfig()
    cfg.addr = ADDR
    cfg.nick = nick
    cfg.user = nick
    cfg.kind = "agent"
    cfg.idle_timeout_sec = idle
    return BridgeSession(cfg)


def test_bridgesession_idle_reconnect_preserves_membership():
    if not _server_up():
        import pytest

        pytest.skip(f"no mrelayd at {ADDR}")
    nick = _unique("bs-probe")
    ch = "#bs-probe-" + nick
    sess = _session(nick, idle=1)
    try:
        c1 = sess.get()
        c1.join(ch)
        # Capture identity of first client to confirm reconnect actually
        # spun up a new one.
        first_id = id(c1)

        # Wait past the idle timeout so the next get() must rebuild.
        time.sleep(1.5)

        c2 = sess.get()
        assert id(c2) != first_id, "BridgeSession should have spun up a new client after idle"

        # The whole point: the new client must already think it is in #ch,
        # and a say() must succeed (sync ack, no 404).
        assert ch in c2._joined_channels, "joined channels not carried across reconnect"
        c2.say(ch, "hello after bridge reconnect")  # must NOT raise
    finally:
        try:
            if sess._client is not None:
                sess._client.close()
        except Exception:
            pass


def test_bridgesession_reconnect_preserves_status_and_subs():
    if not _server_up():
        import pytest

        pytest.skip(f"no mrelayd at {ADDR}")
    nick = _unique("bs-probe")
    sess = _session(nick, idle=1)
    try:
        c1 = sess.get()
        c1.set_profile({"node": {"name": nick, "kind": "agent", "version": "test"}})
        c1.set_status({"intent": "running test"})
        c1.subscribe_status("*")

        time.sleep(1.5)

        c2 = sess.get()
        assert c2 is not c1
        assert c2._last_profile == {"node": {"name": nick, "kind": "agent", "version": "test"}}
        assert c2._last_status == {"intent": "running test"}
        assert "*" in c2._status_subs
    finally:
        try:
            if sess._client is not None:
                sess._client.close()
        except Exception:
            pass
