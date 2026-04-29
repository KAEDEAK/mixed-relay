"""MCP server exposing MixedRelay v0.0.3 as tools.

Channel-only, transparency-first. Agents talk in channels — no DMs, no
tasks, no credentials. USER is a public reader-key bookmark.
"""
from __future__ import annotations

import argparse
import functools
import inspect
import json
import os
import threading
import time
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from .client import BrokenConnection, MixedRelayClient, RemoteError


# ---- bridge config ----


class BridgeConfig:
    def __init__(self) -> None:
        self.addr = os.environ.get("MRELAY_ADDR", "127.0.0.1:6767")
        self.nick = os.environ.get("MRELAY_NICK", "mcp-agent")
        self.user = os.environ.get("MRELAY_USER", os.environ.get("MRELAY_NICK", "mcp-agent"))
        self.kind = os.environ.get("MRELAY_KIND", "agent")
        # F-11: server keepalive (PING/PONG) keeps the socket warm, so the
        # bridge no longer needs to self-recycle on idle. Default 0 = disabled.
        # Set MRELAY_IDLE to a positive value only if you want the bridge to
        # proactively tear down its socket after N seconds of tool inactivity
        # (opt-in, not recommended for lobby-resident use).
        self.idle_timeout_sec = int(os.environ.get("MRELAY_IDLE", "0"))


# ---- shared client with lazy reconnect ----


class BridgeSession:
    """Owns the long-lived caller intent — joined channels, status subs,
    profile/status payloads — across short-lived MixedRelayClient instances.

    feedback-3 (must): the wire client is only alive between connect() and
    the next idle/broken event. ``get()`` discards the dead client and
    spins up a new one, so any state stored on the *client* is lost. The
    rejoin replay therefore lives here, on the session, and is *copied
    onto* the freshly built client before its connect() runs.
    """

    def __init__(self, cfg: BridgeConfig) -> None:
        self.cfg = cfg
        self._client: Optional[MixedRelayClient] = None
        self._lock = threading.Lock()
        self._last_use = 0.0
        self._reconnect_pending = False
        self._missed_ms = 0
        # Session-scoped intent (survives client recreation).
        self._joined_channels: set[str] = set()
        self._status_subs: set[str] = set()
        self._last_profile: Optional[dict] = None
        self._last_status: Optional[dict] = None

    def get(self) -> MixedRelayClient:
        with self._lock:
            now = time.monotonic()
            need_new = False
            if self._client is None:
                need_new = True
            elif self._client._broken:
                need_new = True
            elif self.cfg.idle_timeout_sec > 0 and now - self._last_use > self.cfg.idle_timeout_sec:
                need_new = True
            if need_new:
                # Snapshot caller intent from the dying client *before* we
                # discard it, so the new client's connect() can replay it.
                # First-use case: nothing to snapshot, _last_*/_joined_* are
                # already empty.
                if self._client is not None:
                    if self._last_use:
                        self._missed_ms = int((now - self._last_use) * 1000)
                    self._joined_channels = set(self._client._joined_channels)
                    self._status_subs = set(self._client._status_subs)
                    self._last_profile = self._client._last_profile
                    self._last_status = self._client._last_status
                    try:
                        self._client.close()
                    except Exception:
                        pass
                c = MixedRelayClient(
                    addr=self.cfg.addr,
                    nick=self.cfg.nick,
                    user=self.cfg.user,
                    kind=self.cfg.kind,
                )
                # Push the carried intent onto the new client BEFORE
                # connect() runs its replay loop.
                c._joined_channels = set(self._joined_channels)
                c._status_subs = set(self._status_subs)
                c._last_profile = self._last_profile
                c._last_status = self._last_status
                c.connect()
                self._client = c
                self._reconnect_pending = self._missed_ms > 0
            self._last_use = now
            return self._client

    def consume_reconnect_event(self) -> Optional[dict]:
        with self._lock:
            if not self._reconnect_pending:
                return None
            self._reconnect_pending = False
            return {
                "kind": "raw",
                "command": "MRRECONNECT",
                "prefix": "bridge",
                "params": [],
                "data": {"missed_window_ms": self._missed_ms, "reason": "broken_socket_or_idle"},
            }


# ---- helpers ----


def _ok(client: MixedRelayClient, extra: dict | None = None) -> dict:
    out: dict[str, Any] = {"ok": True}
    if extra:
        out.update(extra)
    return out


def _run(thunk):
    """Standard error envelope for MCP tool bodies.

    We deliberately do NOT use a function wrapper / decorator: FastMCP builds
    the tool JSON schema from the function's actual code-object parameter
    names (``co_varnames``), not from ``__signature__`` or ``__wrapped__``.
    A naive ``def inner(*args, **kwargs)`` wrapper would erase every typed
    parameter and expose tools as taking ``args`` / ``kwargs`` strings only.
    Each tool body therefore calls ``_run(lambda: ...)`` directly.
    """
    try:
        return thunk()
    except RemoteError as e:
        return {"ok": False, "error": {"code": e.code, "message": e.text}}
    except BrokenConnection as e:
        return {"ok": False, "error": {"code": 0, "message": f"broken: {e}"}}
    except Exception as e:
        return {"ok": False, "error": {"code": -1, "message": str(e)}}


# ---- FastMCP app ----


cfg = BridgeConfig()
session = BridgeSession(cfg)
mcp = FastMCP("mrelay-mcp")


def _do_join(channel: str) -> dict:
    c = session.get()
    return _ok(c, {"bundle": c.join(channel)})


def _do_part(channel: str, reason: str) -> dict:
    c = session.get()
    c.part(channel, reason)
    return _ok(c)


def _do_say(channel: str, text: str) -> dict:
    c = session.get()
    c.say(channel, text)
    return _ok(c)


def _do_who(channel: str, kind: str) -> dict:
    c = session.get()
    if kind:
        c.send_raw("MRWHO", [channel, kind])
    else:
        c.send_raw("MRWHO", [channel])
    return _ok(c)


def _do_channels() -> dict:
    c = session.get()
    return _ok(c, {"channels": c.channels()})


def _do_whois(nick: str) -> dict:
    c = session.get()
    return _ok(c, {"info": c.whois(nick)})


def _do_set_profile(profile: dict) -> dict:
    c = session.get()
    c.set_profile(profile)
    return _ok(c)


def _do_get_profile(nick: str) -> dict:
    c = session.get()
    return _ok(c, {"profile": c.get_profile(nick)})


def _do_set_status(patch: dict) -> dict:
    c = session.get()
    c.set_status(patch)
    return _ok(c)


def _do_subscribe_status(nick: str) -> dict:
    c = session.get()
    c.subscribe_status(nick)
    return _ok(c)


def _do_set_topic(channel: str, text: str) -> dict:
    c = session.get()
    c.set_topic(channel, text)
    return _ok(c)


def _do_get_topic(channel: str) -> dict:
    c = session.get()
    return _ok(c, {"topic": c.get_topic(channel)})


def _do_history(channel: str, direction: str, anchor: int, limit: int) -> dict:
    c = session.get()
    return _ok(c, {"entries": c.history(channel, direction=direction, anchor=anchor, limit=limit)})


def _do_read_get(channel: str) -> dict:
    c = session.get()
    return _ok(c, {"seq": c.read_get(channel)})


def _do_read_set(channel: str, seq: int) -> dict:
    c = session.get()
    c.read_set(channel, seq)
    return _ok(c)


def _do_archive(channel: str, before_date: str) -> dict:
    c = session.get()
    return _ok(c, {"segment": c.archive(channel, before_date)})


def _do_archive_list(channel: str) -> dict:
    c = session.get()
    return _ok(c, {"segments": c.archive_list(channel)})


def _do_purge(channel: str) -> dict:
    c = session.get()
    return _ok(c, {"purged": c.purge(channel)})


def _do_rename(new_nick: str) -> dict:
    c = session.get()
    c.rename(new_nick)
    return _ok(c, {"nick": c.nick})


def _do_poll(timeout_ms: int) -> dict:
    c = session.get()
    events = c.poll(timeout_ms=timeout_ms)
    rec = session.consume_reconnect_event()
    if rec is not None:
        events.insert(0, rec)
    return _ok(c, {"events": events})


@mcp.tool()
def mr_join(channel: str) -> dict:
    """Join a channel and return the welcome bundle (members + topic + cursor summary)."""
    return _run(lambda: _do_join(channel))


@mcp.tool()
def mr_part(channel: str, reason: str = "") -> dict:
    """Leave a channel."""
    return _run(lambda: _do_part(channel, reason))


@mcp.tool()
def mr_say(channel: str, text: str) -> dict:
    """Send a PRIVMSG to a channel. DMs are disabled in v0.0.3."""
    return _run(lambda: _do_say(channel, text))


@mcp.tool()
def mr_who(channel: str, kind: str = "") -> dict:
    """List members of a channel. Server replies with MRWHO frames ending in MRWHO END; consume via mr_poll."""
    return _run(lambda: _do_who(channel, kind))


@mcp.tool()
def mr_channels() -> dict:
    """List all channels on the server with member count and last seq."""
    return _run(_do_channels)


@mcp.tool()
def mr_whois(nick: str) -> dict:
    """Look up identity / kind / reader-key summary for a nick."""
    return _run(lambda: _do_whois(nick))


@mcp.tool()
def mr_set_profile(profile: dict) -> dict:
    """Publish your public profile (capabilities, modalities, etc.)."""
    return _run(lambda: _do_set_profile(profile))


@mcp.tool()
def mr_get_profile(nick: str) -> dict:
    """Fetch a peer's public profile."""
    return _run(lambda: _do_get_profile(nick))


@mcp.tool()
def mr_set_status(patch: dict) -> dict:
    """Update your structured status via JSON Merge Patch (RFC 7396).

    Conventional fields (schema not enforced):
      current_focus  — what you are working on (soft claim, C2)
      intent         — what you are about to do (C1)
      paused_reason  — if stopped, why (C4)
    """
    return _run(lambda: _do_set_status(patch))


@mcp.tool()
def mr_subscribe_status(nick: str = "*") -> dict:
    """Subscribe to status updates for a nick (or '*' for everyone)."""
    return _run(lambda: _do_subscribe_status(nick))


@mcp.tool()
def mr_set_topic(channel: str, text: str) -> dict:
    """Set the channel topic. The channel goal lives here (C6)."""
    return _run(lambda: _do_set_topic(channel, text))


@mcp.tool()
def mr_get_topic(channel: str) -> dict:
    """Read the current channel topic."""
    return _run(lambda: _do_get_topic(channel))


@mcp.tool()
def mr_history(channel: str, direction: str = "before", anchor: int = 0, limit: int = 100) -> dict:
    """Fetch channel history. direction='before'|'after', anchor=seq.

    Spans active log and archive segments transparently (F-9.5).
    """
    return _run(lambda: _do_history(channel, direction, anchor, limit))


@mcp.tool()
def mr_read_get(channel: str) -> dict:
    """Get your read cursor for a channel."""
    return _run(lambda: _do_read_get(channel))


@mcp.tool()
def mr_read_set(channel: str, seq: int) -> dict:
    """Update your read cursor (declare what you have read so far)."""
    return _run(lambda: _do_read_set(channel, seq))


@mcp.tool()
def mr_archive(channel: str, before_date: str) -> dict:
    """Archive log entries on or before a date (YYYY-MM-DD or ms timestamp).

    Cut entries are written to a separate folder; active log shrinks. The
    seq numbering stays continuous and MRHISTORY still spans the boundary.
    """
    return _run(lambda: _do_archive(channel, before_date))


@mcp.tool()
def mr_archive_list(channel: str) -> dict:
    """List archive segments for a channel."""
    return _run(lambda: _do_archive_list(channel))


@mcp.tool()
def mr_rename(new_nick: str) -> dict:
    """Rename yourself. Read cursor continuity is preserved via USER (reader key)."""
    return _run(lambda: _do_rename(new_nick))


@mcp.tool()
def mr_purge(channel: str) -> dict:
    """Completely delete all log entries, cursors, and archive segments for a
    channel. The channel starts fresh (seq 0). Use when switching projects
    and old history is noise. This is irreversible."""
    return _run(lambda: _do_purge(channel))


@mcp.tool()
def mr_poll(timeout_ms: int = 0) -> dict:
    """Drain pending wire events for this bridge session."""
    return _run(lambda: _do_poll(timeout_ms))


def _do_wait_for(kind: str, from_nick: str, channel: str, text_contains: str, timeout_ms: int) -> dict:
    c = session.get()
    deadline = time.monotonic() + timeout_ms / 1000.0
    # feedback-3: surface MRRECONNECT so the caller can see they lost the
    # channel and decide whether to retry. The bridge already restored
    # membership by this point, but visibility matters for debugging.
    rec = session.consume_reconnect_event()
    if rec is not None:
        return _ok(c, {"event": rec, "reason": "reconnect"})
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _ok(c, {"event": None, "reason": "timeout"})
        events = c.poll(timeout_ms=int(remaining * 1000))
        for ev in events:
            if kind and ev.get("kind") != kind:
                continue
            if from_nick and ev.get("from") != from_nick:
                continue
            if channel and ev.get("channel") != channel and ev.get("target") != channel:
                continue
            if text_contains and text_contains not in (ev.get("text") or ""):
                continue
            return _ok(c, {"event": ev, "reason": "matched"})


@mcp.tool()
def mr_wait_for(
    kind: str = "",
    from_nick: str = "",
    channel: str = "",
    text_contains: str = "",
    timeout_ms: int = 30000,
) -> dict:
    """Block until one event matching the filters arrives, or timeout."""
    return _run(lambda: _do_wait_for(kind, from_nick, channel, text_contains, timeout_ms))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--addr", default=cfg.addr)
    parser.add_argument("--nick", default=cfg.nick)
    parser.add_argument("--user", default=cfg.user)
    parser.add_argument("--kind", default=cfg.kind)
    args = parser.parse_args()
    cfg.addr = args.addr
    cfg.nick = args.nick
    cfg.user = args.user
    cfg.kind = args.kind
    mcp.run()


if __name__ == "__main__":
    main()
