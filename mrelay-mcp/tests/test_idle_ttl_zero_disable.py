"""``MRELAY_PROC_IDLE_SEC=0`` AND ``MRELAY_PROC_HARD_IDLE_SEC=0`` must
keep the idle watchdog thread from ever starting. Belt and suspenders
for the disable contract.

The idle loop services *both* the stateless-gated soft idle and the
unconditional hard idle, so disabling the thread now requires zeroing
both knobs — the test pins that exact AND condition.
"""

from __future__ import annotations

from mrelay_mcp import lifecycle


class _Cfg:
    addr = "127.0.0.1:6767"
    nick = "test-nick"


def _quiet_env(monkeypatch):
    """Disable every non-target watchdog so the test only observes the
    idle thread's start/no-start decision."""
    monkeypatch.setenv("MRELAY_PARENT_WATCH_SEC", "0")
    monkeypatch.setenv("MRELAY_NATIVE_PARENT_WAIT", "0")
    monkeypatch.setenv("MRELAY_SUPERSEDE_OLDER", "0")
    monkeypatch.setenv("MRELAY_LIFECYCLE_LOG", "0")


def test_zero_ttl_does_not_start_idle_thread(monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setenv("MRELAY_PROC_IDLE_SEC", "0")
    monkeypatch.setenv("MRELAY_PROC_HARD_IDLE_SEC", "0")
    ctx = lifecycle.startup(_Cfg(), is_stateless_provider=lambda: True)
    assert ctx._idle_thread is None


def test_positive_ttl_starts_idle_thread(monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setenv("MRELAY_PROC_IDLE_SEC", "1")
    monkeypatch.setenv("MRELAY_PROC_HARD_IDLE_SEC", "0")
    ctx = lifecycle.startup(_Cfg(), is_stateless_provider=lambda: True)
    assert ctx._idle_thread is not None
    assert ctx._idle_thread.is_alive()


def test_positive_hard_idle_alone_starts_idle_thread(monkeypatch):
    """Only the unconditional hard-idle knob is set — the same idle
    thread must still start because it is the worker for *both*
    timeouts."""
    _quiet_env(monkeypatch)
    monkeypatch.setenv("MRELAY_PROC_IDLE_SEC", "0")
    monkeypatch.setenv("MRELAY_PROC_HARD_IDLE_SEC", "1")
    ctx = lifecycle.startup(_Cfg(), is_stateless_provider=lambda: True)
    assert ctx._idle_thread is not None
    assert ctx._idle_thread.is_alive()
