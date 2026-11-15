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
| `game_launch` | `exe` (default `<repo>\_build\_game\bin_dbg\AnomalyDX11AVX.exe`), `game_root` (default `C:\Anomaly`), `show_window`, `new_game`, `extra_args`, `timeout`, `full_dumps`, `hang_timeout`, `load_timeout` |
| `game_attach` | attach to a running `-mcp_port` game by `port` or `appdata` dir |
| `game_status` | pid, alive, exit code, port, `crashed`, `hung` (+ frame stall, loading), `engine.info`, log rate (lines/s), last lines |
| `game_list` | instances of this supervisor and the caps |
| `game_stop` | console `quit`, kill after 30 s (at once when hung), release the slot |
| `game_restart` | stop if running, relaunch with the same exe/args into the same slot and appdata; `new_game` override |
| `game_last_crash` | crash record: plain-words reason, exit code name, engine stack, dump paths, .log tail, last buffered lines |
| `game_dump` | minidump of the live game (all thread stacks, ~100 MB) into `.mcp-run\<id>\dumps` |
| `console_exec` | engine console command → printed lines |
| `lua_exec` | Lua chunk or expression list → `results` + printed lines (`print` goes to the log during the call) |
| `log_tail` | `n` (≤1000), `grep` regex, `since` seq, from the supervisor's 20k-line buffer |
| `game_teleport` | playable session on a map: from the main menu a new game as `faction` (stock kit incl. PDA, put into the PDA slot) on `level`; in game a level change and/or move to `position` [x,y,z]; then `cvars` (console commands). Returns when the actor is in control: level, position, faction, `load_s`, notes. Unknown level/faction → error with the valid list |
| `game_input` | human keyboard/mouse input as an engine-timed script (silent, no focus needed): `steps` list = sequence, nested list = parallel; steps `{"action":"forward","ms":2000}` (no ms = tap, `"state":"down"/"up"`), `{"key":"ESC"}`, `{"text":"..."}`, `{"look":{"yaw":90,"pitch":0},"ms":300}` (degrees), `{"look_at":[x,y,z]}`, `{"look_dir":[x,y,z]}`, `{"mouse":{"dx","dy"}}` (raw px), `{"wheel":N}`, `{"wait":ms}`; `wait` (default true) → `{id, done, elapsed_ms, frames, held}`; `steps=[]` → status. Unknown names → error with the valid list |
| `game_input_release` | release all held keys/buttons and cancel running input sequences (also automatic on supervisor disconnect) |
| `game_perf` | `seconds` window (default 5): fps, frame-time percentiles, hitches, frame cap/limiter, main-thread phases with Lua share, worker-task Lua/GC, memory (see `notes/perf.md`) |
| `game_screenshot` | the presented frame incl. HUD/UI/menus as inline image + JSON metadata; `max_width` (1024, 0 = full), `format` jpeg (`quality` 70) / png, `save_path` writes the full-res frame (.jpg = JPEG q95, else PNG; relative → `.mcp-run/<id>/screenshots/`); errors while a level loads |
| `game_events` | structured events from the 5k buffer, oldest first: `script_error` (kind, fatal, message, file, line, traceback, count, since_last), `level_loaded`/`level_unloaded`, `actor_spawned`/`actor_died` (killer), `game_saved`/`game_loaded`, `menu_opened`/`menu_closed`, `task_given`/`task_completed`/`task_failed`; `since` seq, `types` (fnmatch), `limit` (≤500) |

`game_status` adds `events` (new events / script errors since the last status call); `console_exec`, `lua_exec` and
`game_teleport` add `script_errors` when script errors arrived since the last such call; `game_last_crash` adds the
last fatal `script_error`. Script errors repeating within 5 s are coalesced by the engine (one event per 5 s with
`since_last` = occurrences folded in, `count` = total). Lua errors inside engine callbacks are fatal in this engine.

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

## Crashes and hangs

- In MCP mode the engine never shows a dialog: FATAL/assert/`R_ASSERT`/`Debug.fatal`/CHECK_OR_EXIT exit with code 1,
  unhandled exceptions exit with the exception code (Windows Error Reporting finishes without UI).
  Before exiting it writes its own minidump and sends `{"event":"crash","data":{"reason","stack","dump"}}`.
- The supervisor builds a crash record on a crash event or a nonzero exit it did not cause; dumps (engine `.mdmp`,
  WER `AnomalyDX11AVX.exe.<pid>.dmp`, `game_dump` files) are moved into `.mcp-run\<id>\dumps`.
- WER local dumps catch crashes that bypass the engine (`__fastfail`, heap corruption, stack overflow, replaced
  filters). Run `mcp\setup_wer.ps1` once as admin; it also lets the supervisor switch `full_dumps` per launch.
  The key is global per exe name: the last launch's `full_dumps` choice applies to all running instances.
- Hang: `engine.heartbeat` (answered by the engine's net thread) reports the frame counter and a loading flag; frames
  stalled longer than 30 s (600 s while loading) mark the instance `hung`. A launch with no port file after 120 s
  is dumped and killed.
- Config (`%LOCALAPPDATA%nomaly-mcp\config.json` or env `ANOMALY_MCP_<KEY>`): `hang_timeout_s` 30,
  `load_hang_timeout_s` 600, `dump_keep` 5 (newest dumps per profile, across instances), `dump_cap_gb` 40.
  Deletions are listed in `game_last_crash` / `game_dump` output.
