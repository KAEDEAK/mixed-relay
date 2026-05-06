"""Tests for the LLM-friendly error handling improvements.

Covers the dev note 2024-05-05-connection_stability_improvements.md:
- §8: invalid channel name (missing '#') must surface immediately, not as
  a 5-second timeout.
- §8.7: error categories must be encoded as a leading prefix so a small-
  parameter LLM can route the response (validation/timeout/transport/server).
- broken state from duplicate NICK on connect must self-heal via
  auto-suffixing.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from mrelay_mcp import server as bridge_module
from mrelay_mcp.client import (
    BrokenConnection,
    MixedRelayClient,
    RemoteError,
    TimeoutWaiting,
    ValidationError,
)
from mrelay_mcp.server import (
    BridgeConfig,
    BridgeSession,
    _do_history,
    _do_join,
    _do_part,
    _do_say,
    _do_set_topic,
    _run,
    _validate_channel,
    _validate_user_text,
)


# ----- _validate_channel (pure unit) -----


def test_validate_channel_rejects_missing_hash():
    with pytest.raises(ValidationError) as ei:
        _validate_channel("lobby")
    msg = str(ei.value)
    assert "#" in msg
    assert "'#lobby'" in msg, "should suggest the corrected name"


def test_validate_channel_rejects_empty():
    with pytest.raises(ValidationError):
        _validate_channel("")


def test_validate_channel_accepts_hashed():
    _validate_channel("#lobby")  # no raise


def test_validate_channel_rejects_spaces():
    with pytest.raises(ValidationError):
        _validate_channel("#has space")


# ----- _run prefix categorisation -----


def test_run_validation_prefix():
    out = _run(lambda: (_ for _ in ()).throw(ValidationError("bad arg")))
    assert out["ok"] is False
    assert out["error"]["message"].startswith("validation: ")
    assert out["error"]["code"] == 400


def test_run_remote_error_uses_server_prefix():
    out = _run(lambda: (_ for _ in ()).throw(RemoteError(404, "no such channel")))
    assert out["ok"] is False
    assert out["error"]["message"] == "server: no such channel"
    assert out["error"]["code"] == 404


def test_run_timeout_prefix():
    out = _run(lambda: (_ for _ in ()).throw(TimeoutWaiting("MRHISTORY reply not received")))
    assert out["ok"] is False
    assert out["error"]["message"].startswith("timeout: ")
    assert out["error"]["code"] == 504


def test_run_transport_prefix():
    out = _run(lambda: (_ for _ in ()).throw(BrokenConnection("greet timeout")))
    assert out["ok"] is False
    assert out["error"]["message"].startswith("transport: ")
    assert out["error"]["code"] == 0


def test_run_internal_prefix_for_unexpected():
    out = _run(lambda: (_ for _ in ()).throw(KeyError("missing")))
    assert out["ok"] is False
    assert out["error"]["message"].startswith("internal: ")


# ----- bridge tools refuse hash-less channel without touching the wire -----


class _ExplodingClient:
    """If the bridge tries to actually use the wire, blow up loudly."""

    nick = "test"
    user = "test"
    _broken = False
    _joined_channels: set = set()
    _status_subs: set = set()
    _last_profile = None
    _last_status = None

    def __getattr__(self, item):
        raise AssertionError(
            f"validation should have aborted before touching client.{item}"
        )


def _patch_session(monkeypatch):
    """Make BridgeSession.get() return a sentinel that explodes on use."""
    monkeypatch.setattr(bridge_module.session, "get", lambda: _ExplodingClient())


def test_do_history_rejects_hashless_channel_without_wire(monkeypatch):
    _patch_session(monkeypatch)
    out = _run(lambda: _do_history("lobby", "after", 0, 100))
    assert out["ok"] is False
    assert out["error"]["message"].startswith("validation: ")
    assert "#lobby" in out["error"]["message"]


def test_do_join_rejects_hashless_channel_without_wire(monkeypatch):
    _patch_session(monkeypatch)
    out = _run(lambda: _do_join("lobby"))
    assert out["ok"] is False
    assert out["error"]["message"].startswith("validation: ")


def test_do_say_rejects_hashless_channel_without_wire(monkeypatch):
    _patch_session(monkeypatch)
    out = _run(lambda: _do_say("lobby", "hi"))
    assert out["ok"] is False
    assert out["error"]["message"].startswith("validation: ")


def test_do_history_rejects_bad_direction(monkeypatch):
    _patch_session(monkeypatch)
    out = _run(lambda: _do_history("#lobby", "sideways", 0, 10))
    assert out["ok"] is False
    assert out["error"]["message"].startswith("validation: ")
    assert "direction" in out["error"]["message"]


def test_do_history_rejects_oversized_limit(monkeypatch):
    _patch_session(monkeypatch)
    out = _run(lambda: _do_history("#lobby", "after", 0, 9999))
    assert out["ok"] is False
    assert out["error"]["message"].startswith("validation: ")


# ----- duplicate NICK auto-suffix on connect -----


def _start_real_server(tmp_path):
    """Spawn the Go server in a goroutine? No — just use a tiny mock TCP.

    Actually we start the real go binary out-of-process is overkill for a
    Python test. We mock the *server side* of the TCP socket: respond with
    ERROR 433 the first time, MRWELCOME on the retry.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    captured = {"nicks_seen": []}

    def serve_one():
        conn, _ = listener.accept()
        f = conn.makefile("rwb", buffering=0)
        try:
            for attempt in range(20):
                line = f.readline()
                if not line:
                    return
                line = line.decode().rstrip("\r\n")
                # Expect: NICK <nick>, USER ..., MRKIND ...
                if line.startswith("NICK "):
                    nick = line.split(" ", 1)[1]
                    captured["nicks_seen"].append(nick)
                    # consume USER + MRKIND
                    f.readline()
                    f.readline()
                    if len(captured["nicks_seen"]) == 1:
                        # First attempt: reject as in-use.
                        f.write(b"ERROR 433 :nick in use\r\n")
                    else:
                        # Subsequent: accept.
                        f.write(b":server MRWELCOME :welcome\r\n")
                        # keep socket open — the test will close it
                        time.sleep(2)
                        return
        finally:
            conn.close()
            listener.close()

    t = threading.Thread(target=serve_one, daemon=True)
    t.start()
    return port, captured


def test_connect_auto_suffixes_on_nick_collision(tmp_path):
    port, captured = _start_real_server(tmp_path)
    c = MixedRelayClient(addr=f"127.0.0.1:{port}", nick="claude_miko", user="claude_miko", kind="agent")
    c.connect()
    assert c.nick == "claude_miko_2", f"expected auto-suffix, got {c.nick}"
    assert c.user == "claude_miko", "USER (cursor key) must NOT be suffixed"
    assert captured["nicks_seen"] == ["claude_miko", "claude_miko_2"]
    c.close()


# ----- _validate_user_text — embedded newline / NUL -----

# This guards against a bug we hit live: a multi-paragraph PRIVMSG sent via
# mr_say went out as one wire write but the embedded '\n' between paragraphs
# made the server parse the second paragraph as a fresh command, producing
# ERROR 421 :unknown command: (2). The fix is to refuse line terminators in
# any user-supplied trailing/param BEFORE the wire send.


def test_validate_user_text_rejects_newline():
    with pytest.raises(ValidationError) as ei:
        _validate_user_text("first line\nsecond line")
    assert "newline" in str(ei.value)


def test_validate_user_text_rejects_carriage_return():
    with pytest.raises(ValidationError):
        _validate_user_text("oops\rback")


def test_validate_user_text_rejects_nul():
    with pytest.raises(ValidationError):
        _validate_user_text("text with \x00 nul")


def test_validate_user_text_accepts_empty_string():
    _validate_user_text("")  # PART without a reason / TOPIC clear


def test_validate_user_text_accepts_unicode_and_punctuation():
    _validate_user_text("こんにちは！ 全角スペースや (1) も OK。")


def test_do_say_rejects_multiline_without_wire():
    out = _run(lambda: _do_say("#lobby", "para1\npara2"))
    assert out["ok"] is False
    assert out["error"]["message"].startswith("validation: ")
    assert "newline" in out["error"]["message"]


def test_do_set_topic_rejects_multiline_without_wire():
    out = _run(lambda: _do_set_topic("#lobby", "title\nbody"))
    assert out["ok"] is False
    assert out["error"]["message"].startswith("validation: ")


def test_do_part_rejects_multiline_reason_without_wire():
    out = _run(lambda: _do_part("#lobby", "reason\nleak"))
    assert out["ok"] is False
    assert out["error"]["message"].startswith("validation: ")


# ----- MRPURGE broadcast → typed poll event (F-13.6) -----

# These run against the pure `_msg_to_event` mapper so they don't need a TCP
# server; they pin the event shape so an LLM polling via `mr_poll` sees a
# semantic 'purge' event when a peer resets the channel, instead of a generic
# 'raw' frame it has to interpret itself.


def test_msg_to_event_classifies_purge_broadcast():
    from mrelay_mcp.client import _msg_to_event, parse_line
    m = parse_line(":alice!alice@host MRPURGE #lobby")
    ev = _msg_to_event(m, my_nick="bob")
    assert ev["kind"] == "purge"
    assert ev["from"] == "alice"
    assert ev["channel"] == "#lobby"
    assert "done" not in ev


def test_msg_to_event_classifies_purge_done_with_count():
    from mrelay_mcp.client import _msg_to_event, parse_line
    m = parse_line(":server MRPURGE #lobby DONE :42")
    ev = _msg_to_event(m, my_nick="bob")
    assert ev["kind"] == "purge"
    assert ev["channel"] == "#lobby"
    assert ev.get("done") is True
    assert ev.get("count") == 42


# ----- mr_whoami sync snapshot (self-pinpoint in collision env) -----

# Pure unit tests on the bridge-local snapshot. No wire I/O is involved,
# so we drive the client object directly without standing up a server.


def _make_client_in_state(nick="claude_miko_2", user="claude_miko",
                         joined=("#lobby",), subs=("*",)):
    """Build a MixedRelayClient instance with its in-memory state populated
    enough to exercise whoami(). We bypass connect() so no socket is opened."""
    from mrelay_mcp.client import MixedRelayClient
    c = MixedRelayClient(addr="127.0.0.1:1", nick=nick, user=user, kind="agent")
    c._joined_channels = set(joined)
    c._status_subs = set(subs)
    return c


def test_whoami_returns_post_suffix_nick_and_base_user():
    c = _make_client_in_state(nick="claude_miko_2", user="claude_miko")
    snap = c.whoami()
    # The point of mr_whoami: nick is whatever the server gave us (suffixed),
    # user is the unchanged base reader-key. Together they identify "us"
    # uniquely in a collision environment.
    assert snap["nick"] == "claude_miko_2"
    assert snap["user"] == "claude_miko"
    assert snap["kind"] == "agent"


def test_whoami_lists_joined_channels_sorted():
    c = _make_client_in_state(joined=("#zzz", "#aaa", "#mmm"))
    snap = c.whoami()
    assert snap["joined_channels"] == ["#aaa", "#mmm", "#zzz"]


def test_whoami_lists_status_subs_sorted():
    c = _make_client_in_state(subs=("bob", "*", "alice"))
    snap = c.whoami()
    assert snap["status_subs"] == ["*", "alice", "bob"]


def test_whoami_reports_disconnected_state():
    c = _make_client_in_state()
    # No connect() was called → no socket → connected must be False.
    assert c.whoami()["connected"] is False
