"""MCP server 'anomaly': tools to inspect and drive a running Anomaly instance.

Sync tools; the MCP SDK runs them in worker threads, so a slow call does not
block other tool calls.
"""

from collections import deque

from mcp.server.mcpserver import MCPServer

from . import config, process
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
def launch_game(clear_shader_cache: bool = False) -> str:
    """Start the game (<ANOMALY_DIR>\bin\<ANOMALY_EXE> -smap2048 -dbg) and wait for its endpoint.

    Refuses if it is already running. clear_shader_cache=True deletes
    appdata\shaders_cache first (needed after rendering/shader changes).
    Blocks until the endpoint answers status (about 90 s at most) and returns a
    text report; the game is then at the main menu.
    """
    try:
        return process.launch_game(clear_shader_cache)
    except Exception as e:
        return f"Launch failed: {e}"


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
