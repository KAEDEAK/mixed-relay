"""``shutdown()`` must return True for exactly one caller (= the winner)
and False afterwards. Watchdogs gate their ``os._exit(0)`` on this so a
late watchdog cannot overwrite ``main()``'s real exit reason.
"""

from __future__ import annotations

from mrelay_mcp import lifecycle


class _Cfg:
    addr = "127.0.0.1:6767"
    nick = "test-nick"


def test_first_shutdown_wins_second_loses(capsys, monkeypatch):
    monkeypatch.setenv("MRELAY_PROC_IDLE_SEC", "0")
    monkeypatch.setenv("MRELAY_PARENT_WATCH_SEC", "0")
    lifecycle.startup(_Cfg(), is_stateless_provider=lambda: True)
    capsys.readouterr()
    assert lifecycle.shutdown("normal") is True
    assert lifecycle.shutdown("idle_timeout") is False
    assert lifecycle.shutdown("signal_int") is False
    err = capsys.readouterr().err
    assert err.count("event=shutdown") == 1


def test_winner_records_reason_extra_fields(capsys, monkeypatch):
    monkeypatch.setenv("MRELAY_PROC_IDLE_SEC", "0")
    monkeypatch.setenv("MRELAY_PARENT_WATCH_SEC", "0")
    lifecycle.startup(_Cfg(), is_stateless_provider=lambda: True)
    capsys.readouterr()
    lifecycle.shutdown("exception", type="RuntimeError", message="boom")
    err = capsys.readouterr().err
    assert "reason=exception" in err
    assert "type=RuntimeError" in err
    assert "message=boom" in err
