import os
import time

import pytest

from anomaly_mcp import process, server


@pytest.fixture(autouse=True)
def no_game_process(monkeypatch):
    monkeypatch.setattr(process, "find_game", lambda: None)
    monkeypatch.setattr(process, "find_window", lambda pid: None)  # never touch real windows


def test_game_status_ok(engine):
    assert server.game_status() == {
        "running": False,
        "endpoint": "ok",
        "frame": 1234,
        "paused": 0,
        "level": 1,
        "pending": 2,
    }


def test_game_status_not_running(dead_port):
    assert server.game_status() == {"running": False, "endpoint": "not_running"}


def test_game_status_timeout(engine):
    from conftest import NO_REPLY

    engine.handler = lambda op, arg: NO_REPLY
    assert server.game_status()["endpoint"] == "timeout"


def test_game_status_never_raises(engine, monkeypatch):
    def boom():
        raise RuntimeError("psutil broke")

    monkeypatch.setattr(process, "find_game", boom)
    engine.handler = lambda op, arg: b"garbage"
    result = server.game_status()
    assert result["running"] is None
    assert result["endpoint"] == "error"


def test_lua_eval(engine):
    assert server.lua_eval("return 1+1") == "2"
    assert server.lua_eval("error()") == 'Lua error: [string "error()"]:1: boom'
    assert server.lua_eval("x = 1") == ""


def test_lua_eval_timeout(engine):
    t0 = time.monotonic()
    assert server.lua_eval("hang", timeout=0.5).startswith("Timed out after 0.5 s")
    assert time.monotonic() - t0 < 2


def test_tools_not_running(dead_port):
    assert server.lua_eval("return 1").startswith("Game is not running")
    assert server.console("cfg_save").startswith("Game is not running")


def test_console(engine):
    assert server.console("cfg_save") == "ok (no output)"
    assert engine.requests[-1] == ("console", "cfg_save")
    engine.handler = lambda op, arg: b"+- g_god 0\n! second line"
    assert server.console("g_god") == "- g_god 0\n! second line"
    engine.handler = lambda op, arg: b"-console not available"
    assert server.console("cfg_save") == "Error: console not available"


def test_log_tail_from_engine(engine):
    assert server.log_tail(3) == "line 0\nline 1\nline 2"
    assert engine.requests[-1] == ("log", "3")


def _make_logs(tmp_path):
    logs = tmp_path / "appdata" / "logs"
    logs.mkdir(parents=True)
    old = logs / "old.log"
    old.write_text("old\n")
    os.utime(old, (1, 1))
    new = logs / "xray_user.log"
    new.write_bytes(b"".join(f"l{i}\r\n".encode() for i in range(10)) + b"caf\xe9\r\n")
    (logs / "crash.mdmp").write_bytes(b"\0")
    return new


def test_log_tail_fallback(dead_port, tmp_path, monkeypatch):
    monkeypatch.setenv("ANOMALY_DIR", str(tmp_path))
    path = _make_logs(tmp_path)
    lines = server.log_tail(3).split("\n")
    assert lines[0] == f"[game not running; last 3 lines of {path}]"
    assert lines[1:] == ["l8", "l9", "caf�"]


def test_log_tail_fallback_no_logs(dead_port, tmp_path, monkeypatch):
    monkeypatch.setenv("ANOMALY_DIR", str(tmp_path))
    assert server.log_tail(5).startswith("[game not running; no log file found")


class FakeProc:
    pid = 4242


def test_launch_refuses_when_running(monkeypatch):
    monkeypatch.setattr(process, "find_game", lambda: FakeProc())
    monkeypatch.setattr(process.subprocess, "Popen", lambda *a, **k: pytest.fail("must not start"))
    assert server.launch_game() == "Refused: AnomalyDX11AVX.exe is already running (pid 4242)."


def test_launch_exe_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("ANOMALY_DIR", str(tmp_path))
    cache = tmp_path / "appdata" / "shaders_cache"
    cache.mkdir(parents=True)
    monkeypatch.setattr(process.subprocess, "Popen", lambda *a, **k: pytest.fail("must not start"))
    report = server.launch_game(clear_shader_cache=True)
    assert report.startswith("Cannot launch:") and "AnomalyDX11AVX.exe does not exist" in report
    assert cache.exists()  # nothing touched when we cannot launch


def test_launch_starts_and_waits(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("ANOMALY_DIR", str(tmp_path))
    monkeypatch.setenv("ANOMALY_EXE", "Fake.exe")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "Fake.exe").write_bytes(b"")
    cache = tmp_path / "appdata" / "shaders_cache"
    cache.mkdir(parents=True)
    started = {}

    class FakePopen:
        pid = 99

        def __init__(self, args, cwd, **kwargs):
            started.update(args=args, cwd=cwd, kwargs=kwargs)

        def poll(self):
            return None

    monkeypatch.setattr(process.subprocess, "Popen", FakePopen)
    report = server.launch_game(clear_shader_cache=True)
    assert not cache.exists()
    assert started["args"] == [str(tmp_path / "bin" / "Fake.exe"), "-smap2048", "-dbg", "-silent_error_mode", "-minimized", "-nosplashwindow"]
    assert started["cwd"] == tmp_path / "bin"
    assert started["kwargs"]["stdout"] == process.subprocess.DEVNULL
    assert "Endpoint answered" in report


def test_kill_not_running():
    assert server.kill_game() == "AnomalyDX11AVX.exe is not running."
