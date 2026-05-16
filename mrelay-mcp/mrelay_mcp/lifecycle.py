"""Process lifecycle observability + self-recycle watchdogs for mrelay-mcp.

This module exists because long-lived MCP hosts (notably Codex Desktop's
``codex.exe app-server``) sometimes spawn a fresh ``mrelay_mcp.server``
subprocess without killing the previous one. The orphaned generations sit
idle, hold their stdin pipes open (so FastMCP's natural EOF exit never
fires), and accumulate over hours.

The module provides:

- An import-time ``_NoOpContext`` installed as the module-global ``_ctx``,
  so pure-unit callers of ``server._run()`` (e.g. the existing
  ``tests/test_validation_and_errors.py``) work without going through
  ``startup()``.
- ``startup(cfg, is_stateless_provider)`` installs a real
  ``LifecycleContext`` and starts two daemon watchdog threads:
    1. idle TTL watchdog: when ``in_flight == 0`` AND the stateless
       predicate returns True AND the idle period exceeds the TTL, exits
       the process via ``os._exit(0)``.
    2. parent watchdog: polls ``psutil.Process(initial_ppid)`` and exits
       on death / reparent (= same PID with a different ``create_time``).
- ``mark_request_start()`` / ``mark_request_end()`` track in-flight
  request count so long-blocking tools (``mr_poll`` / ``mr_wait_for``)
  never trip the idle watchdog mid-call.
- ``shutdown(reason, **extra)`` records the exit reason exactly once and
  returns ``True`` to the winning caller, ``False`` to losers. Watchdogs
  gate their ``os._exit(0)`` on this return so ``main()``'s
  ``KeyboardInterrupt`` / ``SystemExit`` / ``exception`` reason is not
  overwritten by a race.
- ``reset_to_noop_for_test()`` stops watchdogs and restores the no-op
  default. **Production code MUST NOT call this**; the name carries the
  contract.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - psutil missing means parent watchdog disabled
    psutil = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Env parsing helpers
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_flag(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _derive_poll_interval(idle_ttl_sec: int, env_override: Optional[str]) -> float:
    """Pick the idle watchdog tick interval.

    Auto-derive from TTL (= TTL/10) within [0.5, 30] seconds, so a small
    TTL used for tests still fires quickly while a 30-minute production
    TTL polls at a coarse 30s cadence. Override via
    ``MRELAY_PROC_IDLE_POLL_SEC`` for surgical control.
    """
    if env_override is not None and env_override != "":
        try:
            v = float(env_override)
            if v > 0:
                return v
        except ValueError:
            pass
    if idle_ttl_sec <= 0:
        return 30.0
    return max(0.5, min(30.0, idle_ttl_sec / 10.0))


# ---------------------------------------------------------------------------
# Contexts
# ---------------------------------------------------------------------------


class _BaseContext:
    """Common API surface so the module-level helpers can dispatch without
    knowing which context is currently installed."""

    def mark_request_start(self) -> None:
        raise NotImplementedError

    def mark_request_end(self) -> None:
        raise NotImplementedError

    def shutdown(self, reason: str, **extra: Any) -> bool:
        raise NotImplementedError


class _NoOpContext(_BaseContext):
    """Default context installed at import time.

    Lets ``server._run()`` call ``lifecycle.mark_request_start/end()``
    without ``startup()`` having run, so existing pure-unit tests on
    ``_run()`` keep working.
    """

    def mark_request_start(self) -> None:
        return None

    def mark_request_end(self) -> None:
        return None

    def shutdown(self, reason: str, **extra: Any) -> bool:
        return False


class LifecycleContext(_BaseContext):
    """Real lifecycle context: in-flight counter + watchdog threads.

    Construct via ``startup(cfg, is_stateless_provider)``; do not
    instantiate directly in production. Watchdogs run as daemon threads
    so they do not block interpreter exit.
    """

    def __init__(
        self,
        cfg: Any,
        is_stateless_provider: Callable[[], bool],
    ) -> None:
        self._cfg = cfg
        self._is_stateless = is_stateless_provider
        self._idle_ttl_sec = _env_int("MRELAY_PROC_IDLE_SEC", 1800)
        self._parent_watch_sec = _env_int("MRELAY_PARENT_WATCH_SEC", 30)
        self._lifecycle_log_enabled = bool(_env_flag("MRELAY_LIFECYCLE_LOG", 1))
        self._poll_interval = _derive_poll_interval(
            self._idle_ttl_sec,
            os.environ.get("MRELAY_PROC_IDLE_POLL_SEC"),
        )

        self._lock = threading.Lock()
        self._in_flight = 0
        self._last_idle_at = time.monotonic()
        self._shutdown_emitted = False
        self._stop_event = threading.Event()
        self._idle_thread: Optional[threading.Thread] = None
        self._parent_thread: Optional[threading.Thread] = None

        # Snapshot the initial parent identity so a reparent (= same PID,
        # different create_time) trips the watchdog as definitively as a
        # plain disappearance.
        self._initial_ppid = os.getppid()
        self._initial_ppid_create_time: Optional[float] = None
        if psutil is not None and self._parent_watch_sec > 0:
            try:
                self._initial_ppid_create_time = psutil.Process(
                    self._initial_ppid
                ).create_time()
            except Exception:
                self._initial_ppid_create_time = None

    # ----- public API -----

    def mark_request_start(self) -> None:
        with self._lock:
            self._in_flight += 1

    def mark_request_end(self) -> None:
        with self._lock:
            if self._in_flight > 0:
                self._in_flight -= 1
            if self._in_flight == 0:
                # Restart the idle clock the moment we drop back to zero.
                self._last_idle_at = time.monotonic()

    def shutdown(self, reason: str, **extra: Any) -> bool:
        """Record the exit reason. Returns True for the winning caller,
        False for subsequent calls.

        Watchdogs MUST gate their ``os._exit(0)`` on this return value so
        a real ``KeyboardInterrupt`` / ``SystemExit`` / ``exception`` in
        ``main()`` is not overwritten with a misleading ``idle_timeout``
        in a race.
        """
        with self._lock:
            if self._shutdown_emitted:
                return False
            self._shutdown_emitted = True
        if self._lifecycle_log_enabled:
            parts = [
                "event=shutdown",
                f"pid={os.getpid()}",
                f"reason={reason}",
            ]
            for k, v in extra.items():
                parts.append(f"{k}={v}")
            sys.stderr.write("[mrelay-mcp lifecycle] " + " ".join(parts) + "\n")
            sys.stderr.flush()
        return True

    def close(self) -> None:
        """Stop watchdog threads. Idempotent. Test-only — production
        never calls this because ``main()`` ends via ``mcp.run()`` return
        or raise."""
        self._stop_event.set()
        for t in (self._idle_thread, self._parent_thread):
            if t is not None and t.is_alive():
                t.join(timeout=1.0)

    # ----- startup helpers (called by startup() factory) -----

    def _emit_startup(self) -> None:
        if not self._lifecycle_log_enabled:
            return
        addr = getattr(self._cfg, "addr", "")
        nick = getattr(self._cfg, "nick", "")
        sys.stderr.write(
            "[mrelay-mcp lifecycle] "
            f"event=startup pid={os.getpid()} ppid={self._initial_ppid} "
            f"start={datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} "
            f"addr={addr} nick={nick} "
            f"idle_ttl_sec={self._idle_ttl_sec} "
            f"parent_watch_sec={self._parent_watch_sec}\n"
        )
        sys.stderr.flush()

    def _start_watchdogs(self) -> None:
        if self._idle_ttl_sec > 0:
            t = threading.Thread(
                target=self._idle_loop,
                daemon=True,
                name="mrelay-idle-watchdog",
            )
            t.start()
            self._idle_thread = t
        if self._parent_watch_sec > 0 and self._initial_ppid_create_time is not None:
            t = threading.Thread(
                target=self._parent_loop,
                daemon=True,
                name="mrelay-parent-watchdog",
            )
            t.start()
            self._parent_thread = t

    # ----- watchdog loops -----

    def _idle_loop(self) -> None:
        # Defence in depth: _start_watchdogs() already gates on TTL > 0,
        # but we also short-circuit here so a stray instantiation cannot
        # race the close() event.
        if self._idle_ttl_sec <= 0:
            return
        while not self._stop_event.is_set():
            if self._stop_event.wait(self._poll_interval):
                return
            with self._lock:
                in_flight = self._in_flight
                last_idle = self._last_idle_at
            if in_flight > 0:
                continue
            elapsed = time.monotonic() - last_idle
            if elapsed < self._idle_ttl_sec:
                continue
            # in_flight==0 and TTL elapsed — confirm the bridge is
            # actually idle (no joined channels / queued events / etc.)
            # before recycling.
            try:
                stateless = bool(self._is_stateless())
            except Exception:
                # If the predicate itself blew up, err on the side of
                # *not* recycling so a buggy predicate cannot silently
                # kill a healthy session.
                stateless = False
            if not stateless:
                continue
            if self.shutdown("idle_timeout", elapsed_sec=int(elapsed)):
                os._exit(0)
            return  # lost the race; main() owns the exit reason

    def _parent_loop(self) -> None:
        if self._parent_watch_sec <= 0 or psutil is None:
            return
        while not self._stop_event.is_set():
            if self._stop_event.wait(self._parent_watch_sec):
                return
            try:
                p = psutil.Process(self._initial_ppid)
                running = p.is_running()
                status = None
                try:
                    status = p.status()
                except Exception:
                    pass
                if not running or status == getattr(psutil, "STATUS_ZOMBIE", "zombie"):
                    if self.shutdown("parent_gone", ppid=self._initial_ppid):
                        os._exit(0)
                    return
                # Same PID with a different start time = our original
                # parent died and the PID got reused — orphaned in spirit.
                if self._initial_ppid_create_time is not None and (
                    p.create_time() != self._initial_ppid_create_time
                ):
                    if self.shutdown(
                        "parent_gone",
                        ppid=self._initial_ppid,
                        detail="ppid_reused",
                    ):
                        os._exit(0)
                    return
            except psutil.NoSuchProcess:  # type: ignore[attr-defined]
                if self.shutdown("parent_gone", ppid=self._initial_ppid):
                    os._exit(0)
                return
            except Exception:
                # Transient psutil errors (= access denied flicker on
                # Windows) should not cause exit.
                pass


# ---------------------------------------------------------------------------
# Module-level dispatch
# ---------------------------------------------------------------------------


_ctx: _BaseContext = _NoOpContext()


def startup(
    cfg: Any,
    is_stateless_provider: Callable[[], bool],
) -> LifecycleContext:
    """Install a real lifecycle context and start watchdog threads.

    Returns the constructed context so tests can drive it directly. In
    production, ``server.main()`` calls this once after argparse has
    resolved cfg, and never holds onto the return value.
    """
    global _ctx
    ctx = LifecycleContext(cfg, is_stateless_provider)
    _ctx = ctx
    ctx._emit_startup()
    ctx._start_watchdogs()
    return ctx


def mark_request_start() -> None:
    _ctx.mark_request_start()


def mark_request_end() -> None:
    _ctx.mark_request_end()


def shutdown(reason: str, **extra: Any) -> bool:
    return _ctx.shutdown(reason, **extra)


def reset_to_noop_for_test() -> None:
    """Restore the import-time NoOp context.

    Used by the pytest autouse fixture in ``tests/conftest.py`` so a test
    that calls ``startup()`` cannot leak its watchdogs into a sibling
    test. The verbose name is deliberate — production code must never
    call this.
    """
    global _ctx
    if isinstance(_ctx, LifecycleContext):
        _ctx.close()
    _ctx = _NoOpContext()
