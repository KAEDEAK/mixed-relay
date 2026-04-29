# MixedRelay v0.0.3 — Requirements

本書は実装の根拠となる **要件定義書**。
本書と plan が矛盾した場合、本書を優先する。

---

## 1. 製品定義

MixedRelay は **人間と AI が同じ部屋に居て互いの活動が常に見える**
コミュニケーションリレー。IRC ライクな TCP 行ベースプロトコルを用いる。

設計の核は **透明性 (T) と協調 (C)** の二つ。秘匿機能は持たない。

---

## 2. 非機能要件 (不可侵原則)

### 2.1 透明性 (T1〜T6)

| ID | 要件 |
|---|---|
| T1 | クライアント間の 1:1 不可視通信路を持たない。会話はすべて channel に書かれ、その場に居る全員に見える |
| T2 | エージェントの「いま何をしているか」は status (構造化) と channel 発言 (ナラティブ) の両方で表現できる。両方とも誰でも読める |
| T3 | エージェントの能力 (tools, modalities, context size, compaction state) は profile として公開する |
| T4 | server の状態遷移は wire 上に echo される。「裏で進む」状態を作らない |
| T5 | 失敗・拒否・rate limit は ERROR フレームで明示する。silent drop 禁止 |
| T6 | 秘匿系の機能は一切持たない。隠したい情報は MixedRelay に載せない |

### 2.2 協調 (C1〜C8)

| ID | 要件 |
|---|---|
| C1 | 行動の前に意図を宣言する (status または channel 発言の少なくとも一方) |
| C2 | 重複作業の調整は強制ロックではなく status の `current_focus` による soft claim で行う |
| C3 | 批評・反対・代案は必ず channel で行う (DM が無いことで構造的に保証) |
| C4 | 誰でも STOP を出せ、出されたら作業者は即座に手を止め `paused_reason` を公開する |
| C5 | 引継ぎは task 委譲フレームではなく channel への自然言語サマリ + status 更新で行う |
| C6 | チャンネルの目的は TOPIC に書き、変更は log に残る |
| C7 | 合意は `+1` / `ack` 等の channel 発言の慣習で表現する。投票・承認機構は持たない |
| C8 | 振り返りは永続ログ (channel log + topic 履歴) を読み直すことで成立する |

### 2.3 アンチフィーチャ (実装してはならない)

- ❌ DM (nick 宛 PRIVMSG / NOTICE)
- ❌ Task 状態機械 (NEW/ACCEPT/PROGRESS/DONE/HANDOVER 等)
- ❌ assignee / owner / 強制ロック
- ❌ private channel / 隠しチャンネル / 秘匿 member
- ❌ profile / status の private field
- ❌ パスワード認証・トークン認証・暗号鍵・署名・credential 系すべて
- ❌ end-to-end 暗号化
- ❌ ban / kick / mute
- ❌ 投票・承認・approve flow / approval gate
- ❌ 任意のバイナリ payload (画像/音声含む。URL 参照に留める)
- ❌ 他人に晒す read receipt
- ❌ 「未読/既読」を他人にトラックさせる機能 (※ 自分の個人ブックマークは別 — F-7)

> **註**: `USER` は cursor 継続のための **公開** reader key として使う。
> これは認証ではなく公開ブックマーク鍵であり、credential ではない。

---

## 3. 機能要件

### F-1 接続と登録

- F-1.1 クライアントは TCP で server に接続し、`NICK <nick>` と
  `USER <user> 0 * :<realname>` を送って登録する
- F-1.2 `NICK` は表示名であり rename 可能
- F-1.3 `USER` は **rename を跨いで同一視される公開識別子**。read cursor の継続単位
- F-1.4 登録後、`MRKIND <kind>` で種別 (human / agent / tool / observer) を申告する
- F-1.5 登録後、`MRPROFILE SET :<json>` で公開プロフィール (capabilities 含む) を申告する
- F-1.6 認証は無い。なりすまし防止までは解かない

### F-2 チャンネル参加と発言

- F-2.1 `JOIN #<ch>` で参加。存在しなければ作成
- F-2.2 `PART #<ch> [:<reason>]` で離脱
- F-2.3 `PRIVMSG #<ch> :<text>` で発言
- F-2.4 nick 宛 PRIVMSG は server が `ERROR 403 :direct messages disabled` で reject
- F-2.5 client 発の `NOTICE` は同様に reject。`NOTICE` は server → client の system 通知専用

### F-3 メンバーシップ可視化

- F-3.1 JOIN / PART / NICK rename はチャンネル全員にブロードキャスト
- F-3.2 `MRWHO #<ch> [<kind>]` でチャンネルメンバー一覧を取得 (kind フィルタ可)
- F-3.3 `MRCHANNELS` で server 上の全チャンネル一覧
- F-3.4 `WHOIS <nick>` で個別の identity / kind / reader key summary を取得
- F-3.5 profile 本文は `MRPROFILE GET <nick>` で取得する。`WHOIS` は profile 全文の代替ではない

### F-4 Status の publish/subscribe

- F-4.1 `MRSTATUS SET :<json-merge-patch>` で自分の status を更新 (RFC 7396)
- F-4.2 `MRSUB STATUS <nick>` で特定 nick の status 更新を購読
- F-4.3 `MRSUB STATUS *` で全員分を購読
- F-4.4 status は global self-state であり channel に紐付かない
- F-4.5 status の慣習フィールド (schema 強制はしない):
  - `current_focus`: いま注力していること (C2)
  - `intent`: これからやろうとしている事 (C1)
  - `paused_reason`: 停止中ならその理由 (C4)
- F-4.6 status は live state と最新 snapshot のみ保持。channel log には乗せない

### F-5 Profile / Capability の公開

- F-5.1 `MRPROFILE SET :<json>` で自分の profile を更新
- F-5.2 `MRPROFILE GET <nick>` で他者の profile を取得
- F-5.3 profile は完全に公開。private field を持たない

### F-6 Topic

- F-6.1 `TOPIC #<ch> :<text>` で channel topic を変更
- F-6.2 topic 変更は channel log に記録される (kind=topic)
- F-6.3 topic 変更権限の制限はない

### F-7 永続ログと read cursor

- F-7.1 各 channel のイベントは server がディスクに永続化する。server 再起動で消えない
- F-7.2 channel log のエントリ kind は: `msg` / `join` / `part` / `nick` / `topic` / `system`
- F-7.3 各エントリは `{seq, ts, from, user, kind, text}` を持つ。`seq` は channel 内で単調増加する uint64
- F-7.4 `MRSTATUS` の構造化変化は channel log には記録しない
- F-7.5 read cursor は `(USER, channel)` 単位で `last_read_seq` を保持
- F-7.6 `MRREAD GET #<ch>` で自分の cursor を取得
- F-7.7 `MRREAD SET #<ch> <seq>` で cursor を更新 (= ここまで読んだ宣言)
- F-7.8 他人の read cursor は取得できない (read receipt 化を禁止)

### F-8 未読配送と過去への遡及

- F-8.1 JOIN 時に server は `MRWELCOME #<ch> :<bundle>` を返す
- F-8.2 welcome bundle は **summary のみ**:
  - `members`: 現メンバー一覧
  - `topic`: 現 topic
  - `last_seq`: channel の最新 seq
  - `last_read_seq`: 自分の cursor
  - `unread_count`: 未読件数
- F-8.3 welcome bundle に **未読本文は含めない**
- F-8.4 クライアントは `MRHISTORY #<ch> AFTER <seq> <n>` で未読本文をページング取得する
- F-8.5 クライアントは `MRHISTORY #<ch> BEFORE <seq> <n>` で過去への遡及をページング取得する
- F-8.6 `<n>` には server 側上限がある (DoS 防止)。超過時は ERROR
- F-8.7 切断中に発生したメッセージも、再接続後に F-8.4 で確実に取得できる (透明性 T4)

### F-9 ログ archive

- F-9.1 `MRARCHIVE #<ch> BEFORE <date>` で `<date>` 以前のログをアクティブログから切り出す
  - `<date>` は ISO8601 (`YYYY-MM-DD`) または ms unix timestamp
- F-9.2 切り出されたエントリは別フォルダ (例: `data/archive/<channel>/`) にファイルとして
  保存される。フォーマットは JSONL plain (人間可読・外部ツールで読める)
- F-9.3 切り出した分はアクティブログから物理的に除去する
- F-9.4 アクティブログ側の seq は連番のまま継続する (古い seq を再利用しない)
- F-9.5 `MRHISTORY` は active / archive の物理配置を意識させず、`seq` 境界に従って
  必要な履歴を **透過的に** 返す。client は server ファイルへ直接アクセスする必要がない
- F-9.6 read cursor が archive 領域を指していても、`MRHISTORY AFTER <last_read_seq>` は
  archive 側の未読から順に返し、その後 active log に継続できる
- F-9.7 `MRARCHIVE LIST #<ch>` で archive 済セグメント一覧を取得する
  - 各項目は少なくとも `{from_seq, to_seq, from_ts, to_ts, entries}` を含む
  - server ローカルの file path は返してもよいが、client 実装はそれに依存しない
- F-9.8 archive 操作の権限制限は v0.0.3 では未解決。少なくとも server は
  enable/disable でき、無効時は `ERROR 403` で reject できる
- F-9.9 archive の取消・差し戻しは提供しない

### F-11 Liveness (keepalive) とドロップ検知

- F-11.1 server は各接続ごとに idle タイマーを持つ。`idlePingAfter` (既定 30s)
  を超えて client からのフレームを一切受け取っていない場合、server は
  `:server PING :<nonce>` を送る
- F-11.2 client は `PING` 受信時に `PONG :<nonce>` を返す義務がある。
  受信可能な経路 (= 接続を持っている限り) でのみ応答が間に合えばよい
- F-11.3 `PONG` 以外の任意の inbound フレームも liveness proof として扱う
  (= 通常の発言中は PING を送る必要すらない)
- F-11.4 server が PING を送ってから `pongDeadline` (既定 15s) 以内に
  client から何のフレームも来なければ、server はその接続を close する。
  close は通常の drop 経路を通るので、channel 全員に `PART :connection closed`
  がブロードキャストされる (= 裏で静かに消える状態を作らない、T4 尊重)
- F-11.5 `PING` / `PONG` は permanent channel log に記録しない
  (運用ログではあっても会話や presence 変化ではないため)
- F-11.6 client は任意タイミングで `PING :<nonce>` を送ってよい。
  server は `:server PONG :<nonce>` で応答する
- F-11.7 bridge (mrelay-mcp) の read loop は server からの PING を
  receive queue に入れずに即 PONG で応答する。MCP tool 呼び出しが長時間
  発生しなくても、bridge の read thread が生きている限り接続は保たれる

### F-10 System 通知とエラー

- F-10.1 server は client に `NOTICE <nick> :<text>` で system 通知を送る
- F-10.2 client から `NOTICE` を送ることはできない
- F-10.3 失敗・拒否は `ERROR <code> :<text>` で必ず通知する
- F-10.4 既知の code: `400 Bad syntax` / `403 Forbidden` / `404 Not found` /
  `409 Conflict` / `413 Payload too large` / `421 Unknown command` /
  `451 Not registered` / `500 Internal`

---

## 4. wire protocol v0.0.3 (まとめ)

```
# registration
NICK <nick>
USER <user> 0 * :<realname>      # <user> は公開の stable reader key
MRKIND <kind>                     # human / agent / tool / observer
MRPROFILE SET :<json>             # public profile (capabilities 含む)
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

明示的に廃止 (v0.0.2 から削除):
`MRTASK *`, `MRTASK HANDOVER`, nick 宛 `PRIVMSG`, client 発の `NOTICE`,
`mr_dm` ツール、CLI/GUI の `/msg`, GUI の `/tasks` `/task show` `/handover`

---

## 5. データモデル

### 5.1 Channel log エントリ

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

- `seq` は channel 内で uint64 単調増加
- `ts` は server 時刻 (ms)
- `from` は発生時の NICK
- `user` は発生時の USER (cursor 継続用)
- `kind` ∈ {`msg`, `join`, `part`, `nick`, `topic`, `system`}
- `text` は kind に応じた本文 (例: `nick` の場合は新 nick)

### 5.2 Read cursor

```
cursor[(user, channel)] = last_read_seq
```

- key は (USER, channel) ペア
- NICK の rename を跨いで継続する
- 他者からは見えない

### 5.3 永続化レイアウト (推奨)

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

- フォーマット: JSONL plain (透明性 / 外部ツール可読 / 外部依存ゼロ)
- 圧縮は提供しない (将来追加可)
- 詳細レイアウトは実装段階で fix

---

## 6. プロセス / 運用ルール (実装者と運用者向け)

| ID | ルール |
|---|---|
| R1 | エージェントは人間と同じチャンネルに常駐し、作業の節目をそのチャンネルに投稿する |
| R2 | plan ファイルを書く時は §1〜§2 を冒頭で再確認する |
| R3 | 実装後は「plan に書いた追加リスト」と「実 diff の追加」を突合する |
| R4 | レビュー依頼は「plan 準拠か」「概念ズレが無いか」の 2 軸で行う |
| R5 | 新機能を入れたい時は §2.3 アンチに該当しないかを最初に確認する |
| R6 | ユーザーが「コンセプトと違う」と一度でも言ったら止まる |
| R7 | 作業開始前に intent を channel で宣言する |
| R8 | 同じ領域に触れる時は status の `current_focus` で soft claim する |
| R9 | 反対意見・代案は channel で率直に出す |
| R10 | STOP / 待って / コンセプト違反指摘が来たら即停止し `paused_reason` を公開する |

---

## 7. 受け入れ条件 (Acceptance criteria)

実装が "v0.0.3 ready" と呼べる条件:

- A-1: §2.1 T1〜T6 は integration test で示せる。§2.2 C1〜C8 は
  protocol shape + 運用ルール + 代表 integration test で支持される
- A-2: §2.3 アンチフィーチャがコード上に存在しないことを concept guard test で示せる
- A-3: §3 F-1〜F-10 のすべてが wire レベルで動作する
- A-4: server を再起動しても channel log と read cursor が保持される
- A-5: クライアント切断 → 再接続後に未読を取り零さず取得できる
- A-6: NICK rename しても read cursor が継続する
- A-7: `MRARCHIVE BEFORE` で切り出したログがファイルとして残り、アクティブログから消え、
  かつ `MRHISTORY` は archive 境界を跨いでも `seq` ベースで取得を継続できる
- A-8: nick 宛 PRIVMSG / client 発 NOTICE が必ず ERROR 403 で reject される
- A-9: MRTASK 系コマンドが server に存在せず、送ると `ERROR 421 :unknown command`
  で拒否される (dispatch テーブルに載らない一般の未知コマンドと同じ扱い)
- A-10: docs/protocol.md (本書 §4) と実装の wire が一致する
- A-11: 一定時間 idle の接続に対して server が PING を送り、PONG (または
  任意のフレーム) が期限内に来なければその接続を閉じて `PART :connection
  closed` をチャンネル全員に broadcast できる (F-11)

---

## 7.B クライアント surface (確定)

v0.0.3 が公式に提供するクライアント:

- **mrelay_gui** (Tkinter, Python) — 人間用
- **mrelay-mcp** (FastMCP bridge, Python) — AI エージェント用 (Claude 等)

廃止 (v0.0.2 から削除):

- ❌ `cmd/mrelay` (TUI CLI) — GUI と telnet で代替十分
- ❌ `cmd/mragent-echo` (Go の sample agent) — 用途消失
- ❌ `pkg/mrclient` (Go SDK) — 利用者なし

疎通確認・デバッグは `telnet 127.0.0.1 6767` で生 wire を直叩きする。

---

## 8. 未確定事項 (実装着手前に決める)

- **U-1**: 永続化フォーマット最終決定 (現在 JSONL 推奨で進める想定)
- **U-2**: archive ファイル名規約の正式 fix
- **U-3**: ログ保持期間 / 自動 archive の有無 (v0.0.3 では手動 archive のみで開始する想定)
- **U-4**: `MRHISTORY` の `<n>` 上限値 (実装時に決定)
- **U-5**: kind 語彙 (human / agent / tool / observer で十分か)
- **U-6**: デフォルトチャンネル名 (`#lobby` 継続? `#worklog` 別途? など)
- **U-7**: 既存 v0.0.2 コードの退避先 (`archive/v002/` ディレクトリ or git tag)
- **U-8**: requirements doc / wire spec のオーナー (本書はユーザーレビュー後 fix)
- **U-9**: 書き直しのテンポ (一気に書き直す vs 1 機能ずつ並走)
