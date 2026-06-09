"""Codex Desktop app-server lifecycle guards.

The app-server keeps MCP stdio children as long-lived transports. Killing
that child on idle, or superseding it from another same-nick launch, leaves
Codex with a closed tool handle instead of a clean replacement process.
"""

from __future__ import annotations

import json

import psutil

from mrelay_mcp import lifecycle


class _Cfg:
    addr = "127.0.0.1:6767"
    nick = "codex"
    user = "codex"
    kind = "agent"


def _configure_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(
        lifecycle,
        "_REGISTRY_PATH",
        tmp_path / "instance_registry.json",
    )
    monkeypatch.setattr(
        lifecycle,
        "_REGISTRY_LOCK_PATH",
        tmp_path / "instance_registry.lock",
    )


def _quiet_env(monkeypatch):
    monkeypatch.setenv("MRELAY_PROC_IDLE_SEC", "0")
    monkeypatch.setenv("MRELAY_PARENT_WATCH_SEC", "0")
    monkeypatch.setenv("MRELAY_NATIVE_PARENT_WAIT", "0")
    monkeypatch.setenv("MRELAY_SUPERSEDE_OLDER", "1")
    monkeypatch.setenv("MRELAY_SUPERSEDE_GRACE_SEC", "0")
    monkeypatch.setenv("MRELAY_LIFECYCLE_LOG", "0")


def _force_keep_seeded_rows(monkeypatch):
    monkeypatch.setattr(
        lifecycle,
        "_prune_dead_instances",
        lambda instances: [e for e in instances if isinstance(e, dict)],
    )


def _seed_app_server_row(tmp_path):
    proc = psutil.Process()
    row = {
        "identity_version": 2,
        "pid": proc.pid,
        "ppid": 1,
        "pid_create_time": proc.create_time() - 1000.0,
        "started_at": "2026-06-08T00:00:00Z",
        "started_ts": 0.0,
        "status": "running",
        "addr": "127.0.0.1:6767",
        "nick": "codex",
        "user": "codex",
        "kind": "agent",
        "host_kind": "codex-app-server",
        "script_file": str(
            lifecycle.Path(lifecycle.__file__).with_name("server.py").resolve()
        ),
    }
    (tmp_path / "instance_registry.json").write_text(
        json.dumps({"version": 2, "instances": [row]}),
        encoding="utf-8",
    )
    return proc.pid


def test_codex_app_server_default_hard_idle_is_disabled(monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.delenv("MRELAY_PROC_HARD_IDLE_SEC", raising=False)
    monkeypatch.setattr(
        lifecycle, "_detect_host_kind", lambda ppid: "codex-app-server"
    )

    ctx = lifecycle.LifecycleContext(_Cfg(), is_stateless_provider=lambda: False)

    assert ctx._hard_idle_sec == 0


def test_codex_app_server_explicit_hard_idle_is_respected(monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setenv("MRELAY_PROC_HARD_IDLE_SEC", "7")
    monkeypatch.setattr(
        lifecycle, "_detect_host_kind", lambda ppid: "codex-app-server"
    )

    ctx = lifecycle.LifecycleContext(_Cfg(), is_stateless_provider=lambda: False)

    assert ctx._hard_idle_sec == 7


def test_codex_app_server_parent_watchdogs_are_disabled_by_default(monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.delenv("MRELAY_PARENT_WATCH_SEC", raising=False)
    monkeypatch.delenv("MRELAY_NATIVE_PARENT_WAIT", raising=False)
    monkeypatch.setattr(
        lifecycle, "_detect_host_kind", lambda ppid: "codex-app-server"
    )

    ctx = lifecycle.LifecycleContext(_Cfg(), is_stateless_provider=lambda: False)

    assert ctx._parent_watch_sec == 0
    assert ctx._native_parent_wait_enabled is False


def test_codex_app_server_explicit_parent_watchdogs_are_respected(monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setenv("MRELAY_PARENT_WATCH_SEC", "9")
    monkeypatch.setenv("MRELAY_NATIVE_PARENT_WAIT", "1")
    monkeypatch.setattr(
        lifecycle, "_detect_host_kind", lambda ppid: "codex-app-server"
    )

    ctx = lifecycle.LifecycleContext(_Cfg(), is_stateless_provider=lambda: False)

    assert ctx._parent_watch_sec == 9
    assert ctx._native_parent_wait_enabled is True


def test_lifecycle_event_log_records_startup_shutdown(
    monkeypatch, tmp_path
):
    _quiet_env(monkeypatch)
    event_log = tmp_path / "lifecycle_events.jsonl"
    monkeypatch.setenv("MRELAY_LIFECYCLE_EVENT_LOG", str(event_log))
    monkeypatch.setenv("MRELAY_LIFECYCLE_FILE_LOG", "1")
    monkeypatch.setattr(
        lifecycle, "_detect_host_kind", lambda ppid: "codex-app-server"
    )

    ctx = lifecycle.LifecycleContext(_Cfg(), is_stateless_provider=lambda: False)
    ctx._emit_startup()
    ctx.shutdown("normal")

    events = [
        json.loads(line)["event"]
        for line in event_log.read_text(encoding="utf-8").splitlines()
    ]
    assert events == ["startup", "shutdown"]


def test_codex_app_server_does_not_supersede_by_default(monkeypatch, tmp_path):
    _configure_registry(monkeypatch, tmp_path)
    _quiet_env(monkeypatch)
    _force_keep_seeded_rows(monkeypatch)
    monkeypatch.delenv("MRELAY_CODEX_APP_SERVER_SUPERSEDE", raising=False)
    monkeypatch.setenv("MRELAY_PROC_HARD_IDLE_SEC", "0")
    monkeypatch.setattr(
        lifecycle, "_detect_host_kind", lambda ppid: "codex-app-server"
    )
    captured = {"pids": []}
    monkeypatch.setattr(
        lifecycle.LifecycleContext,
        "_terminate_superseded",
        lambda self, pids: captured["pids"].extend(pids),
    )
    _seed_app_server_row(tmp_path)

    lifecycle.startup(_Cfg(), is_stateless_provider=lambda: True)

    raw = json.loads(
        (tmp_path / "instance_registry.json").read_text(encoding="utf-8")
    )
    old_rows = [row for row in raw["instances"] if row.get("started_ts") == 0.0]
    assert len(old_rows) == 1
    assert old_rows[0]["status"] == "running"
    assert captured["pids"] == []


def test_codex_exec_does_not_supersede_app_server(monkeypatch, tmp_path):
    _configure_registry(monkeypatch, tmp_path)
    _quiet_env(monkeypatch)
    _force_keep_seeded_rows(monkeypatch)
    monkeypatch.setenv("MRELAY_PROC_HARD_IDLE_SEC", "0")
    monkeypatch.setattr(lifecycle, "_detect_host_kind", lambda ppid: "codex-exec")
    captured = {"pids": []}
    monkeypatch.setattr(
        lifecycle.LifecycleContext,
        "_terminate_superseded",
        lambda self, pids: captured["pids"].extend(pids),
    )
    _seed_app_server_row(tmp_path)

    lifecycle.startup(_Cfg(), is_stateless_provider=lambda: True)

    raw = json.loads(
        (tmp_path / "instance_registry.json").read_text(encoding="utf-8")
    )
    old_rows = [row for row in raw["instances"] if row.get("started_ts") == 0.0]
    assert len(old_rows) == 1
    assert old_rows[0]["status"] == "running"
    assert captured["pids"] == []
