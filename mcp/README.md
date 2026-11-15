# anomaly MCP supervisor

MCP server (stdio) that launches and drives X-Ray Monolith game instances for AI agents:
agent ⇄ MCP stdio ⇄ `anomaly_mcp.py` ⇄ TCP JSON lines ⇄ engine (`-mcp_port`). It keeps running when a game crashes.

## Setup

Python 3.11+ and the MCP SDK (2.x, `MCPServer`, formerly FastMCP):

```
py -3.11 -m pip install "mcp>=2.3,<3"
```

## Run and register

`.mcp.json` at the repo root registers the server as `anomaly` for Claude Code:

```json
{ "mcpServers": { "anomaly": { "command": "py", "args": ["-3.11", "mcp/anomaly_mcp.py"] } } }
```

The script path is relative: start Claude Code from the repo root (or a worktree root, which has its own copy),
and approve the project server when asked. Each worktree session runs its own supervisor; the instance caps are
shared machine-wide. Manual run: `py -3.11 mcp/anomaly_mcp.py` (speaks MCP on stdin/stdout; logs to stderr).

## Tools

All tools take an optional `instance` (default: the last launched or attached one).

| Tool | What it does |
|---|---|
| `game_launch` | `exe` (default `<repo>\_build\_game\bin_dbg\AnomalyDX11AVX.exe`), `game_root` (default `C:\Anomaly`), `show_window`, `new_game`, `extra_args`, `timeout` |
| `game_attach` | attach to a running `-mcp_port` game by `port` or `appdata` dir |
| `game_status` | pid, alive, exit code, port, `engine.info`, log rate (lines/s), last lines |
| `game_list` | instances of this supervisor and the caps |
| `game_stop` | console `quit`, kill after 30 s, release the slot |
| `console_exec` | engine console command → printed lines |
| `lua_exec` | Lua chunk or expression list → `results` + printed lines (`print` goes to the log during the call) |
| `log_tail` | `n` (≤1000), `grep` regex, `since` seq, from the supervisor's 20k-line buffer |

Output safeguards: consecutive identical lines are collapsed to `line ×N`, long lines are clipped, each result is
capped at 20k chars, and `omitted` says what was left out. The engine itself returns at most 500 lines per exec
(`truncated` count); the full stream is still in `log_tail`.

## Launching

- Profile: `gamma` when `game_root` is `C:\GAMMA` (GAMMA/MO2 launching is not handled), else `vanilla`.
- Machine-wide caps: 3 vanilla, 2 GAMMA (~4 GB RAM each). Slots are lock files in
  `%LOCALAPPDATA%\anomaly-mcp\slots\`, held while the game runs and freed by the OS if the supervisor dies.
  Override with `ANOMALY_MCP_MAX_VANILLA` / `ANOMALY_MCP_MAX_GAMMA` or `%LOCALAPPDATA%\anomaly-mcp\config.json`
  (`{"max_vanilla": 3, "max_gamma": 2}`).
- Instance id = slot (`vanilla-1`, ...). Its appdata is `<repo>\.mcp-run\<id>\appdata` (logs, user.ltx, saves).
- Every launch copies `mcp/templates/user.ltx` (a low-settings baseline with **`r__framelimit 30`**; set
  `r__framelimit 0` via `console_exec` for FPS tests) and seeds `shaders_cache` from `<game_root>\appdata` once.
- DLLs in `<game_root>\bin` missing next to the exe are copied there first.
- Args: `-fsltx <game_root>\fsgame.ltx -appdata <dir> -mcp_port 0` (+ `-showwindow`, + `-start server(all/single/alife/new) client(localhost)` for `new_game`).
  Runs are silent (minimized, no focus steal) unless `show_window`.
- A game that exits with code 0 within seconds was blocked by the presence mutex (same exe dir and appdata
  already running) or failed to start; `game_launch` reports it with the log tail.
- Launched games are in a job object: they are killed if the supervisor exits; on a clean exit (stdin closed) the
  supervisor quits them gracefully first. Attached games are left alone.
