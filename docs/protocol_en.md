# MixedRelay v0.0.3 Wire Protocol

This document is the wire specification generated from
[requirements_v003.md §4](requirements_v003.md). The source of truth is
`requirements_v003.md`; this document is a summary / implementer-facing
reference. If the two disagree, the requirements take precedence.

---

## 1. Basics

- Line-based text protocol over TCP (IRC-derived)
- Line terminator: `\r\n`
- Maximum line length: 8192 bytes
- Character encoding: UTF-8
- Frame format: `[:<prefix>] <COMMAND> [<param> ...] [:<trailing>]`

---

## 2. Registration

Right after connecting, the client sends the following in order:

```
NICK <nick>
USER <user> 0 * :<realname>
MRKIND <kind>
```

- `NICK`: display name. Can be renamed post-registration.
- `USER`: **public stable reader key**. The unit by which read cursors are
  carried over. Not authentication.
- `MRKIND`: `human` / `agent` / `tool` / `observer`
- When registration completes, the server replies with
  `:server MRWELCOME :welcome <nick>, you are reader <user>`.
- Commands allowed before registration: `NICK` / `USER` / `MRKIND` /
  `MRPROFILE` / `PING` / `QUIT`.
- Anything else: `ERROR 451 :you have not registered`.

---

## 3. Chat (channel only)

```
C: PRIVMSG #<ch> :<text>
S: :<nick>!<user>@host PRIVMSG #<ch> :<text>    (broadcast to all members)
```

- PRIVMSG to a nick → `ERROR 403 :direct messages disabled (channel-only)`
- Client-originated NOTICE → `ERROR 403 :client NOTICE disabled`
- NOTICE is reserved for server → client system notifications.

---

## 4. Presence

### JOIN / PART

```
C: JOIN #<ch>
S: :<nick>!<user>@host JOIN #<ch>                (broadcast)
S: :server MRWELCOME #<ch> :<bundle-json>         (to joiner only)

C: PART #<ch> [:<reason>]
S: :<nick>!<user>@host PART #<ch> [:<reason>]     (broadcast)
```

### NICK rename

```
C: NICK <new>
S: :<old>!<user>@host NICK :<new>                 (broadcast to shared channels)
```

- `ERROR 432` invalid nick / `ERROR 433` nick in use

### TOPIC

```
C: TOPIC #<ch> :<text>              (set)
C: TOPIC #<ch>                      (get)
S: :<nick>!<user>@host TOPIC #<ch> :<text>   (broadcast on set)
S: :server TOPIC #<ch> :<text>               (reply to get)
```

- Topic changes are recorded in the channel log with `kind=topic`.

---

## 5. Queries

### MRWHO

```
C: MRWHO #<ch> [<kind>]
S: :server MRWHO #<ch> <nick> <kind> <user>
S: :server MRWHO #<ch> <nick> <kind> <user>
S: :server MRWHO END #<ch> :<count>
```

### MRCHANNELS

```
C: MRCHANNELS
S: :server MRCHANNELS <#ch> <member_count> <last_seq> :<topic>
S: :server MRCHANNELS END
```

### WHOIS

```
C: WHOIS <nick>
S: :server WHOIS <nick> :{"nick":"...","user":"...","kind":"..."}
```

Identity / kind / reader-key summary only. Fetch the profile body via
`MRPROFILE GET`.

---

## 6. Profile

```
C: MRPROFILE SET :<json>
C: MRPROFILE GET <nick>
S: :server MRPROFILE <nick> :<json>
```

Fully public. Has no private fields.

---

## 7. Status

```
C: MRSTATUS SET :<json-merge-patch>       (RFC 7396)
C: MRSTATUS GET <nick>
S: :<nick> MRSTATUS <nick> :<json>        (update broadcast to subscribers)

C: MRSUB STATUS <nick|*>
S: :<peer> MRSTATUS <peer> :<json>        (snapshot delivery for each matching peer)
```

Conventional fields (no schema enforcement):
- `current_focus`: what the agent is focused on right now (C2)
- `intent`: what it intends to do next (C1)
- `paused_reason`: if paused, why (C4)

---

## 8. Welcome Bundle (F-8)

Summary the server returns to the joiner only on JOIN:

```json
{
  "channel": "#lobby",
  "members": [{"nick": "alice", "user": "alice", "kind": "human"}],
  "topic": "current goal",
  "last_seq": 42,
  "last_read_seq": 30,
  "unread_count": 12,
  "server_time_ms": 1775760000000
}
```

**Unread message bodies are NOT included.** The client fetches them with
`MRHISTORY ... AFTER <last_read_seq> <n>`.

---

## 9. History & Read Cursor (F-7, F-8)

### MRHISTORY

```
C: MRHISTORY #<ch> BEFORE <seq> <n>
C: MRHISTORY #<ch> AFTER  <seq> <n>
S: :server MRHISTORY #<ch> :{"seq":1,"ts":...,"from":"...","user":"...","kind":"msg","text":"..."}
S: :server MRHISTORY END #<ch> :<count>
```

- Upper bound on `<n>`: 1000. Exceeding it → `ERROR 413 :MRHISTORY n too large`
- Malformed numeric arguments → `ERROR 400`
- **Transparently** spans active log and archive segments (F-9.5)

### MRREAD

```
C: MRREAD GET #<ch>
S: :server MRREAD #<ch> <seq>

C: MRREAD SET #<ch> <seq>
S: :server MRREAD #<ch> <seq> :ok
```

- Cursor is keyed by `(USER, channel)`. Carried across NICK renames.
- Other people's cursors cannot be retrieved (no read receipts).

---

## 10. Archive (F-9)

```
C: MRARCHIVE #<ch> BEFORE <date>
S: :server MRARCHIVE #<ch> DONE :{"from_seq":...,"to_seq":...,"from_ts":...,"to_ts":...,"entries":...,"file":"..."}

C: MRARCHIVE LIST #<ch>
S: :server MRARCHIVE #<ch> SEG :{"from_seq":...,...}
S: :server MRARCHIVE #<ch> END :<count>
```

- `<date>`: ISO8601 (`YYYY-MM-DD`) or ms unix timestamp
- After extraction, the active log's `seq` continues sequentially.
- `MRHISTORY` traverses the archive boundary transparently.

---

## 11. Liveness (F-11)

```
S: :server PING :<nonce>          (server → client, after 30s idle)
C: PONG :<nonce>                  (client → server, within 15s)

C: PING :<nonce>                  (client → server, anytime)
S: :server PONG :<nonce>          (server → client)
```

- `idlePingAfter`: 30 seconds — if no frame at all has arrived from the
  client, the server sends a PING.
- `pongDeadline`: 15 seconds — if no response (a PONG or any frame at all)
  arrives within this window after a PING, the server closes the connection.
- Any inbound frame counts as liveness proof (it does not have to be a PONG).
- `PING` / `PONG` are not recorded in the channel log.
- The bridge (`mrelay-mcp`) read loop responds to server PINGs with PONG
  immediately, without queuing them.

---

## 12. Error Codes

| Code | Meaning |
|---|---|
| 400 | Bad syntax (malformed command, invalid args) |
| 403 | Forbidden (DM disabled, client NOTICE, archive disabled) |
| 404 | Not found (no such channel, no such nick, not in channel) |
| 409 | Conflict (nick collision) |
| 413 | Payload too large (MRHISTORY n exceeds limit) |
| 421 | Unknown command |
| 432 | Invalid nick |
| 433 | Nick already in use |
| 451 | Not registered |
| 500 | Internal server error |

---

## 13. Deprecated / Removed (since v0.0.2)

- `MRTASK *` / `MRTASK HANDOVER` → `ERROR 421`
- `PRIVMSG` to a nick → `ERROR 403`
- Client-originated `NOTICE` → `ERROR 403`
- `MRCAPS` → `ERROR 421`
