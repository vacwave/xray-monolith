# anomaly-mcp

MCP server (`anomaly`) that lets Claude talk to a running S.T.A.L.K.E.R. Anomaly
built with `MCP_SERVER`. It is a thin client of the engine's loopback TCP
endpoint; the wire format is in [PROTOCOL.md](PROTOCOL.md).

Tools:
- `game_status`, `lua_eval`, `log_tail`, `kill_game`
- `console(cmd)`: run a console command and return what it printed to the log
  (`"ok (no output)"` when nothing was printed).
- `game_input(action, mode="tap", frames=60)`: press a game action (`kINVENTORY`,
  `kFWD`, ... through the player's key bindings; or a raw `DIK_W` key). Modes `tap`,
  `hold` (for `frames` frames, blocks until they have run), `press`, `release`.
- `crash_report()`: why the game died: the last crash in the newest log (`[error]` lines, Lua
  error and traceback, C++ stack, minidump); works when the game is not running.
- `launch_game(clear_shader_cache=False, test_map=False, minimize=True)`: start the
  game with `-smap2048 -dbg -silent_error_mode` (crashes go to the log, no dialog), set `g_always_active on` and `keypress_on_start off`; with `test_map` also
  load the debug test map; minimizes the window unless `minimize=False`.
- `game_window(action)`: `minimize`, `restore` or `focus` the game window (it keeps
  running and rendering while minimized).
- `start_test_map()`: load the debug test map (level `fake_start`, what F2 in the
  main menu does) and wait for the actor. Turns `keypress_on_start` off.
- `teleport(x, y, z, level=None)`: move the actor, snapped to the AI node under
  (x, z); with another `level` it changes level first.
- `screenshot(width=1280, height=720)`: PNG in `<ANOMALY_DIR>\appdata\screenshots\`,
  returned as path text plus the image.

The multi-step tools call the Lua helpers in `gamedata/scripts/mcp_helper.script`
(through `eval`), so the matching db0 must be deployed.

Environment (all optional): `ANOMALY_DIR` (default `C:\Anomaly`),
`ANOMALY_MCP_PORT` (default `28015`), `ANOMALY_EXE` (default `AnomalyDX11AVX.exe`).

```
uv sync
uv run pytest                 # fake engine, no game needed
uv run python smoke.py        # against the live game (start it first)
uv run python smoke.py --test-map  # also: test map, teleport, screenshot
```

Register with Claude Code:

```
claude mcp add anomaly -- uv run --directory D:\projects\xray-monolith-wpo-mt-mcp\tools\mcp anomaly-mcp
```
