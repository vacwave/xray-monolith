"""MCP server 'anomaly': tools to inspect and drive a running Anomaly instance.

Sync tools; the MCP SDK runs them in worker threads, so a slow call does not
block other tool calls.
"""

from collections import deque

from mcp.server.mcpserver import Image, MCPServer

from . import config, crashlog, game, process
from .engine_client import EngineClient, EngineError, EngineNotRunning, EngineTimeout

mcp = MCPServer(
    "anomaly",
    instructions=(
        "Talk to a running S.T.A.L.K.E.R. Anomaly game through its built-in endpoint. "
        "Start with game_status. Lua and console commands run on the game's main thread, "
        "so they wait while a level is loading."
    ),
)


def _client() -> EngineClient:
    return EngineClient(config.HOST, config.port())


def _not_running() -> str:
    return f"Game is not running (nothing listening on {config.HOST}:{config.port()})."


def _timed_out(timeout: float) -> str:
    return (
        f"Timed out after {timeout:g} s waiting for the game's main thread. The request is still "
        "queued and will run when the engine gets to it (e.g. after a level load); check game_status."
    )


@mcp.tool()
def game_status() -> dict:
    """Is the game running and is its endpoint answering?

    Returns a dict:
    - running: True if the game process is found.
    - endpoint: "ok" (answers), "not_running" (nothing listening), "timeout"
      (connected but no answer within 2 s), or "error" (with an "error" message).
    - When endpoint is "ok", also: frame (main-thread frame counter; if it does
      not advance between calls the main thread is stuck or loading), paused (0/1),
      level (1 if a level is loaded, 0 at the main menu), pending (Lua/console
      requests queued for the main thread, not yet run).
    Never raises.
    """
    result: dict = {}
    try:
        result["running"] = process.find_game() is not None
    except Exception as e:  # psutil access issues must not break status
        result["running"] = None
        result["process_error"] = str(e)
    try:
        result.update(_client().status())
        result["endpoint"] = "ok"
    except EngineNotRunning:
        result["endpoint"] = "not_running"
    except EngineTimeout:
        result["endpoint"] = "timeout"
    except Exception as e:
        result["endpoint"] = "error"
        result["error"] = str(e)
    return result


@mcp.tool()
def lua_eval(code: str, timeout: float = 10) -> str:
    """Run a Lua chunk on the game's main thread and return its results.

    `code` is a Lua chunk, so use `return` to get values back, e.g.
    "return db.actor and db.actor:position()". Multiple return values come back
    one per line, serialized (tables to depth 3). Empty output means the chunk
    returned nothing.
    Errors come back as text starting with "Lua error: " (the traceback is also
    in the game log). Runs only when the main thread is free (not during a level
    load); waits at most `timeout` seconds.
    """
    try:
        return _client().request("eval", code, timeout=timeout)
    except EngineError as e:
        return f"Lua error: {e}"
    except EngineNotRunning:
        return _not_running()
    except EngineTimeout:
        return _timed_out(timeout)


@mcp.tool()
def console(cmd: str, timeout: float = 10) -> str:
    """Execute an engine console command (e.g. "cfg_save", "g_god 1") on the main thread.

    Returns "ok" once it has executed, or an error text. Console output is not
    returned; read it with log_tail. Debug-only commands work because the game
    is launched with -dbg.
    """
    try:
        _client().request("console", cmd, timeout=timeout)
        return "ok"
    except EngineError as e:
        return f"Error: {e}"
    except EngineNotRunning:
        return _not_running()
    except EngineTimeout:
        return _timed_out(timeout)


@mcp.tool()
def log_tail(n: int = 50) -> str:
    """Return the last `n` lines of the game log (engine keeps the last 2000).

    If the game is not running (e.g. after a crash), returns the last `n` lines
    of the newest log file in <ANOMALY_DIR>\appdata\logs instead; the first line
    then says so and names the file.
    """
    n = max(1, n)
    try:
        return _client().request("log", str(n), timeout=5)
    except EngineNotRunning:
        reason = "game not running"
    except EngineTimeout:
        reason = "endpoint did not answer"
    except EngineError as e:
        reason = f"endpoint error: {e}"
    path = process.newest_log_file()
    if path is None:
        return f"[{reason}; no log file found in {config.anomaly_dir() / 'appdata' / 'logs'}]"
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = deque((line.rstrip("\r\n") for line in f), maxlen=n)
    return "\n".join([f"[{reason}; last {len(lines)} lines of {path}]", *lines])


@mcp.tool()
def crash_report() -> str:
    """Explain why the game died: the last crash recorded in the newest game log.

    First line: whether the game process is running and the log file path. Then,
    if the log has a FATAL ERROR: its [error] lines (expression, function, file,
    line, description), the Lua error message, the last Lua traceback, up to 10
    lines of the C++ stack and the minidump line if any. With no crash in the log
    it says so and, when the game is not running, shows the last 10 log lines (a
    silent exit). Reads the file, so it works when the game is dead.
    """
    return crashlog.crash_report()


@mcp.tool()
def launch_game(clear_shader_cache: bool = False, test_map: bool = False, minimize: bool = True) -> str:
    r"""Start the game (<ANOMALY_DIR>\bin\<ANOMALY_EXE> -smap2048 -dbg -silent_error_mode) and wait for its endpoint.

    -silent_error_mode makes the engine write crashes to the log without any dialog, so an
    unattended run never blocks on a message box; see crash_report.
    Refuses if it is already running. clear_shader_cache=True deletes
    appdata\shaders_cache first (needed after rendering/shader changes).
    Blocks until the endpoint answers status (about 90 s at most), then runs
    "g_always_active on" (the game keeps running and rendering while unfocused or
    minimized) and "keypress_on_start off" (no "press any key" after a load).
    The game is then at the main menu.
    test_map=True then also does what start_test_map does (up to ~3 more minutes)
    and reports the level name and actor position.
    minimize=True (default) minimizes the game window at the end (see game_window), since
    the bridge usually runs unattended; pass minimize=False when a human needs to watch the screen.
    Returns a text report.
    """
    try:
        return process.launch_game(clear_shader_cache, test_map, minimize)
    except Exception as e:
        return f"Launch failed: {e}"


@mcp.tool()
def game_window(action: str) -> str:
    """Minimize, restore or focus the game window. action: "minimize", "restore" or "focus".

    With g_always_active on (set by launch_game) the game keeps running and
    rendering while minimized, so screenshots still work. "focus" restores the
    window and asks Windows to bring it to the foreground (Windows may refuse).
    Returns a short text, also when the game is not running or has no window.
    """
    try:
        return process.game_window(action)
    except Exception as e:
        return f"Window action failed: {e}"


def _engine_call(fn, *args):
    """fn(*args), with the engine exceptions turned into the usual error texts."""
    try:
        return fn(*args)
    except EngineNotRunning:
        return _not_running()
    except EngineTimeout as e:
        return (
            f"Timed out: {e}. The request is still queued and will run when the engine "
            "gets to it (e.g. after a level load); check game_status."
        )
    except EngineError as e:
        return f"Lua error: {e}"


@mcp.tool()
def start_test_map() -> str:
    """Load the debug test map (level fake_start), like pressing F2 in the main menu.

    Works from the main menu (waits up to 60 s for it to be ready) or from a level
    (that game is dropped without saving). Turns off "press any key to continue".
    Blocks until the level is loaded and the actor exists (up to 120 s), then
    returns the level name and actor position.
    """
    return _engine_call(game.start_test_map)


@mcp.tool()
def teleport(x: float, y: float, z: float, level: str | None = None) -> str:
    """Move the actor to world position x, y, z, snapped to the nearest AI node.

    Snapping uses the AI node under (x, z) (y may be well above or below the floor),
    so the actor never ends up inside a wall. Without `level` (or with the current
    level's name) it moves on the current level. With another level name it first
    changes level (no fade, up to 120 s for the load), then snaps to x, y, z there.
    Returns "x y z level_name" of the final position, prefixed by a "Changed level"
    line after a level change. Errors come back as text starting with "error:",
    e.g. no AI node at that spot, or an unknown level (the message lists the
    levels that can be jumped to; the test map fake_start is not one of them).
    """
    return _engine_call(game.teleport, x, y, z, level)


@mcp.tool()
def screenshot(width: int = 1280, height: int = 720) -> list[str | Image]:
    r"""Take a screenshot of the game window (main menu or level) and return it.

    Returns the PNG path (<ANOMALY_DIR>\appdata\screenshots\mcp_<timestamp>.png)
    with its size, plus the image itself. width/height are clamped to the window
    size by the engine. Fails with a text message if no file appears within 5 s
    (screenshots are disabled when HDR10 is on).
    """
    result = _engine_call(game.screenshot, width, height)
    if isinstance(result, str):
        return [result]
    text, png = result
    return [text, Image(path=png)] if png else [text]


@mcp.tool()
def kill_game() -> str:
    """Stop the game: ask it to quit via the console, force-kill after 10 s. Returns a report."""
    try:
        return process.kill_game()
    except Exception as e:
        return f"Kill failed: {e}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
