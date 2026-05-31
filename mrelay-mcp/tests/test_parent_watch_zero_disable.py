"""``MRELAY_PARENT_WATCH_SEC=0`` must keep the *polling* parent
watchdog from ever starting, and ``MRELAY_NATIVE_PARENT_WAIT=0`` must
do the same for the native one. Companion to
``test_idle_ttl_zero_disable.py``.
"""

from __future__ import annotations

from mrelay_mcp import lifecycle


class _Cfg:
    addr = "127.0.0.1:6767"
    nick = "test-nick"


def _quiet_env(monkeypatch):
    monkeypatch.setenv("MRELAY_PROC_IDLE_SEC", "0")
    monkeypatch.setenv("MRELAY_PROC_HARD_IDLE_SEC", "0")
    monkeypatch.setenv("MRELAY_SUPERSEDE_OLDER", "0")
    monkeypatch.setenv("MRELAY_LIFECYCLE_LOG", "0")


def test_zero_parent_watch_does_not_start_thread(monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setenv("MRELAY_PARENT_WATCH_SEC", "0")
    monkeypatch.setenv("MRELAY_NATIVE_PARENT_WAIT", "0")
    ctx = lifecycle.startup(_Cfg(), is_stateless_provider=lambda: True)
    assert ctx._parent_thread is None
    assert ctx._native_parent_thread is None


def test_native_wait_disabled_keeps_native_thread_off(monkeypatch):
    """Polling watchdog is on but native wait is off — only the
    polling thread spins up. Pins the independent gates."""
    _quiet_env(monkeypatch)
    monkeypatch.setenv("MRELAY_PARENT_WATCH_SEC", "1")
    monkeypatch.setenv("MRELAY_NATIVE_PARENT_WAIT", "0")
    ctx = lifecycle.startup(_Cfg(), is_stateless_provider=lambda: True)
    # _parent_thread spins up only when psutil could read the current
    # parent's create_time. On a CI runner that always succeeds, but
    # we still allow the None outcome so this test does not become
    # flaky on locked-down sandboxes.
    if ctx._initial_ppid_create_time is not None:
        assert ctx._parent_thread is not None
    assert ctx._native_parent_thread is None
