"""Equal-identity supersession at startup.

A new ``mrelay-mcp`` generation rewrites any earlier ``(addr, nick,
user)`` row in the shared registry from ``running`` to ``stopped``
(``shutdown_reason=superseded_by_pid=<my pid>``) and returns the
predecessor PIDs to the caller for OS-level termination.
"""

from __future__ import annotations

import json
import time

import psutil

from mrelay_mcp import lifecycle


class _Cfg:
    addr = "127.0.0.1:6767"
    nick = "rival"
    user = "rival"
    kind = "agent"


def _configure_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(
        lifecycle, "_REGISTRY_PATH", tmp_path / "instance_registry.json"
    )
    monkeypatch.setattr(
        lifecycle, "_REGISTRY_LOCK_PATH", tmp_path / "instance_registry.lock"
    )


def _quiet_env(monkeypatch):
    monkeypatch.setattr(lifecycle, "_detect_host_kind", lambda ppid: "unknown")
    monkeypatch.setenv("MRELAY_PROC_IDLE_SEC", "0")
    monkeypatch.setenv("MRELAY_PROC_HARD_IDLE_SEC", "0")
    monkeypatch.setenv("MRELAY_PARENT_WATCH_SEC", "0")
    monkeypatch.setenv("MRELAY_NATIVE_PARENT_WAIT", "0")
    monkeypatch.setenv("MRELAY_LIFECYCLE_LOG", "0")


def _force_keep_seeded_rows(monkeypatch):
    """Stop ``_prune_dead_instances`` from dropping seeded rows that
    use mismatched ``pid_create_time``."""
    monkeypatch.setattr(
        lifecycle,
        "_prune_dead_instances",
        lambda instances: [e for e in instances if isinstance(e, dict)],
    )


def _seed_registry(tmp_path, rows):
    doc = {"version": 1, "instances": rows}
    (tmp_path / "instance_registry.json").write_text(
        json.dumps(doc), encoding="utf-8"
    )


def _alive_pid_with_create_time():
    p = psutil.Process()
    return p.pid, p.create_time()


def _patch_fake_terminate(monkeypatch):
    """Replace _terminate_superseded so the test does not signal the
    seeded PID (which is the pytest process itself)."""
    captured = {"pids": []}

    def fake_terminate(self, pids):
        captured["pids"] = list(pids)

    monkeypatch.setattr(
        lifecycle.LifecycleContext, "_terminate_superseded", fake_terminate
    )
    return captured


def test_supersede_marks_old_row_stopped_and_returns_pid(monkeypatch, tmp_path):
    _configure_registry(monkeypatch, tmp_path)
    _quiet_env(monkeypatch)
    _force_keep_seeded_rows(monkeypatch)
    monkeypatch.setenv("MRELAY_SUPERSEDE_OLDER", "1")
    monkeypatch.setenv("MRELAY_SUPERSEDE_GRACE_SEC", "0")
    captured = _patch_fake_terminate(monkeypatch)

    alive_pid, ctime = _alive_pid_with_create_time()
    _seed_registry(
        tmp_path,
        [
            {
                "pid": alive_pid,
                "ppid": 1,
                "pid_create_time": ctime - 1000.0,
                "started_at": "2026-05-01T00:00:00Z",
                "started_ts": 0.0,
                "status": "running",
                "addr": "127.0.0.1:6767",
                "nick": "rival",
                "user": "rival",
                "kind": "agent",
            }
        ],
    )

    lifecycle.startup(_Cfg(), is_stateless_provider=lambda: True)

    raw = json.loads(
        (tmp_path / "instance_registry.json").read_text(encoding="utf-8")
    )
    matched = [r for r in raw["instances"] if r.get("started_ts") == 0.0]
    assert len(matched) == 1, f"seeded row missing: {raw['instances']!r}"
    old = matched[0]
    assert old["status"] == "stopped", f"got: {old!r}"
    assert old["shutdown_reason"].startswith("superseded_by_pid=")
    assert old["ended_at"]
    assert captured["pids"] == [alive_pid]


def test_supersede_skips_different_nick(monkeypatch, tmp_path):
    _configure_registry(monkeypatch, tmp_path)
    _quiet_env(monkeypatch)
    _force_keep_seeded_rows(monkeypatch)
    monkeypatch.setenv("MRELAY_SUPERSEDE_OLDER", "1")
    monkeypatch.setenv("MRELAY_SUPERSEDE_GRACE_SEC", "0")
    captured = _patch_fake_terminate(monkeypatch)

    alive_pid, ctime = _alive_pid_with_create_time()
    _seed_registry(
        tmp_path,
        [
            {
                "pid": alive_pid,
                "ppid": 1,
                "pid_create_time": ctime - 1000.0,
                "started_at": "2026-05-01T00:00:00Z",
                "started_ts": 0.0,
                "status": "running",
                "addr": "127.0.0.1:6767",
                "nick": "different-client",
                "user": "different-client",
                "kind": "agent",
            }
        ],
    )

    lifecycle.startup(_Cfg(), is_stateless_provider=lambda: True)

    raw = json.loads(
        (tmp_path / "instance_registry.json").read_text(encoding="utf-8")
    )
    matched = [r for r in raw["instances"] if r.get("nick") == "different-client"]
    assert len(matched) == 1
    assert matched[0]["status"] == "running", "different nick must be left alone"
    assert captured["pids"] == []


def test_supersede_respects_grace_window(monkeypatch, tmp_path):
    """A predecessor that just registered (within grace_sec) is
    treated as a racing peer, not a leaked generation."""
    _configure_registry(monkeypatch, tmp_path)
    _quiet_env(monkeypatch)
    _force_keep_seeded_rows(monkeypatch)
    monkeypatch.setenv("MRELAY_SUPERSEDE_OLDER", "1")
    monkeypatch.setenv("MRELAY_SUPERSEDE_GRACE_SEC", "3600")  # huge grace
    captured = _patch_fake_terminate(monkeypatch)

    alive_pid, ctime = _alive_pid_with_create_time()
    _seed_registry(
        tmp_path,
        [
            {
                "pid": alive_pid,
                "ppid": 1,
                "pid_create_time": ctime - 1000.0,
                "started_at": "2026-05-01T00:00:00Z",
                "started_ts": time.time(),  # right now -> inside grace
                "status": "running",
                "addr": "127.0.0.1:6767",
                "nick": "rival",
                "user": "rival",
                "kind": "agent",
            }
        ],
    )

    lifecycle.startup(_Cfg(), is_stateless_provider=lambda: True)

    raw = json.loads(
        (tmp_path / "instance_registry.json").read_text(encoding="utf-8")
    )
    # Two rows now: the seeded one (still running, inside grace) and
    # our own running row.
    running_rivals = [
        r for r in raw["instances"]
        if r.get("nick") == "rival" and r.get("status") == "running"
    ]
    assert len(running_rivals) >= 1
    assert captured["pids"] == []


def test_supersede_disabled_leaves_old_row_running(monkeypatch, tmp_path):
    _configure_registry(monkeypatch, tmp_path)
    _quiet_env(monkeypatch)
    _force_keep_seeded_rows(monkeypatch)
    monkeypatch.setenv("MRELAY_SUPERSEDE_OLDER", "0")

    alive_pid, ctime = _alive_pid_with_create_time()
    _seed_registry(
        tmp_path,
        [
            {
                "pid": alive_pid,
                "ppid": 1,
                "pid_create_time": ctime - 1000.0,
                "started_at": "2026-05-01T00:00:00Z",
                "started_ts": 0.0,
                "status": "running",
                "addr": "127.0.0.1:6767",
                "nick": "rival",
                "user": "rival",
                "kind": "agent",
            }
        ],
    )

    lifecycle.startup(_Cfg(), is_stateless_provider=lambda: True)

    raw = json.loads(
        (tmp_path / "instance_registry.json").read_text(encoding="utf-8")
    )
    matched = [r for r in raw["instances"] if r.get("started_ts") == 0.0]
    assert len(matched) == 1
    assert matched[0]["status"] == "running"
