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


class TimeoutWaiting(BrokenConnection):
    """RPC sent but the matching reply (or an ERROR for it) never arrived
    within the deadline. Distinct from a plain ``BrokenConnection`` so
    LLMs can tell ``transport dead`` from ``server slow / lost reply``,
    but it still inherits BrokenConnection so existing GUI / SDK
    ``except BrokenConnection`` handlers keep working unchanged."""


class ValidationError(ValueError):
    """Caller-side argument validation failed before any wire activity.
    Raised so MCP tools can return a clear, immediate error that lets
    even small LLMs notice and correct the mistake (e.g. forgetting the
    leading ``#`` on a channel name).

    Inherits ``ValueError`` so callers that catch the standard built-in
    keep working; new code can ``except ValidationError`` to distinguish
    it from other ValueErrors."""


def _reject_wire_breakers(s: str, label: str) -> None:
    """Refuse strings that would corrupt the line-based wire framing.

    The wire is `\\r\\n`-delimited; a `\\n` (or NUL) embedded in a
    user-supplied param or trailing splits a single intended frame into
    two on the wire, and the server parses the second half as a new
    command. We surface this as ValidationError so MCP tools route it to
    the ``validation:`` prefix path (F-12.1) rather than letting it slip
    through as a silent ERROR 421 cascade.
    """
    if not isinstance(s, str):
        raise ValidationError(f"{label} must be a string")
    for ch, name in (("\n", "newline"), ("\r", "carriage return"), ("\x00", "NUL")):
        if ch in s:
            raise ValidationError(
                f"{label} must not contain {name} ({ch!r}); the wire is line-delimited "
                f"so embedded line terminators corrupt framing"
            )


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

    def whoami(self) -> dict:
        """Synchronous snapshot of bridge-local identity / membership state.

        Exposed so MCP-tool consumers can pinpoint *self* in a collision
        environment where the welcome bundle's ``members`` list contains
        multiple entries sharing the same USER (the caller of mr_join cannot
        otherwise distinguish "the entry that is me" from "another bridge
        instance with the same configured base nick"). All fields are read
        from in-process state — no wire I/O — so this returns immediately
        regardless of socket health.

        Returned shape:
          - ``nick``: current registered nick (post auto-suffix if any)
          - ``user``: stable reader-key (unchanged across rename / suffix)
          - ``kind``: "human" / "agent" / "tool" / "observer"
          - ``joined_channels``: list of channels we believe ourselves to be
            in (sorted; the bridge maintains this set across reconnect via
            the rejoin replay in ``connect()``)
          - ``status_subs``: list of currently-held status subscription
            targets (nicks or "*"; sorted)
          - ``connected``: True if we have a live socket and have not seen
            a broken-flag transition. Useful for distinguishing "we think
            we're #lobby but the socket just died" from healthy state.
        """
        return {
            "nick": self._nick,
            "user": self._user,
            "kind": self._kind,
            "joined_channels": sorted(self._joined_channels),
            "status_subs": sorted(self._status_subs),
            "connected": self._sock is not None and not self._broken,
        }

    # ----- connection lifecycle -----

    def connect(self) -> None:
        s = socket.create_connection((self._host, self._port), timeout=10)
        s.settimeout(None)
        self._sock = s
        self._broken = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        # Auto-suffix on NICK collision so two clients with the same configured
        # nick (e.g. VSCode-Claude + CLI-Claude both spawning a bridge as
        # "claude_miko") can both register without one being silently broken.
        # USER (reader-key / cursor identity) stays unchanged so the human-
        # facing "this is the same person" continuity is preserved across the
        # rename — only the wire NICK gets a numeric suffix.
        base_nick = self._nick
        attempts = 0
        max_attempts = 10
        while True:
            attempts += 1
            self.send_raw("NICK", [self._nick])
            self.send_raw("USER", [self._user, "0", "*"], trailing=self._nick, has_trail=True)
            self.send_raw("MRKIND", [self._kind])
            m = self._pop_match(
                lambda m: (m.command == "MRWELCOME" and not m.params)
                or (m.command == "ERROR" and m.params and m.params[0] in ("433", "432")),
                timeout=3,
            )
            if m is None:
                raise BrokenConnection("server did not greet within 3s")
            if m.command == "MRWELCOME":
                break
            # ERROR 433 (nick in use) or 432 (invalid nick).
            if attempts >= max_attempts:
                raise BrokenConnection(
                    f"could not register a unique nick after {attempts} attempts "
                    f"(last reply: {m.params[0]} {m.trailing!r}); base was {base_nick!r}"
                )
            self._nick = f"{base_nick}_{attempts + 1}"
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
        # Defence-in-depth: reject embedded line terminators / NUL anywhere in
        # the outbound frame. Without this, a multi-line user text in the
        # trailing splits at the first '\n' on the wire and the server parses
        # the rest as fresh commands (we hit this in the wild — a
        # multi-paragraph PRIVMSG produced ERROR 421 "unknown command: (2)"
        # for every paragraph after the first). Catch it before send() so the
        # caller gets a clear ValidationError instead of a silent split.
        for p in params or []:
            _reject_wire_breakers(p, "param")
        if has_trail:
            _reject_wire_breakers(trailing, "trailing")
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
                m = self._pop_match(
                    lambda m: m.command == "MRCHANNELS" or m.command == "ERROR",
                    timeout=3,
                )
                if m is None:
                    raise TimeoutWaiting("MRCHANNELS reply not received")
                if m.command == "ERROR":
                    raise RemoteError(int(m.params[0]) if m.params else 0, m.trailing)
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
                m = self._pop_match(
                    lambda m: m.command == "MRHISTORY" or m.command == "ERROR",
                    timeout=5,
                )
                if m is None:
                    raise TimeoutWaiting("MRHISTORY reply not received within 5s")
                if m.command == "ERROR":
                    raise RemoteError(int(m.params[0]) if m.params else 0, m.trailing)
                if m.params and m.params[0] == "END":
                    return out
                if m.has_trail:
                    out.append(json.loads(m.trailing))

    def read_get(self, channel: str) -> int:
        with self._rpc_lock:
            self.send_raw("MRREAD", ["GET", channel])
            m = self._pop_match(
                lambda m: (m.command == "MRREAD" and len(m.params) >= 2 and m.params[0] == channel)
                or m.command == "ERROR",
                timeout=3,
            )
        if m is None:
            raise TimeoutWaiting("MRREAD GET reply not received within 3s")
        if m.command == "ERROR":
            raise RemoteError(int(m.params[0]) if m.params else 0, m.trailing)
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
                m = self._pop_match(
                    lambda m: (m.command == "MRARCHIVE" and len(m.params) >= 2) or m.command == "ERROR",
                    timeout=3,
                )
                if m is None:
                    raise TimeoutWaiting("MRARCHIVE LIST reply not received within 3s")
                if m.command == "ERROR":
                    raise RemoteError(int(m.params[0]) if m.params else 0, m.trailing)
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
    elif cmd == "MRPURGE":
        # F-13.6 broadcast form: ":<nick>!<user>@host MRPURGE #<ch>".
        # Synchronous purge() swallows the server's :server MRPURGE #ch DONE
        # reply via _pop_match before it reaches poll(), so in normal use this
        # branch only fires for purges performed by *other* agents on a
        # channel we are JOIN'd to. Surface it as a typed event so the
        # consuming LLM can notice the reset rather than seeing a raw frame.
        ev["kind"] = "purge"
        ev["from"] = _nick_from_prefix(m.prefix)
        ev["channel"] = m.params[0] if m.params else ""
        if len(m.params) >= 2 and m.params[1] == "DONE":
            ev["done"] = True
            if m.has_trail:
                try:
                    ev["count"] = int(m.trailing)
                except ValueError:
                    pass
    elif cmd == "ERROR":
        ev["kind"] = "error"
    return ev
