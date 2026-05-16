"""``MRELAY_PARENT_WATCH_SEC=0`` must keep the parent watchdog from
ever starting. Companion to ``test_idle_ttl_zero_disable.py``.
"""

from __future__ import annotations

from mrelay_mcp import lifecycle


class _Cfg:
    addr = "127.0.0.1:6767"
    nick = "test-nick"


def test_zero_parent_watch_does_not_start_thread(monkeypatch):
    monkeypatch.setenv("MRELAY_PROC_IDLE_SEC", "0")
    monkeypatch.setenv("MRELAY_PARENT_WATCH_SEC", "0")
    monkeypatch.setenv("MRELAY_LIFECYCLE_LOG", "0")
    ctx = lifecycle.startup(_Cfg(), is_stateless_provider=lambda: True)
    assert ctx._parent_thread is None
