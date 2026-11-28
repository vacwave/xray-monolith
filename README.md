# X-Ray Monolith MCP

A fork of the S.T.A.L.K.E.R. Anomaly 1.5.3 engine that an AI agent can play. The engine gets a small control server, and a Python [MCP](https://modelcontextprotocol.io) server on top of it gives an agent about 50 tools to launch the game, see it, move, talk, fight, follow quests and read what went wrong. It is made for testing the game and its mods the way a player would, without a person at the keyboard.

Built on the MT version of [themrdemonized's Modded Exes](https://github.com/themrdemonized/xray-monolith) (`dev-mt`), which has the full list of base engine features. Without the `-mcp_port` argument the exe behaves like the normal Modded Exes build.

Works with plain Anomaly, Anomaly with mods through Mod Organizer 2, and GAMMA. Windows only, DX11.

## How it works

```
AI agent (Claude Code or any MCP client)
      |  MCP over stdio
mcp/anomaly_mcp.py        supervisor: launches games, keeps logs and events, builds crash records
      |  TCP on localhost, one JSON object per line
AnomalyDX11AVX.exe        engine started with -mcp_port
```

**Engine side** (`src/xrEngine/mcp_*`, `src/xrGame/mcp_*`)
- `-mcp_port N` starts a localhost-only server for one client (`0` picks a free port and writes it to `<appdata>\mcp_port`). Without the argument nothing is started.
- A network thread only reads and queues requests. Every handler runs on the main thread at a frame boundary, so game, Lua and console calls are as safe as the game's own.
- Requests are `{"id", "method", "params"}`, answers `{"id", "result"}` or `{"id", "error"}`, and the engine pushes `{"event", "data"}` lines on its own: every log line, game events and a crash record.
- Methods cover console and Lua execution, a state snapshot, world and object queries, the UI tree, quest tasks and info portions, AI-map navigation, input scripts, screenshots, time control and performance counters.
- Input is injected inside the engine as timed key, mouse and cursor steps. It needs no window focus and never moves the real Windows cursor.
- An MCP run is silent: the window starts minimized, takes no focus, keeps rendering and does not pause when it is in the background. `-showwindow` gives a normal window.
- In MCP mode the engine never shows a dialog. A fatal error writes a minidump, sends the reason and stack to the supervisor and exits.
- `-appdata <dir>` gives each instance its own saves, logs, `user.ltx` and shader cache, so several games run side by side and the real install is not modified.

**Supervisor side** (`mcp/anomaly_mcp.py`)
- Starts the game directly or through Mod Organizer 2 (it reads the MO2 profile and mod list, never changes them), waits for the port and connects.
- Keeps a 20,000-line log buffer and a game event buffer per instance. They survive a crash of the game, so the agent can still read what happened.
- Detects crashes and hangs (frame counter heartbeat), collects dumps and returns a plain-words crash record.
- Limits how many games run at once on the machine (3 vanilla, 1 GAMMA by default) and kills its games when it exits.
- Builds the high-level tools from the engine methods: walking a path with doors and enemies on the way, aiming and shooting with real ballistics, holding a conversation, clicking UI widgets by their text, waiting for a condition.

## Tools

| Group | Tools |
|---|---|
| Start and stop | `game_launch`, `game_attach`, `game_list`, `game_status`, `game_restart`, `game_stop` |
| Look | `game_state`, `game_screenshot`, `game_screenshot_burst`, `game_objects`, `game_object`, `game_ui`, `game_nav`, `game_nav_map` |
| Move | `game_move_to`, `game_changelevel`, `game_teleport` |
| Interact | `game_use`, `game_talk`, `game_ui_click`, `game_input`, `game_input_release` |
| Fight | `game_shoot`, `game_hunt`, `game_clear_area`, `game_guard` |
| Quests and events | `game_tasks`, `game_infos`, `game_events`, `game_wait` |
| Scenario setup | `game_spawnable`, `game_spawn`, `game_give`, `game_actor`, `game_kill`, `game_release`, `game_weather`, `game_emission`, `game_time`, `game_step`, `game_save`, `game_load`, `game_saves` |
| Diagnose | `log_tail`, `game_perf`, `game_lua_profile`, `game_last_crash`, `game_dump` |
| Last resort | `console_exec`, `lua_exec` |

A typical session:

```
game_launch(game_root="C:\\Anomaly")
game_teleport(level="l01_escape", faction="stalker")     # new game, returns when the actor is in control
game_objects(kinds=["stalker"], radius=50)               # who is around
game_use(id=1234)                                        # walk up and talk
game_talk(n=1)                                           # pick a dialogue option
game_wait(until={"event": "task_given"}, fail_on=["script_error"])
game_tasks()                                             # stage and target of the new task
game_hunt(task="<task id>")                              # go there and fight
game_events(types=["script_error"])                      # did any script break
game_stop()
```

God mode, infinite magazine and no recoil are on by default so a test does not die halfway; `game_launch(cheats=False)` turns them off.

Documentation:
- [`mcp/TESTER_GUIDE.md`](mcp/TESTER_GUIDE.md): how to test with the tools, which tool for what, and the gotchas.
- [`mcp/TOOLS.md`](mcp/TOOLS.md): every argument and result field of every tool.
- [`mcp/README.md`](mcp/README.md): the supervisor in detail (launching, MO2, instance slots, options, crashes, hangs, dumps).

## Setup

1. **Build the engine.** Visual Studio 2022 with MFC and ATL. Run `git submodule update --init`, open `src\engine-vs2022.sln` and build `DX11-AVX`. The output is `_build\_game\bin_dbg\AnomalyDX11AVX.exe`.
2. **Deploy it.** Copy the exe (and the `.pdb` for readable crash stacks) to `<Anomaly>\bin_mcp\`. The game's own `bin` folder stays untouched; the supervisor copies the DLLs it needs from there on the first launch.
3. **Gamedata.** If `gamedata\` differs from your installed Modded Exes, pack it with `compressor\xrCompress.exe` (see `compressor\! compress_gamedata.cmd`) and put it in `<Anomaly>\db\mods\00_modded_exes_gamedata.db0`.
4. **Install the supervisor's dependency.** Python 3.11 or newer:
   ```
   py -3.11 -m pip install "mcp>=2.3,<3"
   ```
5. **Register the MCP server.** The repo's `.mcp.json` registers it as `anomaly` for Claude Code when you start it from the repo root. For another client:
   ```json
   { "mcpServers": { "anomaly": { "command": "py", "args": ["-3.11", "<repo>/mcp/anomaly_mcp.py"] } } }
   ```
6. **Optional:** run `mcp\setup_wer.ps1` once as admin so Windows keeps a dump for crashes that bypass the engine's own handler.

Paths the supervisor assumes:

| Path | Meaning |
|---|---|
| `C:\AnomalyMO2` | Default `game_root`: a Mod Organizer 2 folder whose game path is the Anomaly install. |
| `C:\Anomaly` | Plain Anomaly without mods. |
| `C:\GAMMA` | GAMMA's Mod Organizer folder. It counts against the GAMMA limit and gets the option overrides in `mcp\templates\options_overrides_gamma.ltx`. |
| `<game path>\bin_mcp\AnomalyDX11AVX.exe` | Default exe. |
| `<repo>\.mcp-run\<instance>\` | Each instance's appdata (saves, logs, options), screenshots and dumps. |

The first three are defaults in `mcp/anomaly_mcp.py`. Any other install works by passing `game_root` (and `exe`) to `game_launch`.

Every launch starts from `mcp\templates\user.ltx`, a low-settings config capped at 30 fps. For a performance test, uncap it first with `r__framelimit 0`.

## Using the engine server without the supervisor

Start the game with `-mcp_port 5000` and talk to it with any TCP client, one JSON object per line:

```
> {"id":1,"method":"engine.info"}
< {"id":1,"result":{"protocol_version":1,"version":"X-Ray Monolith v1.5.3 (...)","mt":true,"level":"l01_escape", ...}}
> {"id":2,"method":"console.exec","params":{"cmd":"g_god on"}}
< {"id":2,"result":{"lines":[...],"truncated":0}}
< {"event":"log","data":{"seq":812,"line":"..."}}
```

New methods are added in C++ with `mcp::register_method(name, handler)` (`src/xrEngine/mcp_server.h`); events are sent with `mcp::send_event`.

## Limits

- A Lua error inside a game callback is fatal in this engine, so careless `lua_exec` code can crash the game.
- Ladders are not on the AI map: `game_move_to` cannot climb them.
- The Mod Organizer launch is refused while another Mod Organizer is open.
- One GAMMA instance needs about 10 GB of RAM and 7 GB of VRAM; plain Anomaly about 4 GB and 1.6 GB.
- Only the DX11 renderer and the MT engine are supported.

## Branches

- `dev-mt-mcp`: this branch, the MCP engine.
- `dev-mt-mcp-dlss`: the same with DLSS and FSR frame generation.
- `dev-mt-dlss`: DLSS and FSR frame generation alone, without MCP, with player releases.

Pull requests in this repository are kept as a reference for individual engine fixes.

## Credits and licence

GSC Game World (X-Ray engine, S.T.A.L.K.E.R.), the Anomaly team, themrdemonized and contributors (Modded Exes / xray-monolith), OpenXRay and IX-Ray. Uses [nlohmann/json](https://github.com/nlohmann/json) (MIT) and the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk). See `License.txt` for the engine's licence terms.
