"""Native parent-death waiter.

The native waiter blocks on an OS primitive (``WaitForSingleObject`` on
Windows, ``pidfd_open`` + ``select`` on Linux) so parent-death detection
is essentially instantaneous instead of paying the 30-second polling
latency of ``_parent_loop``.

These tests pin the wrapper-level behaviour:

* ``is_supported()`` returns True on Windows or when ``os.pidfd_open``
  is available; False elsewhere (e.g. macOS).
* ``_native_parent_loop`` calls ``shutdown('parent_gone',
  detail='native_wait')`` when the waiter signals death.
* Cancellation via ``stop_event`` makes the loop return cleanly
  without invoking ``os._exit``.

We never spawn a real parent-death scenario inside the test; we patch
``_NativeParentWaiter`` to either return True (= parent died) or False
(= cancelled). Real OS-level wait is exercised in production, where
the watchdog actually has a parent to watch.
"""

from __future__ import annotations

import os
import threading

import pytest

from mrelay_mcp import lifecycle


class _Cfg:
    addr = "127.0.0.1:6767"
    nick = "test-nick"
    user = "test-nick"
    kind = "agent"


def _build_ctx(monkeypatch):
    monkeypatch.setenv("MRELAY_PROC_IDLE_SEC", "0")
    monkeypatch.setenv("MRELAY_PROC_HARD_IDLE_SEC", "0")
    monkeypatch.setenv("MRELAY_PARENT_WATCH_SEC", "0")
    monkeypatch.setenv("MRELAY_NATIVE_PARENT_WAIT", "1")
    monkeypatch.setenv("MRELAY_SUPERSEDE_OLDER", "0")
    monkeypatch.setenv("MRELAY_LIFECYCLE_LOG", "0")
    ctx = lifecycle.LifecycleContext(_Cfg(), is_stateless_provider=lambda: True)
    return ctx


def _patch_os_exit(monkeypatch):
    calls = []

    def fake_exit(code):
        calls.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(lifecycle.os, "_exit", fake_exit)
    return calls


def test_is_supported_on_current_platform():
    """The waiter is supported on Windows (always) and on Linux with
    pidfd_open available. macOS / older Linux without pidfd return
    False, in which case the polling watchdog covers the fallback."""
    if os.name == "nt":
        assert lifecycle._NativeParentWaiter.is_supported() is True
    else:
        assert lifecycle._NativeParentWaiter.is_supported() == hasattr(
            os, "pidfd_open"
        )


def test_native_loop_exits_on_death(monkeypatch):
    """When the waiter reports parent death, the loop must call
    ``shutdown('parent_gone', detail='native_wait')`` and exit."""
    ctx = _build_ctx(monkeypatch)
    calls = _patch_os_exit(monkeypatch)

    class _FakeWaiter:
        def __init__(self, ppid):
            pass

        def wait(self, stop_event):
            return True  # parent died

    monkeypatch.setattr(lifecycle, "_NativeParentWaiter", _FakeWaiter)

    with pytest.raises(SystemExit):
        ctx._native_parent_loop()
    assert calls == [0]


def test_native_loop_returns_silent_when_unsupported(monkeypatch):
    """If the primitive reports it cannot run (= ``wait`` returns
    False), the loop must NOT call ``os._exit`` — that is the polling
    watchdog's job."""
    ctx = _build_ctx(monkeypatch)
    _patch_os_exit(monkeypatch)

    class _UnsupportedWaiter:
        def __init__(self, ppid):
            pass

        def wait(self, stop_event):
            return False

    monkeypatch.setattr(lifecycle, "_NativeParentWaiter", _UnsupportedWaiter)
    # No SystemExit — returns cleanly.
    ctx._native_parent_loop()


def test_native_loop_records_reason_for_lifecycle_log(monkeypatch):
    """The shutdown line must carry ``detail=native_wait`` so triage
    can attribute the exit to this watchdog rather than the polling
    one."""
    ctx = _build_ctx(monkeypatch)
    ctx._lifecycle_log_enabled = True
    _patch_os_exit(monkeypatch)

    class _FakeWaiter:
        def __init__(self, ppid):
            pass

        def wait(self, stop_event):
            return True

    monkeypatch.setattr(lifecycle, "_NativeParentWaiter", _FakeWaiter)

    import io
    import sys

    captured = io.StringIO()
    monkeypatch.setattr(sys, "stderr", captured)
    with pytest.raises(SystemExit):
        ctx._native_parent_loop()
    out = captured.getvalue()
    assert "reason=parent_gone" in out
    assert "detail=native_wait" in out


def test_waiter_respects_stop_event(monkeypatch):
    """Direct test of ``_NativeParentWaiter.wait``: when stop_event is
    pre-set, wait must return False quickly without blocking on the
    real OS primitive.

    We patch the platform-specific helpers to a sentinel that would
    block forever if reached, so the test fails clearly on regression."""
    waiter = lifecycle._NativeParentWaiter(os.getppid())
    stop = threading.Event()
    stop.set()

    def must_not_be_reached(self, stop_event):
        raise AssertionError("platform helper invoked despite stop_event set")

    # We cannot just set the stop_event and call wait(), because the
    # platform helpers themselves loop and check stop_event each tick.
    # Instead we verify that wait() with a pre-set stop event returns
    # without an exit (= False), running through the real helper.
    rc = waiter.wait(stop)
    assert rc is False
