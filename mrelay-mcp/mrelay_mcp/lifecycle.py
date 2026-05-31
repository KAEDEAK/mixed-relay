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
  ``LifecycleContext`` and starts up to four daemon watchdog threads:
    1. idle TTL watchdog: when ``in_flight == 0`` AND the stateless
       predicate returns True AND the idle period exceeds the TTL, exits
       the process via ``os._exit(0)``. The same loop also enforces an
       unconditional *hard* idle TTL (``MRELAY_PROC_HARD_IDLE_SEC``) so
       sessions that joined a channel (= ``is_stateless()`` is False
       forever) can still be recycled when the MCP host stops calling.
    2. parent polling watchdog: polls ``psutil.Process(initial_ppid)``
       and exits on death / reparent (= same PID with a different
       ``create_time``). Useful when the *direct* parent is the actual
       MCP client process; ineffective when an intermediate
       ``codex.exe app-server`` survives the user's ``codex exec`` call.
    3. native parent watchdog: blocks on an OS primitive
       (``WaitForSingleObject`` on Windows, ``pidfd_open`` + ``select``
       on Linux) for instant detection without polling delay. Skipped
       on platforms without a supported primitive (e.g. macOS) — the
       polling watchdog covers that fallback path.
- ``mark_request_start()`` / ``mark_request_end()`` track in-flight
  request count so long-blocking tools (``mr_poll`` / ``mr_wait_for``)
  never trip the idle watchdog mid-call.
- ``shutdown(reason, **extra)`` records the exit reason exactly once and
  returns ``True`` to the winning caller, ``False`` to losers. Watchdogs
  gate their ``os._exit(0)`` on this return so ``main()``'s
  ``KeyboardInterrupt`` / ``SystemExit`` / ``exception`` reason is not
  overwritten by a race.
- A small JSON registry in the package directory tracks live instances
  across Codex / Claude / VSCode generations. Startup and instance-list
  calls prune dead PID entries; shutdown marks the winning instance as
  ended before exit. At startup, the new instance also supersedes
  earlier instances that share the same ``(addr, nick, user)`` triple
  (default ON, gated by ``MRELAY_SUPERSEDE_OLDER``) — the old PIDs are
  terminated via ``psutil`` so the same client reconnecting cannot
  accumulate orphan generations.
- ``reset_to_noop_for_test()`` stops watchdogs and restores the no-op
  default. **Production code MUST NOT call this**; the name carries the
  contract.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - psutil missing means parent watchdog disabled
    psutil = None  # type: ignore[assignment]

if os.name == "nt":  # pragma: no cover - exercised on Windows in production
    import msvcrt
else:  # pragma: no cover - exercised on POSIX in CI if applicable
    import fcntl
    import select


# ---------------------------------------------------------------------------
# Native parent-process waiter (no-polling parent-death detection)
# ---------------------------------------------------------------------------


class _NativeParentWaiter:
    """Block on an OS primitive until ``ppid`` terminates.

    Windows  : ``WaitForSingleObject`` on the handle returned by
               ``OpenProcess(PROCESS_SYNCHRONIZE)``. The kernel signals
               the handle the moment the process exits, so detection
               latency is essentially zero.
    Linux    : ``pidfd_open(ppid)`` + ``select.select`` on the fd. The
               fd becomes readable when the process exits. Available on
               Python 3.9+ / Linux 5.3+.
    Other    : not supported. ``is_supported()`` returns False and the
               caller falls back to the psutil-based polling watchdog.

    The two ``wait`` implementations both honour ``stop_event`` by
    ticking on a short interval rather than truly blocking forever, so
    test teardown (= ``reset_to_noop_for_test()``) can shut the daemon
    thread down without leaking a real kernel wait.
    """

    def __init__(self, ppid):
        self._ppid = ppid

    @staticmethod
    def is_supported():
        if os.name == "nt":
            return True
        return hasattr(os, "pidfd_open")

    def wait(self, stop_event):
        if os.name == "nt":
            return self._wait_windows(stop_event)
        if hasattr(os, "pidfd_open"):
            return self._wait_linux(stop_event)
        return False

    def _wait_windows(self, stop_event):  # pragma: no cover - Windows-only
        import ctypes
        from ctypes import wintypes

        PROCESS_SYNCHRONIZE = 0x00100000
        WAIT_OBJECT_0 = 0x00000000
        WAIT_TIMEOUT = 0x00000102

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(PROCESS_SYNCHRONIZE, False, self._ppid)
        if not handle:
            return False
        try:
            while not stop_event.is_set():
                rc = kernel32.WaitForSingleObject(handle, 1000)
                if rc == WAIT_OBJECT_0:
                    return True
                if rc == WAIT_TIMEOUT:
                    continue
                return False
            return False
        finally:
            kernel32.CloseHandle(handle)

    def _wait_linux(self, stop_event):  # pragma: no cover - Linux-only
        try:
            pidfd = os.pidfd_open(self._ppid)
        except (OSError, AttributeError):
            return False
        try:
            while not stop_event.is_set():
                try:
                    ready, _, _ = select.select([pidfd], [], [], 1.0)
                except OSError:
                    return True
                if ready:
                    return True
            return False
        finally:
            try:
                os.close(pidfd)
            except OSError:
                pass


_REGISTRY_VERSION = 1
_REGISTRY_PATH = Path(__file__).with_name("instance_registry.json")
_REGISTRY_LOCK_PATH = _REGISTRY_PATH.with_suffix(".lock")


# ---------------------------------------------------------------------------
# Env parsing helpers
# ---------------------------------------------------------------------------


def _env_int(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_flag(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _derive_poll_interval(idle_ttl_sec, hard_idle_sec, env_override):
    if env_override is not None and env_override != "":
        try:
            v = float(env_override)
            if v > 0:
                return v
        except ValueError:
            pass
    candidates = [s for s in (idle_ttl_sec, hard_idle_sec) if s > 0]
    if not candidates:
        return 30.0
    return max(0.5, min(30.0, min(candidates) / 10.0))


def _utc_now():
    return datetime.now(timezone.utc)


def _isoformat_utc(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_process_create_time(pid):
    if psutil is None:
        return None
    try:
        return psutil.Process(pid).create_time()
    except Exception:
        return None


def _default_registry_doc():
    return {"version": _REGISTRY_VERSION, "instances": []}


def _read_registry_unlocked():
    if not _REGISTRY_PATH.exists():
        return _default_registry_doc()
    try:
        raw = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return _default_registry_doc()
    if not isinstance(raw, dict):
        return _default_registry_doc()
    instances = raw.get("instances")
    if not isinstance(instances, list):
        instances = []
    return {"version": _REGISTRY_VERSION, "instances": instances}


def _write_registry_unlocked(doc):
    _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _REGISTRY_PATH.with_suffix(
        f"{_REGISTRY_PATH.suffix}.tmp.{os.getpid()}"
    )
    payload = json.dumps(doc, ensure_ascii=True, indent=2, sort_keys=True)
    try:
        with tmp_path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, _REGISTRY_PATH)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


@contextmanager
def _registry_lock():
    _REGISTRY_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _REGISTRY_LOCK_PATH.open("a+b") as fh:
        fh.seek(0)
        if fh.tell() == 0:
            fh.write(b"0")
            fh.flush()
        fh.seek(0)
        if os.name == "nt":
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover - Windows desktop is primary target
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fh.seek(0)
            if os.name == "nt":
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - Windows desktop is primary target
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _entry_matches_process(entry, proc):
    expected = _as_float(entry.get("pid_create_time"))
    if expected is None:
        return True
    try:
        actual = proc.create_time()
    except Exception:
        return True
    return actual == expected


def _instance_is_live(entry):
    pid = _as_int(entry.get("pid"))
    if pid is None or pid <= 0:
        return False
    if psutil is None:
        return True
    try:
        proc = psutil.Process(pid)
        if not proc.is_running():
            return False
        try:
            if proc.status() == getattr(psutil, "STATUS_ZOMBIE", "zombie"):
                return False
        except Exception:
            pass
        return _entry_matches_process(entry, proc)
    except psutil.NoSuchProcess:
        return False
    except Exception:
        return True


def _prune_dead_instances(instances):
    live = []
    for entry in instances:
        if not isinstance(entry, dict):
            continue
        if _instance_is_live(entry):
            live.append(entry)
    return live


def _mutate_registry(mutator):
    with _registry_lock():
        doc = _read_registry_unlocked()
        instances = _prune_dead_instances(doc.get("instances", []))
        instances, result = mutator(instances)
        doc = {"version": _REGISTRY_VERSION, "instances": instances}
        _write_registry_unlocked(doc)
        return result


def _elapsed_seconds(entry, now_ts):
    started_ts = _as_float(entry.get("started_ts"))
    if started_ts is None:
        started_at = entry.get("started_at")
        if isinstance(started_at, str):
            try:
                started_ts = datetime.fromisoformat(
                    started_at.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                started_ts = now_ts
        else:
            started_ts = now_ts
    return max(0, int(now_ts - started_ts))


def _sorted_instances(instances):
    return sorted(
        instances,
        key=lambda item: (
            _as_float(item.get("started_ts")) or 0.0,
            _as_int(item.get("pid")) or 0,
        ),
    )


def _format_instance(entry, now_ts):
    out = {
        "pid": _as_int(entry.get("pid")),
        "ppid": _as_int(entry.get("ppid")),
        "status": entry.get("status", "running"),
        "started_at": entry.get("started_at"),
        "elapsed_sec": _elapsed_seconds(entry, now_ts),
        "addr": entry.get("addr", ""),
        "nick": entry.get("nick", ""),
        "user": entry.get("user", ""),
        "kind": entry.get("kind", ""),
    }
    ended_at = entry.get("ended_at")
    if ended_at:
        out["ended_at"] = ended_at
    shutdown_reason = entry.get("shutdown_reason")
    if shutdown_reason:
        out["shutdown_reason"] = shutdown_reason
    return out


# ---------------------------------------------------------------------------
# Contexts
# ---------------------------------------------------------------------------


class _BaseContext:
    def mark_request_start(self):
        raise NotImplementedError

    def mark_request_end(self):
        raise NotImplementedError

    def shutdown(self, reason, **extra):
        raise NotImplementedError

    def register_instance(self):
        return None


class _NoOpContext(_BaseContext):
    def mark_request_start(self):
        return None

    def mark_request_end(self):
        return None

    def shutdown(self, reason, **extra):
        return False


class LifecycleContext(_BaseContext):
    def __init__(self, cfg, is_stateless_provider):
        self._cfg = cfg
        self._is_stateless = is_stateless_provider
        self._idle_ttl_sec = _env_int("MRELAY_PROC_IDLE_SEC", 1800)
        self._hard_idle_sec = _env_int("MRELAY_PROC_HARD_IDLE_SEC", 300)
        self._parent_watch_sec = _env_int("MRELAY_PARENT_WATCH_SEC", 30)
        self._native_parent_wait_enabled = bool(
            _env_flag("MRELAY_NATIVE_PARENT_WAIT", 1)
        )
        self._supersede_enabled = bool(_env_flag("MRELAY_SUPERSEDE_OLDER", 1))
        self._supersede_grace_sec = _env_float(
            "MRELAY_SUPERSEDE_GRACE_SEC", 5.0
        )
        self._lifecycle_log_enabled = bool(_env_flag("MRELAY_LIFECYCLE_LOG", 1))
        self._poll_interval = _derive_poll_interval(
            self._idle_ttl_sec,
            self._hard_idle_sec,
            os.environ.get("MRELAY_PROC_IDLE_POLL_SEC"),
        )

        self._lock = threading.Lock()
        self._in_flight = 0
        self._last_idle_at = time.monotonic()
        self._shutdown_emitted = False
        self._stop_event = threading.Event()
        self._idle_thread = None
        self._parent_thread = None
        self._native_parent_thread = None
        self._pid = os.getpid()
        self._started_dt = _utc_now()
        self._started_ts = self._started_dt.timestamp()
        self._started_at = _isoformat_utc(self._started_dt)
        self._pid_create_time = _safe_process_create_time(self._pid)

        self._initial_ppid = os.getppid()
        self._initial_ppid_create_time = None
        if psutil is not None and self._parent_watch_sec > 0:
            try:
                self._initial_ppid_create_time = psutil.Process(
                    self._initial_ppid
                ).create_time()
            except Exception:
                self._initial_ppid_create_time = None

    def mark_request_start(self):
        with self._lock:
            self._in_flight += 1

    def mark_request_end(self):
        with self._lock:
            if self._in_flight > 0:
                self._in_flight -= 1
            if self._in_flight == 0:
                self._last_idle_at = time.monotonic()

    def shutdown(self, reason, **extra):
        with self._lock:
            if self._shutdown_emitted:
                return False
            self._shutdown_emitted = True
        self._mark_instance_stopped(reason)
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

    def close(self):
        self._stop_event.set()
        for t in (
            self._idle_thread,
            self._parent_thread,
            self._native_parent_thread,
        ):
            if t is not None and t.is_alive():
                t.join(timeout=2.0)

    def register_instance(self):
        superseded_pids = _mutate_registry(self._register_instance_mutation)
        if self._supersede_enabled and superseded_pids:
            self._terminate_superseded(superseded_pids)

    def _emit_startup(self):
        if not self._lifecycle_log_enabled:
            return
        addr = getattr(self._cfg, "addr", "")
        nick = getattr(self._cfg, "nick", "")
        native_wait = int(
            self._native_parent_wait_enabled
            and _NativeParentWaiter.is_supported()
        )
        sys.stderr.write(
            "[mrelay-mcp lifecycle] "
            f"event=startup pid={os.getpid()} ppid={self._initial_ppid} "
            f"start={datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} "
            f"addr={addr} nick={nick} "
            f"idle_ttl_sec={self._idle_ttl_sec} "
            f"hard_idle_sec={self._hard_idle_sec} "
            f"parent_watch_sec={self._parent_watch_sec} "
            f"native_parent_wait={native_wait} "
            f"supersede_older={int(self._supersede_enabled)}\n"
        )
        sys.stderr.flush()

    def _start_watchdogs(self):
        if self._idle_ttl_sec > 0 or self._hard_idle_sec > 0:
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
        if self._native_parent_wait_enabled and _NativeParentWaiter.is_supported():
            t = threading.Thread(
                target=self._native_parent_loop,
                daemon=True,
                name="mrelay-parent-wait-native",
            )
            t.start()
            self._native_parent_thread = t

    def _instance_record(self):
        return {
            "pid": self._pid,
            "ppid": self._initial_ppid,
            "pid_create_time": self._pid_create_time,
            "started_at": self._started_at,
            "started_ts": self._started_ts,
            "status": "running",
            "ended_at": None,
            "shutdown_reason": None,
            "addr": getattr(self._cfg, "addr", ""),
            "nick": getattr(self._cfg, "nick", ""),
            "user": getattr(self._cfg, "user", ""),
            "kind": getattr(self._cfg, "kind", ""),
        }

    def _same_process(self, entry):
        pid = _as_int(entry.get("pid"))
        if pid != self._pid:
            return False
        expected = _as_float(entry.get("pid_create_time"))
        if expected is None or self._pid_create_time is None:
            return True
        return expected == self._pid_create_time

    def _register_instance_mutation(self, instances):
        ended_at = _isoformat_utc(_utc_now())
        kept = []
        superseded = []
        for entry in instances:
            if self._same_process(entry):
                continue
            if self._supersede_enabled and self._is_supersession_candidate(entry):
                pid = _as_int(entry.get("pid"))
                if pid is not None and pid > 0:
                    superseded.append(pid)
                new_entry = dict(entry)
                new_entry["status"] = "stopped"
                new_entry["ended_at"] = ended_at
                new_entry["shutdown_reason"] = f"superseded_by_pid={self._pid}"
                kept.append(new_entry)
                continue
            kept.append(entry)
        kept.append(self._instance_record())
        return _sorted_instances(kept), superseded

    def _mark_instance_stopped(self, reason):
        ended_at = _isoformat_utc(_utc_now())

        def mutate(instances):
            updated = []
            found = False
            for entry in instances:
                if self._same_process(entry):
                    new_entry = dict(entry)
                    new_entry["status"] = "stopped"
                    new_entry["ended_at"] = ended_at
                    new_entry["shutdown_reason"] = reason
                    updated.append(new_entry)
                    found = True
                else:
                    updated.append(entry)
            if not found:
                new_entry = self._instance_record()
                new_entry["status"] = "stopped"
                new_entry["ended_at"] = ended_at
                new_entry["shutdown_reason"] = reason
                updated.append(new_entry)
            return _sorted_instances(updated), None

        _mutate_registry(mutate)

    def _idle_loop(self):
        if self._idle_ttl_sec <= 0 and self._hard_idle_sec <= 0:
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

            if self._hard_idle_sec > 0 and elapsed >= self._hard_idle_sec:
                if self.shutdown(
                    "hard_idle_timeout", elapsed_sec=int(elapsed)
                ):
                    os._exit(0)
                return

            if self._idle_ttl_sec > 0 and elapsed >= self._idle_ttl_sec:
                try:
                    stateless = bool(self._is_stateless())
                except Exception:
                    stateless = False
                if not stateless:
                    continue
                if self.shutdown("idle_timeout", elapsed_sec=int(elapsed)):
                    os._exit(0)
                return

    def _parent_loop(self):
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
            except psutil.NoSuchProcess:
                if self.shutdown("parent_gone", ppid=self._initial_ppid):
                    os._exit(0)
                return
            except Exception:
                pass

    def _native_parent_loop(self):
        waiter = _NativeParentWaiter(self._initial_ppid)
        died = waiter.wait(self._stop_event)
        if not died:
            return
        if self.shutdown(
            "parent_gone",
            ppid=self._initial_ppid,
            detail="native_wait",
        ):
            os._exit(0)

    def _is_supersession_candidate(self, entry):
        if entry.get("status") != "running":
            return False
        my_addr = getattr(self._cfg, "addr", "")
        my_nick = getattr(self._cfg, "nick", "")
        my_user = getattr(self._cfg, "user", "")
        if (
            entry.get("addr") != my_addr
            or entry.get("nick") != my_nick
            or entry.get("user") != my_user
        ):
            return False
        if self._same_process(entry):
            return False
        started_ts = _as_float(entry.get("started_ts"))
        if started_ts is not None:
            age = self._started_ts - started_ts
            if age < self._supersede_grace_sec:
                return False
        return True

    def _terminate_superseded(self, pids):
        if psutil is None or not pids:
            return
        for pid in pids:
            try:
                p = psutil.Process(pid)
            except psutil.NoSuchProcess:
                continue
            except Exception:
                continue
            if self._lifecycle_log_enabled:
                sys.stderr.write(
                    "[mrelay-mcp lifecycle] "
                    f"event=supersede target_pid={pid} my_pid={self._pid} "
                    f"nick={getattr(self._cfg, 'nick', '')} "
                    f"user={getattr(self._cfg, 'user', '')}\n"
                )
                sys.stderr.flush()
            try:
                p.terminate()
            except Exception:
                continue
            try:
                p.wait(timeout=3.0)
            except psutil.TimeoutExpired:
                try:
                    p.kill()
                except Exception:
                    pass
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Module-level dispatch
# ---------------------------------------------------------------------------


_ctx = _NoOpContext()


def startup(cfg, is_stateless_provider):
    global _ctx
    ctx = LifecycleContext(cfg, is_stateless_provider)
    _ctx = ctx
    ctx.register_instance()
    ctx._emit_startup()
    ctx._start_watchdogs()
    return ctx


def mark_request_start():
    _ctx.mark_request_start()


def mark_request_end():
    _ctx.mark_request_end()


def shutdown(reason, **extra):
    return _ctx.shutdown(reason, **extra)


def list_instances():
    def mutate(instances):
        ordered = _sorted_instances(instances)
        now_ts = time.time()
        rendered = [_format_instance(entry, now_ts) for entry in ordered]
        return ordered, rendered

    return _mutate_registry(mutate)


def reset_to_noop_for_test():
    global _ctx
    if isinstance(_ctx, LifecycleContext):
        _ctx.close()
    _ctx = _NoOpContext()
