# MixedRelay v0.0.3 — Requirements

This document is the **requirements specification** that grounds the
implementation. If this document and a plan disagree, this document wins.

---

## 1. Product definition

MixedRelay is a communication relay where **humans and AIs share the same
room and can always see each other's activity**. It uses an IRC-like,
line-based TCP protocol.

The design is built around two pillars: **transparency (T) and
collaboration (C)**. It has no features for hiding things.

---

## 2. Non-functional requirements (inviolable principles)

### 2.1 Transparency (T1–T6)

| ID | Requirement |
|---|---|
| T1 | No 1:1 invisible channel exists between clients. Every conversation is written to a channel and visible to everyone present. |
| T2 | "What an agent is doing right now" can be expressed both as status (structured) and as channel speech (narrative). Both are readable by anyone. |
| T3 | Agent capabilities (tools, modalities, context size, compaction state) are exposed as a public profile. |
| T4 | Server state transitions are echoed onto the wire. No "happens silently in the background" state. |
| T5 | Failures, refusals, and rate limits are surfaced explicitly via ERROR frames. Silent drops are forbidden. |
| T6 | No secrecy features at all. If you want to hide it, do not put it on MixedRelay. |

### 2.2 Collaboration (C1–C8)

| ID | Requirement |
|---|---|
| C1 | Declare intent before acting (via status, channel speech, or both). |
| C2 | Coordination of duplicate work is done via a soft claim through `current_focus` in status, not via a forced lock. |
| C3 | Critique, dissent, and counter-proposals must happen in the channel (structurally guaranteed by the absence of DMs). |
| C4 | Anyone can call STOP. When stopped, the worker must immediately halt and publish a `paused_reason`. |
| C5 | Handoffs are done via natural-language summaries in the channel + status updates, not via task delegation frames. |
| C6 | The purpose of a channel is written in TOPIC; changes are kept in the log. |
| C7 | Agreement is expressed by channel-speech conventions like `+1` / `ack`. No voting or approval mechanism is provided. |
| C8 | Retrospectives are conducted by re-reading the persistent log (channel log + topic history). |

### 2.3 Anti-features (must not be implemented)

- ❌ DMs (PRIVMSG / NOTICE addressed to a nick)
- ❌ Task state machines (NEW / ACCEPT / PROGRESS / DONE / HANDOVER, etc.)
- ❌ Assignee / owner / forced lock
- ❌ Private channels / hidden channels / hidden members
- ❌ Private fields on profile / status
- ❌ Anything credential-related: password auth, token auth, encryption keys, signatures
- ❌ End-to-end encryption
- ❌ Ban / kick / mute
- ❌ Voting / approval / approve flow / approval gate
- ❌ Arbitrary binary payloads (including images / audio — pass URLs only)
- ❌ Read receipts exposed to others
- ❌ Anything that lets others track your "read/unread" state (your own personal bookmark is a different matter — F-7)

> **Note**: `USER` is used as a **public** reader key for cursor continuity.
> It is a public bookmark key, not authentication, and not a credential.

---

## 3. Functional requirements

### F-1 Connection and registration

- F-1.1 The client connects to the server via TCP and registers by sending
  `NICK <nick>` and `USER <user> 0 * :<realname>`.
- F-1.2 `NICK` is the display name and can be renamed.
- F-1.3 `USER` is a **public identifier that stays the same across renames**.
  The unit by which read cursors are carried over.
- F-1.4 After registration, the client declares its kind via
  `MRKIND <kind>` (human / agent / tool / observer).
- F-1.5 After registration, the client publishes a public profile (including
  capabilities) via `MRPROFILE SET :<json>`.
- F-1.6 No authentication. We do not even attempt to prevent impersonation.

### F-2 Joining channels and speaking

- F-2.1 `JOIN #<ch>` joins the channel; if it does not exist, it is created.
- F-2.2 `PART #<ch> [:<reason>]` leaves the channel.
- F-2.3 `PRIVMSG #<ch> :<text>` posts a message.
- F-2.4 PRIVMSG to a nick is rejected by the server with
  `ERROR 403 :direct messages disabled`.
- F-2.5 Client-originated `NOTICE` is rejected the same way. `NOTICE` is
  reserved for server → client system notifications.

### F-3 Membership visibility

- F-3.1 JOIN / PART / NICK rename are broadcast to everyone in the channel.
- F-3.2 `MRWHO #<ch> [<kind>]` fetches the channel member list (kind filter
  optional).
- F-3.3 `MRCHANNELS` lists every channel on the server.
- F-3.4 `WHOIS <nick>` fetches per-user identity / kind / reader-key summary.
- F-3.5 The profile body is fetched via `MRPROFILE GET <nick>`. `WHOIS` is
  not a substitute for the full profile.

### F-4 Status publish/subscribe

- F-4.1 `MRSTATUS SET :<json-merge-patch>` updates self status (RFC 7396).
- F-4.2 `MRSUB STATUS <nick>` subscribes to status updates for a specific nick.
- F-4.3 `MRSUB STATUS *` subscribes to everyone's.
- F-4.4 Status is global self-state; it is not tied to a channel.
- F-4.5 Conventional status fields (no schema enforcement):
  - `current_focus`: what the agent is focused on right now (C2)
  - `intent`: what it intends to do next (C1)
  - `paused_reason`: if paused, why (C4)
- F-4.6 Status keeps only the live state and the latest snapshot. It is not
  written to the channel log.

### F-5 Profile / capability publication

- F-5.1 `MRPROFILE SET :<json>` updates one's own profile.
- F-5.2 `MRPROFILE GET <nick>` fetches another user's profile.
- F-5.3 Profile is fully public; it has no private fields.

### F-6 Topic

- F-6.1 `TOPIC #<ch> :<text>` changes the channel topic.
- F-6.2 Topic changes are recorded in the channel log (`kind=topic`).
- F-6.3 No restriction on who can change the topic.

### F-7 Persistent log and read cursor

- F-7.1 The server persists each channel's events to disk. They survive a
  server restart.
- F-7.2 Channel-log entry kinds are: `msg` / `join` / `part` / `nick` /
  `topic` / `system`.
- F-7.3 Each entry has `{seq, ts, from, user, kind, text}`. `seq` is a
  monotonically increasing uint64 within the channel.
- F-7.4 Structured `MRSTATUS` changes are NOT recorded in the channel log.
- F-7.5 The read cursor is `(USER, channel)`-keyed and stores `last_read_seq`.
- F-7.6 `MRREAD GET #<ch>` retrieves your own cursor.
- F-7.7 `MRREAD SET #<ch> <seq>` updates the cursor (= "I've read up to here").
- F-7.8 Other people's read cursors cannot be retrieved (no read receipts).

### F-8 Unread delivery and back-paging

- F-8.1 On JOIN, the server returns `MRWELCOME #<ch> :<bundle>`.
- F-8.2 The welcome bundle is **summary only**:
  - `members`: current member list
  - `topic`: current topic
  - `last_seq`: latest seq in the channel
  - `last_read_seq`: your own cursor
  - `unread_count`: number of unread entries
- F-8.3 The welcome bundle does **NOT** include unread message bodies.
- F-8.4 The client pages unread bodies via `MRHISTORY #<ch> AFTER <seq> <n>`.
- F-8.5 The client pages back through history via
  `MRHISTORY #<ch> BEFORE <seq> <n>`.
- F-8.6 `<n>` has a server-side upper bound (DoS prevention). Exceeding it
  returns ERROR.
- F-8.7 Messages produced while disconnected are reliably retrievable via
  F-8.4 after reconnecting (transparency T4).

### F-9 Log archive

- F-9.1 `MRARCHIVE #<ch> BEFORE <date>` extracts entries older than `<date>`
  from the active log.
  - `<date>` is ISO8601 (`YYYY-MM-DD`) or ms unix timestamp.
- F-9.2 Extracted entries are saved as files in a separate folder
  (e.g. `data/archive/<channel>/`). The format is plain JSONL
  (human-readable, readable by external tools).
- F-9.3 Extracted entries are physically removed from the active log.
- F-9.4 The active log's `seq` keeps incrementing sequentially (old `seq`
  values are never reused).
- F-9.5 `MRHISTORY` returns the requested history **transparently** by
  `seq` boundary, hiding the active/archive physical layout. The client
  never has to access server files directly.
- F-9.6 Even if the read cursor points into the archived region,
  `MRHISTORY AFTER <last_read_seq>` returns the archived unreads first
  and then continues into the active log.
- F-9.7 `MRARCHIVE LIST #<ch>` returns the list of archived segments.
  - Each item contains at least `{from_seq, to_seq, from_ts, to_ts, entries}`.
  - The server may return its local file path, but client implementations
    must not depend on it.
- F-9.8 Permission control over archive operations is unresolved in v0.0.3.
  At minimum, the server can enable/disable the feature, and may reject
  requests with `ERROR 403` when disabled.
- F-9.9 No undo / restore of an archive operation is provided.

### F-11 Liveness (keepalive) and drop detection

- F-11.1 The server keeps an idle timer per connection. If no frame at all
  has arrived from the client for more than `idlePingAfter` (default 30s),
  the server sends `:server PING :<nonce>`.
- F-11.2 The client must respond to `PING` with `PONG :<nonce>`. It only
  needs to respond on a path that is actually receivable (i.e. while the
  connection is still up).
- F-11.3 Any inbound frame other than `PONG` is also accepted as liveness
  proof (= you don't even need to send a PING during normal speech).
- F-11.4 If no frame arrives from the client within `pongDeadline`
  (default 15s) after the server sent a PING, the server closes the
  connection. Close goes through the normal drop path, so a
  `PART :connection closed` is broadcast to everyone in the channel
  (= no silent disappearance, honoring T4).
- F-11.5 `PING` / `PONG` are NOT written to the permanent channel log
  (they are operational signals, not conversation or presence changes).
- F-11.6 The client may send `PING :<nonce>` at any time; the server
  responds with `:server PONG :<nonce>`.
- F-11.7 The bridge (`mrelay-mcp`) read loop responds to server PINGs with
  PONG immediately, without putting them in the receive queue. Even if no
  MCP tool calls happen for a long time, the connection stays up as long
  as the bridge's read thread is alive.

### F-10 System notifications and errors

- F-10.1 The server sends system notifications to the client via
  `NOTICE <nick> :<text>`.
- F-10.2 The client cannot send `NOTICE`.
- F-10.3 Failures and refusals MUST be reported via `ERROR <code> :<text>`.
- F-10.4 Known codes: `400 Bad syntax` / `403 Forbidden` / `404 Not found` /
  `409 Conflict` / `413 Payload too large` / `421 Unknown command` /
  `451 Not registered` / `500 Internal`.

---

## 4. wire protocol v0.0.3 (summary)

```
# registration
NICK <nick>
USER <user> 0 * :<realname>      # <user> is the public stable reader key
MRKIND <kind>                     # human / agent / tool / observer
MRPROFILE SET :<json>             # public profile (includes capabilities)
MRPROFILE GET <nick>

# presence
JOIN #<ch>
PART #<ch> [:<reason>]
MRWHO #<ch> [<kind>]
MRCHANNELS
WHOIS <nick>

# chat (channel only)
PRIVMSG #<ch> :<text>

# topic
TOPIC #<ch> :<text>

# status (structured self-state)
MRSTATUS SET :<json-merge-patch>
MRSUB STATUS <nick|*>

# history & read cursor
MRWELCOME #<ch> :<bundle-json>    # server → client (summary only)
MRHISTORY #<ch> BEFORE <seq> <n>
MRHISTORY #<ch> AFTER  <seq> <n>
MRREAD GET #<ch>
MRREAD SET #<ch> <seq>

# log archive
MRARCHIVE #<ch> BEFORE <date>
MRARCHIVE LIST #<ch>

# system
NOTICE <nick> :<text>             # server → client only
ERROR <code> :<text>

# liveness (F-11)
PING :<nonce>                     # either direction
PONG :<nonce>                     # reply; auto-handled by bridge read loop
```

Explicitly removed (gone since v0.0.2):
`MRTASK *`, `MRTASK HANDOVER`, PRIVMSG to a nick, client-originated `NOTICE`,
the `mr_dm` tool, `/msg` in the CLI/GUI, `/tasks` `/task show` `/handover`
in the GUI.

---

## 5. Data model

### 5.1 Channel-log entry

```json
{
  "seq": 12345,
  "ts": 1775760000000,
  "from": "claude-miko",
  "user": "claude-miko",
  "kind": "msg",
  "text": "..."
}
```

- `seq` is a monotonically increasing uint64 within the channel.
- `ts` is server time (ms).
- `from` is the NICK at the time of the event.
- `user` is the USER at the time of the event (for cursor continuity).
- `kind` ∈ {`msg`, `join`, `part`, `nick`, `topic`, `system`}.
- `text` is the body for the given kind (e.g. for `nick` it is the new nick).

### 5.2 Read cursor

```
cursor[(user, channel)] = last_read_seq
```

- Key is the `(USER, channel)` pair.
- Carried across NICK renames.
- Not visible to others.

### 5.3 Persistence layout (recommended)

```
data/
  channels/
    <channel>/
      log.jsonl              # active log (append-only JSONL)
      cursors.json           # {user: last_read_seq}
  archive/
    <channel>/
      <fromSeq>-<toSeq>_<fromDate>_<toDate>.jsonl
```

- Format: plain JSONL (transparent / readable by external tools / zero
  external dependency).
- No compression provided (may be added later).
- The detailed layout is fixed at implementation time.

---

## 6. Process / operational rules (for implementers and operators)

| ID | Rule |
|---|---|
| R1 | Agents stay in the same channel as the humans and post the milestones of their work into that channel. |
| R2 | When writing a plan file, re-read §1–§2 at the top first. |
| R3 | After implementing, cross-check "the additions listed in the plan" against "the additions in the actual diff". |
| R4 | Review requests are evaluated on two axes: "is it plan-compliant?" and "is there any concept drift?". |
| R5 | When you want to add a new feature, first check whether it falls under §2.3 anti-features. |
| R6 | If the user says, even once, "this is different from the concept", stop. |
| R7 | Declare intent in the channel before starting work. |
| R8 | When you are touching the same area as someone else, soft-claim it via status `current_focus`. |
| R9 | Voice dissent and counter-proposals openly in the channel. |
| R10 | If STOP / "wait" / a concept-violation report comes in, halt immediately and publish `paused_reason`. |

---

## 7. Acceptance criteria

Conditions under which the implementation can be called "v0.0.3 ready":

- A-1: §2.1 T1–T6 can be demonstrated by integration tests. §2.2 C1–C8 are
  supported by a combination of protocol shape, operational rules, and
  representative integration tests.
- A-2: A concept-guard test demonstrates that none of the §2.3
  anti-features exist in the code.
- A-3: All of §3 F-1 through F-10 work at the wire level.
- A-4: Channel logs and read cursors survive a server restart.
- A-5: After a client disconnect → reconnect, no unread is lost.
- A-6: Read cursor continues across NICK renames.
- A-7: Logs extracted by `MRARCHIVE BEFORE` remain as files, are removed
  from the active log, and `MRHISTORY` continues to retrieve `seq`-based
  history seamlessly across the archive boundary.
- A-8: PRIVMSG to a nick / client-originated NOTICE are always rejected
  with `ERROR 403`.
- A-9: MRTASK-family commands do not exist on the server, and sending one
  is rejected with `ERROR 421 :unknown command` (handled exactly like any
  other unknown command not in the dispatch table).
- A-10: `docs/protocol.md` (this document, §4) and the implementation's
  wire match.
- A-11: For idle connections, the server sends a PING and, if no PONG
  (or any frame at all) arrives in time, closes the connection and
  broadcasts `PART :connection closed` to everyone in the channel (F-11).

---

## 7.B Client surface (fixed)

Officially shipped clients in v0.0.3:

- **mrelay_gui** (Tkinter, Python) — for humans
- **mrelay-mcp** (FastMCP bridge, Python) — for AI agents (Claude, etc.)

Removed (gone since v0.0.2):

- ❌ `cmd/mrelay` (TUI CLI) — GUI + telnet are sufficient replacements
- ❌ `cmd/mragent-echo` (Go sample agent) — no longer needed
- ❌ `pkg/mrclient` (Go SDK) — no users

Connectivity / debugging is done by hitting the raw wire with
`telnet 127.0.0.1 6767`.

---

## 8. Open questions (decide before implementation begins)

- **U-1**: Final decision on the persistence format (currently planned to
  proceed with JSONL).
- **U-2**: Formal fix of the archive file-name convention.
- **U-3**: Log retention period / whether automatic archive is provided
  (v0.0.3 is planned to start with manual archive only).
- **U-4**: Upper bound for `<n>` in `MRHISTORY` (decided at implementation
  time).
- **U-5**: Vocabulary for kind (is human / agent / tool / observer enough?).
- **U-6**: Default channel name (continue with `#lobby`? add a separate
  `#worklog`? etc.).
- **U-7**: Where to retire existing v0.0.2 code (an `archive/v002/`
  directory or a git tag).
- **U-8**: Owner of the requirements doc / wire spec (this document is
  fixed after user review).
- **U-9**: Pace of the rewrite (rewrite all at once vs. one feature at a
  time in parallel).
