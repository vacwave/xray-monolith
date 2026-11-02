from anomaly_mcp import crashlog, process

CRASH = r"""normal line 1
normal line 2
! [LUA] stack traceback:
! [LUA] [C]: in function 'next'
! [LUA] scripts/foo.script:10: in function 'bar'
! [SCRIPT ERROR]: invalid key to 'next'
FATAL ERROR

[error]Expression    : <no expression>
[error]Function      : CScriptEngine::lua_error
[error]File          : D:\src\xrGame\script_engine.cpp
[error]Line          : 123
[error]Description   : fatal error
[error]Arguments     : LUA error: invalid key to 'next'

LUA error: invalid key to 'next'
D:\src\xrCore\xrDebugNew.cpp (500): xrDebug::fatal
D:\src\xrGame\script_engine.cpp (123): CScriptEngine::lua_error
Minidump written to C:\Anomaly\appdata\crash.dmp
"""


def patch(monkeypatch, path, running=None):
    monkeypatch.setattr(process, "newest_log_file", lambda: path)
    monkeypatch.setattr(process, "find_game", lambda: running)


def test_crash_report(tmp_path, monkeypatch):
    log = tmp_path / "a.log"
    log.write_text(CRASH, encoding="utf-8")
    patch(monkeypatch, log)
    report = crashlog.crash_report()
    print(report)
    assert report.startswith(f"Game not running; log: {log}")
    assert "[error]Function      : CScriptEngine::lua_error" in report
    assert "LUA error: invalid key to 'next'" in report
    assert "! [SCRIPT ERROR]: invalid key to 'next'" in report
    assert "! [LUA] scripts/foo.script:10: in function 'bar'" in report
    assert "xrDebugNew.cpp (500): xrDebug::fatal" in report
    assert "crash.dmp" in report
    assert "normal line" not in report


def test_no_crash(tmp_path, monkeypatch):
    log = tmp_path / "a.log"
    log.write_text("\n".join(f"line {i}" for i in range(30)), encoding="utf-8")
    patch(monkeypatch, log)
    report = crashlog.crash_report()
    assert f"No crash recorded in {log}" in report
    assert "line 29" in report and "line 19" not in report


def test_no_log(monkeypatch):
    patch(monkeypatch, None)
    assert "No log file" in crashlog.crash_report()
