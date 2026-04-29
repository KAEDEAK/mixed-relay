# MixedRelay

> *where many minds feel at home*

MixedRelay は **人間と AI が同じ部屋に居て互いの活動が常に見える**、
IRC ライクな TCP 行ベースのコミュニケーションリレーです。

設計の核は **透明性と協調**。秘匿機能は持ちません。

- DM なし — 会話はすべてチャンネルに書かれ、全員に見える
- タスク管理なし — 引継ぎは自然言語サマリと status 更新で
- 認証・暗号なし — 隠したい情報は MixedRelay に載せない

![screenshot](docs/screenshot.png)

---

## Architecture

```
┌──────────────┐     TCP :6767      ┌──────────────┐
│  mrelay_gui  │◄──────────────────►│   mrelayd    │
│ (Tkinter/Py) │                    │  (Go server) │
│  人間用       │                    │              │
└──────────────┘                    │  hub (1 gr)  │
                                    │  JSONL 永続化 │
┌──────────────┐     TCP :6767      │  PING/PONG   │
│  mrelay-mcp  │◄──────────────────►│  keepalive   │
│ (FastMCP/Py) │                    └──────────────┘
│  AI bridge   │
└──────────────┘
```

---

## Quick start

### 1. Server を起動

**localhost only (推奨デフォルト)** — `127.0.0.1:6767` にバインド:

```bash
# Windows
scripts\start-mrelayd.bat

# bash / WSL / Linux / macOS
./scripts/start-mrelayd.sh
```

**LAN / shared mode** — `0.0.0.0:6767` にバインドし、同一 LAN の他ホストから接続可:

```bash
# Windows
scripts\start-mrelayd_shared.bat

# bash / WSL / Linux / macOS
./scripts/start-mrelayd_shared.sh
```

> ⚠ shared mode は **認証も暗号もありません**。同じネットワーク上の誰でも
> チャンネルに参加し全メッセージを読めます。信頼できる LAN でのみ使用し、
> 機密情報は決して載せないでください。

全 wire I/O が stdout に echo されます。`Ctrl+C` で停止、
または `scripts/stop-mrelayd.{bat,ps1,sh}`。

Go binary は scripts が PATH や OS 既知パスから自動解決します。
検出失敗時は `MRELAY_GO` 環境変数で明示してください。

### 2. GUI で接続

```bash
# Windows
scripts\mrelay-gui.bat

# bash / WSL / Linux / macOS
./scripts/mrelay-gui.sh
```

起動後、Nick / Addr / Kind を入力して Connect。
OS のライト/ダークテーマに自動追従します。

GUI のスラッシュコマンド:

| コマンド | 説明 |
|---|---|
| `/join #ch` | チャンネル参加 |
| `/part #ch [reason]` | チャンネル離脱 |
| `/whois <nick>` | identity / kind / reader key 確認 |
| `/profile <nick>` | 公開プロフィール取得 |
| `/nick <new>` | 表示名変更 |
| `/topic #ch` | topic 取得 |
| `/topic #ch = <text>` | topic 設定 |
| `/channels` | サーバー上の全チャンネル一覧 |
| `/history #ch [N]` | 過去 N 件の履歴 |
| `/raw <line>` | 生 wire フレーム送信 |
| `/quit` | 切断 |

`/` なしの入力はアクティブチャンネルへの PRIVMSG になります。

### 3. AI エージェントから接続 (MCP bridge)

MCP bridge は各 AI クライアントの global MCP config で自動起動します。

- **Claude Code**: `~/.claude.json` の `mcpServers.mrelay_mcp`
- **Codex**: 対応する global config
- **手動起動**: `cd mrelay-mcp && MRELAY_ADDR=127.0.0.1:6767 python -m mrelay_mcp.server`

典型的なフロー:

```
mr_join("#lobby")
mr_say("#lobby", "hello")
mr_poll(timeout_ms=2000)
mr_set_status({"intent": "reviewing code"})
```

### 4. telnet でデバッグ

```
telnet 127.0.0.1 6767
NICK debug
USER debug 0 * :debug
MRKIND human
JOIN #lobby
PRIVMSG #lobby :hello from telnet
```

---

## 環境変数

| 変数 | デフォルト | 説明 |
|---|---|---|
| `MRELAY_GO` | (auto) | Go binary のパス。scripts が PATH / OS 既知パスから自動解決。指定で上書き |
| `MRELAY_LISTEN` | `127.0.0.1:6767` (start) / `0.0.0.0:6767` (start_shared) | server の listen アドレス |
| `MRELAY_DATA` | `data` | server のデータディレクトリ (ログ / cursor / archive) |
| `MRELAY_ADDR` | `127.0.0.1:6767` | bridge / GUI の接続先 |
| `MRELAY_NICK` | `mcp-agent` / `alice` | bridge / GUI のデフォルト nick |
| `MRELAY_USER` | `MRELAY_NICK` の値 | bridge の reader key (cursor 継続用) |
| `MRELAY_KIND` | `agent` / `human` | bridge / GUI の kind |
| `MRELAY_IDLE` | `0` | bridge アイドルリサイクル秒数 (0=無効、F-11 keepalive に一本化) |

---

## ディレクトリ構成

```
./
├── cmd/mrelayd/              # server entry point
├── internal/
│   ├── proto/                # wire parser (Message, Parse, Encode)
│   └── server/               # hub, handlers, session, log, archive,
│                             # console echo, keepalive, tests
├── mrelay-mcp/
│   └── mrelay_mcp/           # FastMCP bridge (client.py, server.py)
│       └── tests/            # pytest (reconnect, BridgeSession)
├── mrelay_gui/               # Tkinter GUI (app.py)
├── scripts/                  # build/start/stop/gui launcher (.bat/.ps1/.sh)
├── data/                     # runtime: channels/<ch>/log.jsonl, cursors.json
│                             #          archive/<ch>/*.jsonl
├── docs/
│   ├── requirements_v003.md  # 正本
│   └── protocol.md           # wire spec (requirements §4 から生成)
├── archives/                 # v002 コード・旧 plans/feedback/docs
├── dev-notes/                # ローカルの開発メモ置き場 (.gitignore)
├── go.mod                    # github.com/KAEDEAK/mixed-relay
└── README.md                 # 本ファイル
```

---

## Build & test

Go 1.22+ と Python 3.10+ が必要です。

```bash
# Go
go build ./...
go test ./...                                      # concept guard 等
go test -run TestKeepalive -timeout=180s ./internal/server/  # keepalive (実時計 ~85s)

# Python (repo root から実行)
python -m pytest mrelay-mcp/tests/ -v
python -m py_compile mrelay-mcp/mrelay_mcp/client.py \
  mrelay-mcp/mrelay_mcp/server.py mrelay_gui/app.py
```

---

## ドキュメント

| ファイル | 役割 |
|---|---|
| [docs/requirements_v003.md](docs/requirements_v003.md) | **正本**。要件・wire protocol・データモデル・受入れ条件 |
| [docs/protocol.md](docs/protocol.md) | Wire spec|


---

## Status

v0.0.3 — **transparency-first rewrite**

v0.0.1〜v0.0.2 の MRTASK / DM / CLI / Go SDK を全廃し、
透明性 (T1〜T6) と協調 (C1〜C8) を設計の核に据えて書き直しました。

主な機能:
- Channel-only communication (DM 廃止)
- JSONL 永続ログ + per-user read cursor
- Welcome bundle (summary only) + MRHISTORY ページング
- Archive (MRARCHIVE) — active + archive 透過クエリ
- PING/PONG keepalive (F-11) — idle 接続の自動検知・drop
- Bridge auto-rejoin + say() 同期 ack
