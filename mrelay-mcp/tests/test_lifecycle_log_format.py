"""Startup and shutdown lifecycle log format.

The leak triage doc explicitly asked for one line per startup / exit so
``[mrelay-mcp lifecycle] event=... pid=... reason=...`` can be grepped
out of the codex-app-server stderr. These tests pin that format.
"""

from __future__ import annotations

import os

from mrelay_mcp import lifecycle


class _Cfg:
    addr = "127.0.0.1:6767"
    nick = "test-nick"


def test_startup_emits_one_line_with_pid_and_nick(capsys, monkeypatch):
    # Force lifecycle to not start watchdogs (= keep the test deterministic
    # by setting TTL=0 and parent_watch=0).
    monkeypatch.setenv("MRELAY_PROC_IDLE_SEC", "0")
    monkeypatch.setenv("MRELAY_PARENT_WATCH_SEC", "0")
    lifecycle.startup(_Cfg(), is_stateless_provider=lambda: True)
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if "event=startup" in ln]
    assert len(lines) == 1, f"expected one startup line, got: {err!r}"
    line = lines[0]
    assert line.startswith("[mrelay-mcp lifecycle]")
    assert f"pid={os.getpid()}" in line
    assert "nick=test-nick" in line
    assert "addr=127.0.0.1:6767" in line


def test_shutdown_emits_one_line_for_winner_only(capsys, monkeypatch):
    monkeypatch.setenv("MRELAY_PROC_IDLE_SEC", "0")
    monkeypatch.setenv("MRELAY_PARENT_WATCH_SEC", "0")
    lifecycle.startup(_Cfg(), is_stateless_provider=lambda: True)
    capsys.readouterr()  # drop the startup line
    first = lifecycle.shutdown("normal")
    second = lifecycle.shutdown("idle_timeout")
    err = capsys.readouterr().err
    shutdown_lines = [ln for ln in err.splitlines() if "event=shutdown" in ln]
    assert first is True
    assert second is False
    assert len(shutdown_lines) == 1, f"expected one shutdown line, got: {err!r}"
    assert "reason=normal" in shutdown_lines[0]


def test_lifecycle_log_can_be_disabled(capsys, monkeypatch):
    monkeypatch.setenv("MRELAY_PROC_IDLE_SEC", "0")
    monkeypatch.setenv("MRELAY_PARENT_WATCH_SEC", "0")
    monkeypatch.setenv("MRELAY_LIFECYCLE_LOG", "0")
    lifecycle.startup(_Cfg(), is_stateless_provider=lambda: True)
    lifecycle.shutdown("normal")
    err = capsys.readouterr().err
    assert "event=startup" not in err
    assert "event=shutdown" not in err
