# MixedRelay v0.0.3 Wire Protocol

本書は [requirements_v003.md §4](requirements_v003.md) から生成した wire 仕様です。
正本は requirements_v003.md であり、本書はその要約・実装者向け参照です。
矛盾した場合は requirements を優先してください。

---

## 1. 基本

- TCP 上の行ベーステキストプロトコル (IRC 派生)
- 行区切り: `\r\n`
- 行最大長: 8192 bytes
- 文字コード: UTF-8
- フレーム形式: `[:<prefix>] <COMMAND> [<param> ...] [:<trailing>]`

---

## 2. Registration

クライアントは接続後、以下を順に送る:

```
NICK <nick>
USER <user> 0 * :<realname>
MRKIND <kind>
```

- `NICK`: 表示名。post-registration rename 可能
- `USER`: **公開の stable reader key**。read cursor の継続単位。認証ではない
- `MRKIND`: `human` / `agent` / `tool` / `observer`
- 登録完了時 server は `:server MRWELCOME :welcome <nick>, you are reader <user>` を返す
- 登録前に許可されるコマンド: `NICK` / `USER` / `MRKIND` / `MRPROFILE` / `PING` / `QUIT`
- それ以外は `ERROR 451 :you have not registered`

---

## 3. Chat (channel only)

```
C: PRIVMSG #<ch> :<text>
S: :<nick>!<user>@host PRIVMSG #<ch> :<text>    (broadcast to all members)
```

- nick 宛 PRIVMSG → `ERROR 403 :direct messages disabled (channel-only)`
- client 発 NOTICE → `ERROR 403 :client NOTICE disabled`
- NOTICE は server → client の system 通知専用

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

- topic 変更は channel log に kind=topic で記録

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

identity / kind / reader key summary のみ。profile 本文は MRPROFILE GET で取得。

---

## 6. Profile

```
C: MRPROFILE SET :<json>
C: MRPROFILE GET <nick>
S: :server MRPROFILE <nick> :<json>
```

完全に公開。private field を持たない。

---

## 7. Status

```
C: MRSTATUS SET :<json-merge-patch>       (RFC 7396)
C: MRSTATUS GET <nick>
S: :<nick> MRSTATUS <nick> :<json>        (update broadcast to subscribers)

C: MRSUB STATUS <nick|*>
S: :<peer> MRSTATUS <peer> :<json>        (snapshot delivery for each matching peer)
```

慣習フィールド (schema 強制なし):
- `current_focus`: いま注力していること (C2)
- `intent`: これからやろうとしている事 (C1)
- `paused_reason`: 停止中ならその理由 (C4)

---

## 8. Welcome Bundle (F-8)

JOIN 時に server が joiner にのみ返す summary:

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

**未読本文は含めない**。client は `MRHISTORY ... AFTER <last_read_seq> <n>` で取得。

---

## 9. History & Read Cursor (F-7, F-8)

### MRHISTORY

```
C: MRHISTORY #<ch> BEFORE <seq> <n>
C: MRHISTORY #<ch> AFTER  <seq> <n>
S: :server MRHISTORY #<ch> :{"seq":1,"ts":...,"from":"...","user":"...","kind":"msg","text":"..."}
S: :server MRHISTORY END #<ch> :<count>
```

- `<n>` 上限: 1000。超過 → `ERROR 413 :MRHISTORY n too large`
- 不正な数値引数 → `ERROR 400`
- active log と archive segments を **透過的に** 跨ぐ (F-9.5)

### MRREAD

```
C: MRREAD GET #<ch>
S: :server MRREAD #<ch> <seq>

C: MRREAD SET #<ch> <seq>
S: :server MRREAD #<ch> <seq> :ok
```

- cursor は `(USER, channel)` 単位。NICK rename を跨いで継続
- 他人の cursor は取得できない (read receipt 化を禁止)

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
- 切り出し後もアクティブ log の seq は連番継続
- MRHISTORY は archive 境界を透過的に辿る

---

## 10.5 Channel reset (F-13)

```
C: MRPURGE #<ch>
S: :<nick>!<user>@host MRPURGE #<ch>             (broadcast to channel members, EXCEPT caller)
S: :server MRPURGE #<ch> DONE :<count>           (reply to caller only)
```

- channel を完全に初期状態へリセット (active log / 全 read cursor / archive segments を破棄)
- `topic` と現在のメンバーシップは保持
- リセット後の次の発言は `seq = 1` から再採番される (F-7.3 の単調性は purge を境に reset)
- `<count>` は削除した entry の合計 (active + archive)
- 取消・partial purge は提供しない
- 権限制限なし (運用合意で制御)
- broadcast は caller を除外する (caller は DONE で結果を知る)。これにより既存クライアントの
  「MRPURGE 送信→DONE 受信」同期パターンは無変更で動作する

---

## 11. Liveness (F-11)

```
S: :server PING :<nonce>          (server → client, idle 30s 後)
C: PONG :<nonce>                  (client → server, 15s 以内)

C: PING :<nonce>                  (client → server, 任意タイミング)
S: :server PONG :<nonce>          (server → client)
```

- `idlePingAfter`: 30 秒 — client から何もフレームが来ない場合に server が PING 送信
- `pongDeadline`: 15 秒 — PING 後にこの期間内に応答 (PONG or 任意フレーム) が無ければ server は接続 close
- 任意の inbound フレームが liveness proof として扱われる (PONG 専用ではない)
- PING / PONG は channel log に記録しない
- bridge (mrelay-mcp) の read loop は server PING を queue に入れず即 PONG 応答

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

## 13. Deprecated / Removed (from v0.0.2)

- `MRTASK *` / `MRTASK HANDOVER` → `ERROR 421`
- nick 宛 `PRIVMSG` → `ERROR 403`
- client 発 `NOTICE` → `ERROR 403`
- `MRCAPS` → `ERROR 421`
