"""Shared instance registry for mrelay-mcp.

Pins the cross-instance JSON bookkeeping so stale PID rows get pruned on
startup and on ``mr_instances`` reads, while the current process is
registered and later marked stopped on shutdown.
"""

from __future__ import annotations

import json
import os

from mrelay_mcp import lifecycle, server


class _Cfg:
    addr = "127.0.0.1:6767"
    nick = "registry-test"
    user = "registry-user"
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


def _disable_watchdogs(monkeypatch):
    monkeypatch.setenv("MRELAY_PROC_IDLE_SEC", "0")
    monkeypatch.setenv("MRELAY_PROC_HARD_IDLE_SEC", "0")
    monkeypatch.setenv("MRELAY_PARENT_WATCH_SEC", "0")
    monkeypatch.setenv("MRELAY_NATIVE_PARENT_WAIT", "0")
    # Registry tests place hand-crafted rows with arbitrary PIDs into
    # the registry. Supersession is OFF here so a fake "running" row
    # cannot trigger a real psutil.terminate() against an unrelated
    # OS process that happens to be alive at that PID.
    monkeypatch.setenv("MRELAY_SUPERSEDE_OLDER", "0")


def test_startup_registers_current_instance(monkeypatch, tmp_path):
    _configure_registry(monkeypatch, tmp_path)
    _disable_watchdogs(monkeypatch)

    lifecycle.startup(_Cfg(), is_stateless_provider=lambda: True)

    instances = lifecycle.list_instances()
    assert len(instances) == 1
    entry = instances[0]
    assert entry["pid"] == os.getpid()
    assert entry["status"] == "running"
    assert entry["nick"] == "registry-test"
    assert entry["elapsed_sec"] >= 0

    raw = json.loads((tmp_path / "instance_registry.json").read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert len(raw["instances"]) == 1
    assert raw["instances"][0]["pid"] == os.getpid()


def test_shutdown_marks_current_instance_stopped(monkeypatch, tmp_path):
    _configure_registry(monkeypatch, tmp_path)
    _disable_watchdogs(monkeypatch)

    lifecycle.startup(_Cfg(), is_stateless_provider=lambda: True)
    assert lifecycle.shutdown("normal") is True

    raw = json.loads((tmp_path / "instance_registry.json").read_text(encoding="utf-8"))
    assert len(raw["instances"]) == 1
    entry = raw["instances"][0]
    assert entry["pid"] == os.getpid()
    assert entry["status"] == "stopped"
    assert entry["shutdown_reason"] == "normal"
    assert entry["ended_at"]


def test_list_instances_prunes_dead_rows(monkeypatch, tmp_path):
    _configure_registry(monkeypatch, tmp_path)

    dead_pid = 999999
    doc = {
        "version": 1,
        "instances": [
            {
                "pid": dead_pid,
                "ppid": 1,
                "pid_create_time": 123.0,
                "started_at": "2026-05-18T00:00:00Z",
                "started_ts": 0.0,
                "status": "running",
                "addr": "127.0.0.1:6767",
                "nick": "dead",
                "user": "dead",
                "kind": "agent",
            }
        ],
    }
    (tmp_path / "instance_registry.json").write_text(
        json.dumps(doc),
        encoding="utf-8",
    )

    instances = lifecycle.list_instances()
    assert instances == []

    raw = json.loads((tmp_path / "instance_registry.json").read_text(encoding="utf-8"))
    assert raw["instances"] == []


def test_mr_instances_returns_registry_snapshot(monkeypatch):
    sample = [{"pid": 12, "elapsed_sec": 34, "status": "running"}]
    monkeypatch.setattr(lifecycle, "list_instances", lambda: sample)
    monkeypatch.setattr(
        lifecycle,
        "_REGISTRY_PATH",
        lifecycle.Path("D:/tmp/instance_registry.json"),
    )

    out = server.mr_instances()

    assert out["ok"] is True
    assert out["instances"] == sample
    assert out["registry_path"].endswith("instance_registry.json")
