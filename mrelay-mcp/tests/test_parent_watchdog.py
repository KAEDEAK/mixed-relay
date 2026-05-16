"""Parent watchdog branches.

We drive ``_parent_loop`` directly with a fake ``psutil.Process`` so we
cover the four exit cases (alive / not-running / NoSuchProcess /
reparent) deterministically. The loop body itself sets the stop event
inside ``_alive`` so the test does not hang when no exit fires.
"""

from __future__ import annotations

import psutil
import pytest

from mrelay_mcp import lifecycle


class _Cfg:
    addr = "127.0.0.1:6767"
    nick = "test-nick"


def _build_ctx(monkeypatch):
    monkeypatch.setenv("MRELAY_PROC_IDLE_SEC", "0")
    monkeypatch.setenv("MRELAY_PARENT_WATCH_SEC", "0")
    monkeypatch.setenv("MRELAY_LIFECYCLE_LOG", "0")
    ctx = lifecycle.LifecycleContext(_Cfg(), is_stateless_provider=lambda: True)
    # We will run the loop body directly. Short poll so the wait() at
    # the top is effectively instant.
    ctx._parent_watch_sec = 0.01
    ctx._initial_ppid_create_time = 1.0
    return ctx


def _patch_os_exit(monkeypatch):
    """Raise SystemExit instead of really killing the process when the
    watchdog calls os._exit. Returns a list that captures the codes."""
    calls = []

    def fake_exit(code):
        calls.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(lifecycle.os, "_exit", fake_exit)
    return calls


def test_parent_gone_triggers_exit(monkeypatch):
    ctx = _build_ctx(monkeypatch)
    calls = _patch_os_exit(monkeypatch)

    class _DeadProc:
        def is_running(self):
            return False

        def status(self):
            return "dead"

        def create_time(self):
            return 1.0

    monkeypatch.setattr(psutil, "Process", lambda pid: _DeadProc())
    with pytest.raises(SystemExit):
        ctx._parent_loop()
    assert calls == [0]


def test_reparent_detected_via_create_time(monkeypatch):
    ctx = _build_ctx(monkeypatch)
    calls = _patch_os_exit(monkeypatch)

    class _Reparented:
        def is_running(self):
            return True

        def status(self):
            return "running"

        def create_time(self):
            return 2.0  # differs from ctx._initial_ppid_create_time

    monkeypatch.setattr(psutil, "Process", lambda pid: _Reparented())
    with pytest.raises(SystemExit):
        ctx._parent_loop()
    assert calls == [0]


def test_no_such_process_triggers_exit(monkeypatch):
    ctx = _build_ctx(monkeypatch)
    calls = _patch_os_exit(monkeypatch)

    def raise_nsp(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(psutil, "Process", raise_nsp)
    with pytest.raises(SystemExit):
        ctx._parent_loop()
    assert calls == [0]


def test_alive_parent_does_not_exit(monkeypatch):
    ctx = _build_ctx(monkeypatch)

    def explode(code):
        raise AssertionError(f"unexpected exit {code}")

    monkeypatch.setattr(lifecycle.os, "_exit", explode)

    class _Alive:
        def is_running(self):
            return True

        def status(self):
            return "running"

        def create_time(self):
            return 1.0

    # Stop the loop after the first observation so the test terminates
    # cleanly without an exit firing.
    seen = {"count": 0}

    def _proc(pid):
        seen["count"] += 1
        if seen["count"] >= 1:
            ctx._stop_event.set()
        return _Alive()

    monkeypatch.setattr(psutil, "Process", _proc)
    ctx._parent_loop()  # must return cleanly without exiting
    assert seen["count"] >= 1
