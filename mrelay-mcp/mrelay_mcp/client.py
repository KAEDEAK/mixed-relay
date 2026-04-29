"""MixedRelay v0.0.3 wire client.

Channel-only, transparency-first. No DMs, no MRTASK, no credentials.
USER is the public reader key used to continue read cursors across NICK
renames.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RemoteError(Exception):
    """Server returned an ERROR frame."""

    def __init__(self, code: int, text: str) -> None:
        super().__init__(f"{code}: {text}")
        self.code = code
        self.text = text


class BrokenConnection(Exception):
    """Socket died mid-RPC. Caller should reconnect."""


# ---------------------------------------------------------------------------
# Wire parser (minimal IRC-ish)
# ---------------------------------------------------------------------------


@dataclass
class Message:
    prefix: str = ""
    command: str = ""
    params: list[str] = None  # type: ignore[assignment]
    trailing: str = ""
    has_trail: bool = False

    def __post_init__(self) -> None:
        if self.params is None:
            self.params = []


def parse_line(line: str) -> Message:
    m = Message()
    rest = line
    if rest.startswith(":"):
        sp = rest.find(" ")
        m.prefix = rest[1:sp]
        rest = rest[sp + 1 :].lstrip(" ")
    sp = rest.find(" ")
    if sp < 0:
        m.command = rest.upper()
        return m
    m.command = rest[:sp].upper()
    rest = rest[sp + 1 :].lstrip(" ")
    while rest:
        if rest[0] == ":":
            m.trailing = rest[1:]
            m.has_trail = True
            break
        sp = rest.find(" ")
        if sp < 0:
            m.params.append(rest)
            break
        m.params.append(rest[:sp])
        rest = rest[sp + 1 :].lstrip(" ")
    return m


def encode(prefix: str, cmd: str, params: list[str], trailing: str = "", has_trail: bool = False) -> str:
    parts = []
    if prefix:
        parts.append(":" + prefix)
    parts.append(cmd)
    parts.extend(params)
    line = " ".join(parts)
    if has_trail or " " in trailing or trailing.startswith(":"):
        line += " :" + trailing
    return line


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class MixedRelayClient:
    """Synchronous wire client with a background reader.

    Public methods are designed to be called from a single thread (the GUI
    worker / MCP tool handler). Reader runs in its own thread; events arrive
    through ``poll`` while RPC-style commands wait for a matching reply via
    ``_pop_match``.
    """

    def __init__(
        self,
        addr: str,
        nick: str,
        kind: str = "agent",
        user: Optional[str] = None,
        token: Any = None,  # accepted for backwards compat; ignored (no auth in v003)
    ) -> None:
        host, _, port = addr.partition(":")
        self._host = host or "127.0.0.1"
        self._port = int(port or "6767")
        self._nick = nick
        self._user = user or nick
        self._kind = kind
        self._sock: Optional[socket.socket] = None
        self._rx_buf = b""
        self._queue: deque[Message] = deque()
        self._cv = threading.Condition()
        self._rpc_lock = threading.Lock()
        self._broken = False
        self._reader: Optional[threading.Thread] = None
        # Predicates registered by in-flight RPCs. ``poll`` skips any frame
        # matching one of these so the RPC's ``_pop_match`` can claim it.
        # Guarded by ``self._cv``.
        self._pending_preds: list[Callable[[Message], bool]] = []
        # rev10: session-scoped state that must be restored after a
        # transparent reconnect. ``connect()`` uses these to replay the
        # JOINs and status subscriptions the caller had before the drop,
        # so the "I was in #lobby, I still am" illusion stays true.
        self._joined_channels: set[str] = set()
        self._status_subs: set[str] = set()
        # Last MRSTATUS SET we sent — so the peer's view of us survives
        # reconnect (the server loses per-connection status on drop).
        self._last_status: Optional[dict] = None
        self._last_profile: Optional[dict] = None

    # ----- properties -----

    @property
    def nick(self) -> str:
        return self._nick

    @property
    def user(self) -> str:
        return self._user

    # ----- connection lifecycle -----

    def connect(self) -> None:
        s = socket.create_connection((self._host, self._port), timeout=10)
        s.settimeout(None)
        self._sock = s
        self._broken = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self.send_raw("NICK", [self._nick])
        self.send_raw("USER", [self._user, "0", "*"], trailing=self._nick, has_trail=True)
        self.send_raw("MRKIND", [self._kind])
        # wait for top-level MRWELCOME (server welcome banner, no channel param)
        m = self._pop_match(lambda m: m.command == "MRWELCOME" and not m.params, timeout=3)
        if m is None:
            raise BrokenConnection("server did not greet within 3s")
        # rev10: restore session-scoped state so the caller doesn't have to
        # know a reconnect happened (F-12). Profile / status first so peers
        # see a coherent snapshot; then rejoin channels; then replay status
        # subscriptions. We silently swallow individual failures here —
        # the rejoin loop is best-effort and the caller will notice via
        # missing JOIN frames if something went wrong.
        if self._last_profile is not None:
            try:
                self.set_profile(self._last_profile)
            except Exception:
                pass
        if self._last_status is not None:
            try:
                self.send_raw(
                    "MRSTATUS",
                    ["SET"],
                    trailing=json.dumps(self._last_status, ensure_ascii=False),
                    has_trail=True,
                )
            except Exception:
                pass
        for ch in list(self._joined_channels):
            try:
                # use the internal send path, not .join(), to avoid
                # double-booking the rejoin into the membership set.
                self.send_raw("JOIN", [ch])
                self._pop_match(
                    lambda m, ch=ch: m.command == "MRWELCOME" and m.params and m.params[0] == ch,
                    timeout=3,
                )
            except Exception:
                pass
        for nick in list(self._status_subs):
            try:
                self.send_raw("MRSUB", ["STATUS", nick])
            except Exception:
                pass

    def close(self) -> None:
        if self._sock is None:
            return
        try:
            self.send_raw("QUIT", [], trailing="bye", has_trail=True)
        except Exception:
            pass
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        self._sock = None
        self._broken = True
        with self._cv:
            self._cv.notify_all()

    # ----- low-level send -----

    def send_raw(self, command: str, params: Optional[list[str]] = None, trailing: str = "", has_trail: bool = False) -> None:
        if self._sock is None:
            raise BrokenConnection("not connected")
        line = encode("", command, params or [], trailing, has_trail)
        try:
            self._sock.sendall((line + "\r\n").encode("utf-8"))
        except OSError as e:
            self._broken = True
            raise BrokenConnection(f"send failed: {e}")

    def send_verbatim(self, raw: str) -> None:
        if self._sock is None:
            raise BrokenConnection("not connected")
        if not raw.endswith("\r\n"):
            raw = raw + "\r\n"
        try:
            self._sock.sendall(raw.encode("utf-8"))
        except OSError as e:
            self._broken = True
            raise BrokenConnection(f"send failed: {e}")

    # ----- read loop -----

    def _read_loop(self) -> None:
        while True:
            try:
                if self._sock is None:
                    return
                chunk = self._sock.recv(4096)
            except OSError:
                chunk = b""
            if not chunk:
                self._broken = True
                with self._cv:
                    self._cv.notify_all()
                return
            self._rx_buf += chunk
            while b"\n" in self._rx_buf:
                line, self._rx_buf = self._rx_buf.split(b"\n", 1)
                line = line.rstrip(b"\r").decode("utf-8", errors="replace")
                if not line:
                    continue
                m = parse_line(line)
                # F-11: auto-reply to server PING so the socket stays warm
                # even if the MCP tool layer is idle. PONG is sent from the
                # reader thread (the only code path with a guaranteed live
                # socket reference) and is NOT enqueued for consumers.
                if m.command == "PING":
                    try:
                        self.send_raw("PONG", [], trailing=m.trailing, has_trail=m.has_trail)
                    except Exception:
                        pass
                    continue
                with self._cv:
                    self._queue.append(m)
                    self._cv.notify_all()

    def _pop_match(self, pred: Callable[[Message], bool], timeout: float) -> Optional[Message]:
        deadline = time.monotonic() + timeout
        with self._cv:
            self._pending_preds.append(pred)
            try:
                while True:
                    for i, m in enumerate(self._queue):
                        if pred(m):
                            del self._queue[i]
                            return m
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or self._broken:
                        return None
                    self._cv.wait(timeout=remaining)
            finally:
                try:
                    self._pending_preds.remove(pred)
                except ValueError:
                    pass

    # ----- poll (event consumer for the GUI / MCP tools) -----

    def poll(self, timeout_ms: int = 0) -> list[dict]:
        out: list[dict] = []
        with self._cv:
            if not self._has_consumable() and timeout_ms > 0:
                self._cv.wait(timeout=timeout_ms / 1000.0)
            # Drain everything that no in-flight RPC has dibs on. Frames
            # matching a pending predicate stay in the queue so the RPC
            # waiter can grab them.
            kept: deque[Message] = deque()
            while self._queue:
                m = self._queue.popleft()
                if any(p(m) for p in self._pending_preds):
                    kept.append(m)
                else:
                    out.append(_msg_to_event(m, self._nick))
            # restore reserved frames in original order at the front
            while kept:
                self._queue.appendleft(kept.pop())
        return out

    def _has_consumable(self) -> bool:
        if not self._queue:
            return False
        if not self._pending_preds:
            return True
        for m in self._queue:
            if not any(p(m) for p in self._pending_preds):
                return True
        return False

    # ----- profile / status -----

    def set_profile(self, profile: dict) -> None:
        self._last_profile = profile
        self.send_raw("MRPROFILE", ["SET"], trailing=json.dumps(profile, ensure_ascii=False), has_trail=True)

    def get_profile(self, nick: str) -> Any:
        with self._rpc_lock:
            self.send_raw("MRPROFILE", ["GET", nick])
            m = self._pop_match(
                lambda m: (m.command == "MRPROFILE" and m.params and m.params[0].lower() == nick.lower())
                or m.command == "ERROR",
                timeout=3,
            )
        if m is None:
            raise BrokenConnection("MRPROFILE GET timed out")
        if m.command == "ERROR":
            raise RemoteError(int(m.params[0]) if m.params else 0, m.trailing)
        return json.loads(m.trailing) if m.trailing else None

    def set_status(self, patch: dict) -> None:
        # Merge locally so the rejoin replay uses the full current state,
        # not just the latest delta.
        if self._last_status is None:
            self._last_status = {}
        self._last_status = _merge_patch(self._last_status, patch)
        self.send_raw("MRSTATUS", ["SET"], trailing=json.dumps(patch, ensure_ascii=False), has_trail=True)

    def subscribe_status(self, nick: str = "*") -> None:
        self._status_subs.add(nick)
        self.send_raw("MRSUB", ["STATUS", nick])

    # ----- presence / queries -----

    def join(self, channel: str) -> dict:
        with self._rpc_lock:
            self.send_raw("JOIN", [channel])
            m = self._pop_match(
                lambda m: (m.command == "MRWELCOME" and m.params and m.params[0] == channel)
                or m.command == "ERROR",
                timeout=3,
            )
        if m is None:
            raise BrokenConnection("JOIN timed out")
        if m.command == "ERROR":
            raise RemoteError(int(m.params[0]) if m.params else 0, m.trailing)
        self._joined_channels.add(channel)
        return json.loads(m.trailing) if m.has_trail else {}

    def part(self, channel: str, reason: str = "") -> None:
        if reason:
            self.send_raw("PART", [channel], trailing=reason, has_trail=True)
        else:
            self.send_raw("PART", [channel])
        self._joined_channels.discard(channel)

    def say(self, channel: str, text: str) -> None:
        """Send PRIVMSG and block briefly for ack (server echoes the frame
        back to all channel members, including us) or ERROR. This closes
        the feedback-3 gap where a send-only tool returned ok even when the
        server had dropped us from the channel."""
        with self._rpc_lock:
            self.send_raw("PRIVMSG", [channel], trailing=text, has_trail=True)
            my = self._nick.lower()
            m = self._pop_match(
                lambda m: (
                    m.command == "PRIVMSG"
                    and len(m.params) >= 1
                    and m.params[0] == channel
                    and _nick_from_prefix(m.prefix).lower() == my
                    and m.trailing == text
                )
                or m.command == "ERROR",
                timeout=3,
            )
        if m is None:
            raise BrokenConnection("PRIVMSG not acknowledged within 3s")
        if m.command == "ERROR":
            raise RemoteError(int(m.params[0]) if m.params else 0, m.trailing)

    def whois(self, nick: str) -> dict:
        with self._rpc_lock:
            self.send_raw("WHOIS", [nick])
            m = self._pop_match(
                lambda m: (m.command == "WHOIS" and m.params and m.params[0].lower() == nick.lower())
                or m.command == "ERROR",
                timeout=3,
            )
        if m is None:
            raise BrokenConnection("WHOIS timed out")
        if m.command == "ERROR":
            raise RemoteError(int(m.params[0]) if m.params else 0, m.trailing)
        return json.loads(m.trailing) if m.trailing else {}

    def rename(self, new_nick: str) -> None:
        with self._rpc_lock:
            self.send_raw("NICK", [new_nick])
            # wait briefly for either ERROR (rejection) or our own NICK echo
            m = self._pop_match(
                lambda m: m.command == "ERROR"
                or (m.command == "NICK" and m.has_trail and m.trailing == new_nick),
                timeout=1,
            )
        if m is not None and m.command == "ERROR":
            raise RemoteError(int(m.params[0]) if m.params else 0, m.trailing)
        self._nick = new_nick

    def channels(self) -> list[dict]:
        with self._rpc_lock:
            self.send_raw("MRCHANNELS", [])
            out: list[dict] = []
            while True:
                m = self._pop_match(lambda m: m.command == "MRCHANNELS", timeout=3)
                if m is None:
                    raise BrokenConnection("MRCHANNELS timed out")
                if m.params and m.params[0] == "END":
                    return out
                if len(m.params) >= 3:
                    out.append({
                        "name": m.params[0],
                        "members": int(m.params[1]),
                        "last_seq": int(m.params[2]),
                        "topic": m.trailing,
                    })

    def get_topic(self, channel: str) -> str:
        with self._rpc_lock:
            self.send_raw("TOPIC", [channel])
            m = self._pop_match(
                lambda m: (m.command == "TOPIC" and m.params and m.params[0] == channel) or m.command == "ERROR",
                timeout=3,
            )
        if m is None:
            raise BrokenConnection("TOPIC GET timed out")
        if m.command == "ERROR":
            raise RemoteError(int(m.params[0]) if m.params else 0, m.trailing)
        return m.trailing

    def set_topic(self, channel: str, text: str) -> None:
        self.send_raw("TOPIC", [channel], trailing=text, has_trail=True)

    # ----- history & cursor -----

    def history(self, channel: str, *, direction: str = "before", anchor: int = 0, limit: int = 100) -> list[dict]:
        with self._rpc_lock:
            self.send_raw("MRHISTORY", [channel, direction.upper(), str(anchor), str(limit)])
            out: list[dict] = []
            while True:
                m = self._pop_match(lambda m: m.command == "MRHISTORY", timeout=5)
                if m is None:
                    raise BrokenConnection("MRHISTORY timed out")
                if m.params and m.params[0] == "END":
                    return out
                if m.has_trail:
                    out.append(json.loads(m.trailing))

    def read_get(self, channel: str) -> int:
        with self._rpc_lock:
            self.send_raw("MRREAD", ["GET", channel])
            m = self._pop_match(
                lambda m: m.command == "MRREAD" and len(m.params) >= 2 and m.params[0] == channel,
                timeout=3,
            )
        if m is None:
            raise BrokenConnection("MRREAD GET timed out")
        return int(m.params[1])

    def read_set(self, channel: str, seq: int) -> None:
        self.send_raw("MRREAD", ["SET", channel, str(seq)])

    # ----- archive -----

    def archive(self, channel: str, before_date: str) -> dict:
        with self._rpc_lock:
            self.send_raw("MRARCHIVE", [channel, "BEFORE", before_date])
            m = self._pop_match(
                lambda m: (m.command == "MRARCHIVE" and len(m.params) >= 2 and m.params[1] == "DONE")
                or m.command == "ERROR",
                timeout=10,
            )
        if m is None:
            raise BrokenConnection("MRARCHIVE timed out")
        if m.command == "ERROR":
            raise RemoteError(int(m.params[0]) if m.params else 0, m.trailing)
        return json.loads(m.trailing) if m.has_trail else {}

    def archive_list(self, channel: str) -> list[dict]:
        with self._rpc_lock:
            self.send_raw("MRARCHIVE", ["LIST", channel])
            out: list[dict] = []
            while True:
                m = self._pop_match(lambda m: m.command == "MRARCHIVE" and len(m.params) >= 2, timeout=3)
                if m is None:
                    raise BrokenConnection("MRARCHIVE LIST timed out")
                if m.params[1] == "END":
                    return out
                if m.params[1] == "SEG" and m.has_trail:
                    out.append(json.loads(m.trailing))

    def purge(self, channel: str) -> int:
        """Completely delete all log entries, cursors, and archive segments
        for a channel. Returns the number of purged entries."""
        with self._rpc_lock:
            self.send_raw("MRPURGE", [channel])
            m = self._pop_match(
                lambda m: (m.command == "MRPURGE" and len(m.params) >= 2 and m.params[1] == "DONE")
                or m.command == "ERROR",
                timeout=10,
            )
        if m is None:
            raise BrokenConnection("MRPURGE timed out")
        if m.command == "ERROR":
            raise RemoteError(int(m.params[0]) if m.params else 0, m.trailing)
        return int(m.trailing) if m.has_trail else 0


# ---------------------------------------------------------------------------
# Wire → poll-event mapping
# ---------------------------------------------------------------------------


def _merge_patch(orig: dict, patch: dict) -> dict:
    """RFC 7396 JSON Merge Patch, applied locally so the bridge can replay
    the cumulative status on reconnect."""
    if not isinstance(patch, dict):
        return patch
    out = dict(orig) if isinstance(orig, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_patch(out[k], v)
        else:
            out[k] = v
    return out


def _nick_from_prefix(p: str) -> str:
    if not p:
        return ""
    return p.split("!", 1)[0]


def _msg_to_event(m: Message, my_nick: str) -> dict:
    ev: dict[str, Any] = {
        "kind": "raw",
        "command": m.command,
        "prefix": m.prefix,
        "params": list(m.params),
    }
    if m.has_trail:
        if m.command in ("MRWELCOME", "MRSTATUS", "MRPROFILE", "WHOIS", "MRHISTORY", "MRARCHIVE", "ERROR"):
            try:
                ev["data"] = json.loads(m.trailing)
            except ValueError:
                ev["text"] = m.trailing
        else:
            ev["text"] = m.trailing
    cmd = m.command
    if cmd == "PRIVMSG":
        ev["kind"] = "privmsg"
        ev["from"] = _nick_from_prefix(m.prefix)
        ev["target"] = m.params[0] if m.params else ""
        ev["text"] = m.trailing
    elif cmd == "NOTICE":
        ev["kind"] = "notice"
        ev["from"] = _nick_from_prefix(m.prefix)
        ev["target"] = m.params[0] if m.params else ""
        ev["text"] = m.trailing
    elif cmd == "JOIN":
        ev["kind"] = "join"
        ev["from"] = _nick_from_prefix(m.prefix)
        ev["channel"] = m.params[0] if m.params else ""
    elif cmd == "PART":
        ev["kind"] = "part"
        ev["from"] = _nick_from_prefix(m.prefix)
        ev["channel"] = m.params[0] if m.params else ""
        ev["reason"] = m.trailing
    elif cmd == "NICK":
        ev["kind"] = "nick"
        ev["from"] = _nick_from_prefix(m.prefix)
        ev["new"] = m.trailing if m.has_trail else (m.params[0] if m.params else "")
    elif cmd == "TOPIC":
        ev["kind"] = "topic"
        ev["from"] = _nick_from_prefix(m.prefix)
        ev["channel"] = m.params[0] if m.params else ""
        ev["text"] = m.trailing
    elif cmd == "MRSTATUS":
        ev["kind"] = "status"
        ev["nick"] = m.params[0] if m.params else ""
    elif cmd == "MRPROFILE":
        ev["kind"] = "profile"
        ev["nick"] = m.params[0] if m.params else ""
    elif cmd == "MRWELCOME" and m.params:
        ev["kind"] = "welcome"
        ev["channel"] = m.params[0]
    elif cmd == "ERROR":
        ev["kind"] = "error"
    return ev
