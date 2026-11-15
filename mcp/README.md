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
| `game_launch` | `exe` (default `<repo>\_build\_game\bin_dbg\AnomalyDX11AVX.exe`), `game_root` (default `C:\Anomaly`), `show_window`, `new_game`, `extra_args`, `timeout`, `full_dumps`, `hang_timeout`, `load_timeout`, `load` (start straight into a save already in the slot's savedgames; waits until the actor is in control, result `load` as game_load) |
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
| `game_save` | `name` (cleaned to letters/digits/`_-.`, overwrites) → saved in the instance's appdata `savedgames`; returns when on disk: file, size, parts (scop/scoc/dds), level, position, money, `save_s`. Console save: UI vetoes (campfire-only, Agony/timer, emission/combat) do not apply. Errors at the main menu, while loading, actor dead |
| `game_load` | `name`, `timeout` (600) → from the main menu or a running game (also dead: retry after death); returns when the actor is in control: level, position, money, `load_s`, via. Missing save → error with the list; game died → `loaded:false` + crash record + `missing_sections` (save made with mods this game lacks); timeout → error, the load goes on |
| `game_saves` | the instance's saves, newest first: name, size, mtime, level/game_time/actor_health (`details`, game running); `import_from` a `.scop` path (copied with .scoc/.dds, source untouched, optional `import_as`); `delete` a save in the instance's own savedgames |
| `game_input` | human keyboard/mouse input as an engine-timed script (silent, no focus needed): `steps` list = sequence, nested list = parallel; steps `{"action":"forward","ms":2000}` (no ms = tap, `"state":"down"/"up"`), `{"key":"ESC"}`, `{"text":"..."}`, `{"look":{"yaw":90,"pitch":0},"ms":300}` (degrees), `{"look_at":[x,y,z]}`, `{"look_dir":[x,y,z]}`, `{"mouse":{"dx","dy"}}` (raw px), `{"wheel":N}`, `{"wait":ms}`, UI cursor `{"cursor":[x,y]}` / `{"click":[x,y],"button","double","hover"}` / `{"drag":{"from","to"},"ms"}` / `{"wheel":N,"at":[x,y]}` / `{"cursor":"release"}` (pixels of the last screenshot; `"space":"ui"|"norm"`; never moves the real Windows cursor); `wait` (default true) → `{id, done, elapsed_ms, frames, held}`; `steps=[]` → status. Unknown names → error with the valid list |
| `game_ui` | what is on screen, to click by meaning: visible widgets of the top dialog (`root` top/menu/game/hud/all), `view` interactive (default) / text / tree / all; per node `path`, `kind` (button, check, tab, edit, track, combo, list_item, cell, question, menu_item, static, ...), text (or `label`), state (checked, active, selected, value, scroll, hover, enabled), `item` for inventory/trade cells, rect/center in pixels of the last `game_screenshot` image; `talk` {phrases, options}; filters `text` (also item names), `name`, `kind`, `limit` (80) |
| `game_ui_click` | click a widget by `text` / `name` / `path` / talk `option` (1-based): cursor to its centre, click through the normal mouse path (`button` left/right/middle, `double`); ambiguous → candidates; returns the clicked node and the resulting mode/dialog |
| `game_input_release` | release all held keys/buttons and cancel running input sequences (also automatic on supervisor disconnect) |
| `game_state` | cheap structured snapshot: `mode` (main_menu, loading, in_game, in_game_menu, dialog, dead, paused, tutorial, console, hung), `ui` (dialog kind/class/script class+mode/partner, cursor, input receiver, actor_control), `actor` (position, yaw/pitch, vertices, health/stamina/radiation/bleeding/psy/satiety/thirst/sleepiness, move/speed, weight, money, community, rank, god mode, outfit), `hands` (weapon ammo/mag/type/spare, state, fire mode, zoom, jam; torch, NVG, detector), `target` (crosshair object, distance, relation), `world` (level, game time, weather, time factors, surge, psi storm, cover); optional `sections` `inventory`, `tasks`, `stats` or `["all"]`. While a level loads it answers `loading` from the heartbeat without waiting |
| `game_objects` | objects around the actor (or `position`), nearest first: `radius` (100 m), `kinds` (stalker, monster, corpse, item, weapon, ammo, artefact, anomaly, campfire, container, door, physic, vehicle, ladder, level_changer, smart_terrain, restrictor, cover, squad, other + aliases npc/box/zone/loot/creature/all), `pattern` glob, `alive`, `hostile_only`, `visible_only`, `los` (≤64 rays), `limit` (30, cap 100), `summary` (counts only), `offline` (alife entities without a client object); per object id/kind/section/name/display/position/distance/bearing/elevation + kind fields; crosshair `target` |
| `game_object` | one object by `id` in full detail: game_objects fields + los, inventory summary/money/active item (NPCs, corpses), container contents, zone power; offline server entity if not online |
| `game_time` | MCP pause / resume (`pause`), simulation speed (`sim_speed` = engine time_factor 0.01..100) and game clock rate (`clock_rate` = level time factor, in-game s per sim s); returns pause flags, frame, sim time, game time, rates. Pause/speed reset when the supervisor disconnects (see `notes/time.md`) |
| `game_step` | while paused, advance `frames` or `ms` of simulation at a fixed `dt_ms` (default 20) and pause again; optional `input` (game_input steps, ms = simulation ms), `screenshot`, `state`: the agent loop in one call |
| `game_perf` | `seconds` window (default 5): fps, frame-time percentiles, hitches, frame cap/limiter, main-thread phases with Lua share, worker-task Lua/GC, memory (see `notes/perf.md`) |
| `game_screenshot` | the presented frame incl. HUD/UI/menus as inline image + JSON metadata; `max_width` (1024, 0 = full), `format` jpeg (`quality` 70) / png, `save_path` writes the full-res frame (.jpg = JPEG q95, else PNG; relative → `.mcp-run/<id>/screenshots/`); errors while a level loads |
| `game_events` | structured events from the 5k buffer, oldest first: `script_error` (kind, fatal, message, file, line, traceback, count, since_last), `level_loaded`/`level_unloaded`, `actor_spawned`/`actor_died` (killer), `game_saved`/`game_loaded`, `menu_opened`/`menu_closed`, `task_given`/`task_completed`/`task_failed`; `since` seq, `types` (fnmatch), `limit` (≤500) |
| `game_lua_profile` | `seconds` window (default 5), `top` rows, `mode` time/calls/alloc: which Lua costs time, per main/worker thread — exact engine→Lua entry points (ms, calls, max), sampled functions (self/total ms, % of Lua and frame), files, `RegisterScriptCallback` callbacks with their listeners, the 5 worst frames' top contributors, GC/heap, profiler overhead; `calls` adds exact call counts (JIT off), `alloc` allocation KB/frame per function/file (JIT off) |
| `game_spawnable` | spawnable sections from the debug spawner lists (`ui_debug_main.get_spawn_table`, mods included): no filter → categories/counts, aliases, squad factions; `kind` (item, weapon, ammo, outfit, artefact, monster, stalker, npc, squad, monster_squad, stalker_squad or a category key/glob), `pattern` glob/substring, `faction` (squads), `limit` (200) |
| `game_spawn` | `section`, `count` (1..50), `position` [x,y,z] or `distance` ahead (item 2 m, creature 15, squad 25; blocked view → shortened/turned), snapped to the AI map; items, monsters, stalkers, squads (created like the debug spawner: members, sim target), anomalies, vehicles, physics; `relation` enemy/neutral/friend (squads/stalkers); `wait` s for online → ids, members, positions, online. Unknown section → error with similar names |
| `game_give` | `section`, `count` (ammo: boxes) or `rounds` (exact, split into boxes), `equip` (to its slot; weapon made active), `remove` (take up to `count` from the actor, 0 = all) → item ids |
| `game_actor` | `heal`, `health`/`stamina`/`radiation`/`satiety`/`psy_health` (0..1, absolute), `bleeding` 0, `money` → before/after (+ note when god mode blocks a change); god mode / unlimited ammo = `console_exec` `g_god on` / `g_unlimitedammo on` |
| `game_kill` | `ids` (creatures or squads → online members), `by_actor` → engine kill path (death callbacks, corpses, squad bookkeeping); offline → use release |
| `game_release` | `ids` and/or `spawned` (everything game_spawn made in this game process; ids checked against their section) → alife_release (squads with members) |
| `game_weather` | no args → current + valid weathers (cycles {cycle: [presets]}, other files); `weather` cycle/preset via level_weathers (sticks for a weather period) or any weather file; `time` 'HH:MM' (advances the clock), `advance_hours` → before/after weather, cycle, preset, time, day |
| `game_wait` | block until `until` holds (at once if it already does), `timeout_s` (30): readiness (`in_control`, `loaded`, `main_menu`), `mode`, `dialog` open/closed, next `event` (type glob + `where` fields, `since` seq), `log` regex, `sim_ms` / `frames` / `game_clock`, `state` path ops, `has_item`, `object` (exists/gone/alive/dead, within R, field ops), `count` of a game_objects filter, `lua` predicate, `all`/`any`/`not`; `fail_on` list (crash/exit always ends it) → `met`, `failed_on`, `timed_out`, elapsed s/frames/sim ms/game s, `conditions` with values (evidence or last observed), `event_seq`/`log_seq` for chaining. Events/log wake it at once; state/object/Lua conditions are polled (`poll_ms` 50, one batched frame). sim_ms/game_clock while MCP-paused are refused |
| `game_emission` | `kind` surge/psi_storm, `action` start/stop/status via surge_manager / psi_storm_manager (real scripted effects) → before/after started, elapsed_s, duration, weather_fx, time_factor; skip reasons in notes |

`game_status` adds `events` (new events / script errors since the last status call); `console_exec`, `lua_exec` and
`game_teleport` add `script_errors` when script errors arrived since the last such call; `game_last_crash` adds the
last fatal `script_error`. Script errors repeating within 5 s are coalesced by the engine (one event per 5 s with
`since_last` = occurrences folded in, `count` = total). Lua errors inside engine callbacks are fatal in this engine.

`game_wait` examples (`until` values):

- `"in_control"` or `{"in_control": "l02_garbage"}` — after a teleport/load issued elsewhere
- `{"event": "task_given", "where": {"id": "my_task"}}` with `fail_on: ["script_error", "actor_died"]`; next wait `since` = returned `event_seq`
- `{"state": "actor.position", "within": 2, "of": [-100, 0.4, 12]}`, `{"state": "hands.item.ammo", "eq": 0}`
- `{"dialog": "inventory"}` / `{"dialog": "talk", "open": false}`
- `{"count": {"kinds": ["monster"], "radius": 50, "alive": true, "hostile_only": true}, "eq": 0}`, `{"object": 1234, "is": "dead"}`
- `{"any": [{"lua": "db.actor:money() >= 5000"}, {"log": "Quest .* done"}, {"sim_ms": 10000}]}`

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
