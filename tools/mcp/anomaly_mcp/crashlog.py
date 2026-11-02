"""Summarize the last crash recorded in the newest game log."""

import re

from . import process

STACK_RE = re.compile(r"\.(cpp|hpp|h|c)\s*\(\d+\)\s*:\s*\S")  # "D:\...\xrDebugNew.cpp (500): xrDebug::fatal"


def crash_report(previous: bool = False) -> str:
    path = process.newest_log_file()
    if path is None:
        return "No log file found; nothing to report."
    if previous:
        path = process.previous_log_path(path)
        if not path.is_file():
            return f"No archived previous log at {path} (launch_game archives the log of the run before it)."
    running = process.find_game()
    head = f"Game {'running (pid %d)' % running.pid if running else 'not running'}; log: {path}"
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = [line.rstrip("\r\n") for line in f]

    fatal = max((i for i, s in enumerate(lines) if s.startswith("FATAL ERROR")), default=None)
    if fatal is None:
        out = [head, f"No crash recorded in {path}"]
        if not running:
            out += ["Last 10 log lines:", *lines[-10:]]
        return "\n".join(out)

    # The Lua traceback and script error come before FATAL ERROR; the rest after it.
    before, after = lines[:fatal], lines[fatal:]
    lua = [i for i, s in enumerate(before) if s.startswith("! [LUA]")]
    block = []
    if lua:
        end = lua[-1]
        start = end
        while start > 0 and before[start - 1].startswith("! [LUA]"):
            start -= 1
        block = before[start : end + 1][-15:]
    script_error = [s for s in before[lua[-1] if lua else 0 :] if s.startswith("! [SCRIPT ERROR]")][-1:]

    errors = [s for s in after if s.startswith("[error]")]
    lua_error = [s for s in after if s.lstrip().startswith("LUA error:")][:1]
    stack = [s for s in after if STACK_RE.search(s) and not s.startswith("!")][:10]
    dump = [s for s in after if ".dmp" in s.lower()][:1]

    out = [head, "Crash:", *errors, *lua_error, *script_error]
    if block:
        out += ["Lua traceback:", *block]
    if stack:
        out += ["C++ stack:", *stack]
    out += dump
    return "\n".join(out)
