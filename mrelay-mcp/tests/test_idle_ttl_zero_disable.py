"""``MRELAY_PROC_IDLE_SEC=0`` must keep the idle watchdog thread from
ever starting. Belt and suspenders for the disable contract.
"""

from __future__ import annotations

from mrelay_mcp import lifecycle


class _Cfg:
    addr = "127.0.0.1:6767"
    nick = "test-nick"


def test_zero_ttl_does_not_start_idle_thread(monkeypatch):
    monkeypatch.setenv("MRELAY_PROC_IDLE_SEC", "0")
    monkeypatch.setenv("MRELAY_PARENT_WATCH_SEC", "0")
    monkeypatch.setenv("MRELAY_LIFECYCLE_LOG", "0")
    ctx = lifecycle.startup(_Cfg(), is_stateless_provider=lambda: True)
    assert ctx._idle_thread is None


def test_positive_ttl_starts_idle_thread(monkeypatch):
    monkeypatch.setenv("MRELAY_PROC_IDLE_SEC", "1")
    monkeypatch.setenv("MRELAY_PARENT_WATCH_SEC", "0")
    monkeypatch.setenv("MRELAY_LIFECYCLE_LOG", "0")
    ctx = lifecycle.startup(_Cfg(), is_stateless_provider=lambda: True)
    assert ctx._idle_thread is not None
    assert ctx._idle_thread.is_alive()
