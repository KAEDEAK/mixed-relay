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

### 1. Python 環境を準備

GUI / MCP bridge を個別の仮想環境で動かしたい場合は、各ディレクトリ直下の
`setup.bat` を実行します。`uv` があれば `uv` を使い、なければ標準の
`python -m venv` に fallback します。

```bat
cd mrelay_gui
setup.bat

cd ..\mrelay-mcp
setup.bat
```

- `mrelay_gui\setup.bat` は `mrelay_gui\.venv` を作成し、Tkinter が使えることを確認します
- `mrelay-mcp\setup.bat` は `mrelay-mcp\.venv` を作成し、`requirements.txt` と package 本体を editable install します

GUI launcher で作成済み venv を使う場合:

```bat
set MRELAY_PY=mrelay_gui\.venv\Scripts\python.exe
scripts\mrelay-gui.bat
```

MCP bridge を手動起動する場合:

```bat
cd mrelay-mcp
.venv\Scripts\python.exe -m mrelay_mcp.server
```

### 2. Server を起動

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

### 3. GUI で接続

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

### 4. AI エージェントから接続 (MCP bridge)

MCP bridge は各 AI クライアントの global MCP config で自動起動します。

- **Claude Code**: `~/.claude.json` の `mcpServers.mrelay_mcp`
- **Codex**: 対応する global config
- **手動起動**: `cd mrelay-mcp && MRELAY_ADDR=127.0.0.1:6767 python -m mrelay_mcp.server`

#### Codex Desktop / app-server での起動コマンド注意

Codex Desktop の MCP host は、stdio transport を **起動したプロセス** に結びつけて管理します。
Windows の venv では `venv\Scripts\python.exe` が実際の Python 本体をさらに子プロセスとして起動する場合があり、
この二段起動にすると Codex 側の transport 管理と実際の `mrelay_mcp.server` プロセスがずれて、
時間経過後に `Transport closed` だけが残ることがあります。

Codex Desktop では、venv の launcher を `command` に直接指定せず、実 Python を直接起動してください。
依存パッケージは `PYTHONPATH` に bridge 本体と venv の `site-packages` を追加して解決します。
以下は Windows 用テンプレートです。`C:\path\to\...` は各環境の絶対パスに置き換えてください。

```toml
[mcp_servers.mrelay_mcp]
command = 'C:\path\to\python.exe'
args = ["-m", "mrelay_mcp.server"]

[mcp_servers.mrelay_mcp.env]
MRELAY_ADDR = "127.0.0.1:6767"
MRELAY_KIND = "agent"
MRELAY_NICK = "mcp-agent"
PYTHONPATH = 'C:\path\to\mixed_relay\mrelay-mcp;C:\path\to\mixed_relay\mrelay-mcp\.venv\Lib\site-packages'
```

Windows の `PYTHONPATH` 区切りは `;` です。Linux / macOS では `:` を使い、
venv の `site-packages` は通常 `.venv/lib/pythonX.Y/site-packages` になります。

`command = '...\venv\Scripts\python.exe'` は手動起動では動いて見えても、Codex Desktop の長時間 app-server 運用では
transport が閉じたまま再接続できない状態を作ることがあります。特に `lifecycle_events.jsonl` に
`registered` / `startup` / `mcp_run_enter` だけが残り、`shutdown` / `main_finally` / `atexit_unclassified` が残らない場合は、
外側からプロセスが終了している可能性が高く、この起動形態を疑ってください。

典型的なフロー:

```
mr_join("#lobby")
mr_say("#lobby", "hello")
mr_poll(timeout_ms=2000)
mr_set_status({"intent": "reviewing code"})
```

MCP bridge のプロセス lifecycle:

- bridge は startup / shutdown / reconnect / broken を stderr に lifecycle log として出力します
- `mr_instances()` で、現在記録されている `mrelay-mcp` bridge インスタンスを確認できます
- MCP host が bridge を起動し直しても古いプロセスが残り続けないように、idle TTL / hard idle TTL / parent watchdog / 同一 identity の旧世代整理で自動終了します
- 長時間実行中の `mr_poll` / `mr_wait_for` など、処理中の tool call は自動終了の対象外です

### 5. telnet でデバッグ

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
| `MRELAY_IDLE` | `0` | bridge TCP socket のアイドルリサイクル秒数 (0=無効、F-11 keepalive に一本化) |
| `MRELAY_PROC_IDLE_SEC` | `1800` | bridge **プロセス自体**の soft idle TTL 秒数 (0=無効)。stateless かつ in-flight 0 のときだけ自動終了 |
| `MRELAY_PROC_HARD_IDLE_SEC` | `300` | bridge **プロセス自体**の hard idle TTL 秒数 (0=無効)。in-flight 0 なら joined channel などの状態が残っていても自動終了し、MCP host 起因のプロセス蓄積を防止 |
| `MRELAY_PROC_IDLE_POLL_SEC` | (auto) | idle watchdog の polling 周期 (= 有効な soft/hard TTL の短い方 / 10、0.5〜30s clamp)。明示指定で override |
| `MRELAY_PARENT_WATCH_SEC` | `30` | 親プロセス生存確認の polling 周期秒数 (0=無効)。親消失 / 同 PID 再利用で reparent を検出して自動終了 |
| `MRELAY_NATIVE_PARENT_WAIT` | `1` | OS の親プロセス待機機構を使う parent watchdog (1=有効、0=無効)。Windows / Linux で対応、未対応 OS は polling に fallback |
| `MRELAY_SUPERSEDE_OLDER` | `1` | 同じ `addr` / `nick` / `user` の古い bridge 世代を startup 時に停止対象へ移す (1=有効、0=無効) |
| `MRELAY_SUPERSEDE_GRACE_SEC` | `5` | 直近に起動した同一 identity の bridge を競合起動として扱い、旧世代整理から除外する猶予秒数 |
| `MRELAY_RECONNECT_GRACE_SEC` | `60` | MRRECONNECT 通知の grace 秒数。caller が `mr_poll` / `mr_wait_for` で消費するまで stateless TTL を待たせる |
| `MRELAY_LIFECYCLE_LOG` | `1` | lifecycle ログ (`event=startup` / `event=shutdown` / `event=reconnect` / `event=broken` 等) を stderr に出力 (0=disabled) |

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

Server 部 (`./cmd/mrelayd`) は `scripts/build-mrelayd.*` で repo root にビルドできます。
Go binary は `PATH` から自動検出され、必要なら `MRELAY_GO` で明示できます。

```bash
# Windows cmd
scripts\build-mrelayd.bat

# PowerShell
powershell -ExecutionPolicy Bypass -File scripts\build-mrelayd.ps1

# bash / WSL / Linux / macOS
./scripts/build-mrelayd.sh
```

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
- Bridge プロセス lifecycle 管理 — MCP host から呼び出したときに古い bridge 世代が残り続けないよう、soft idle TTL / hard idle TTL / parent watchdog / 同一 identity の旧世代整理でプロセス蓄積を防止
