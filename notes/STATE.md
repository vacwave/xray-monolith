# STATE
Scope: MT only (`dev-mt-mcp`; non-MT dropped). Agent ⇄ Python MCP supervisor (`mcp/anomaly_mcp.py`, server `anomaly` via `.mcp.json`) ⇄ TCP JSON-lines ⇄ engine (`-mcp_port`).

Done (all pushed): 0 notes · 1 IPC server 757d973c · 2 multi-instance 5ac6faea · 3 log streaming 6045689a · 4 silent runs 8795fb14 · 5 console/lua exec ca66329c · 6 supervisor dd980ff1 · 7 crash capture + hang detection + WER dumps 08b0cd0e · 8 game_teleport f74782d1 · 9 game_perf 9f0f7f34 · 10 game_lua_profile e860cd26 · 11 game_events e08dd083 · 12 game_screenshot eb4a5914 · 13 game_input 90926fd9 · 14 game_state 1dc9bfc9 · 15 game_objects 1ba9778d · 16 game_save/load/saves a60c8aeb · 17 scenario tools (spawn/give/actor/kill/release/weather/emission) 93cd110e.

Queue (planned 2026-11-15, running unattended since user go-ahead). User priority: analysing Lua (mods) choking the main thread.
- In flight (2 parallel devs, worktrees, unattended): 18 time control, 19 UI interaction. Devs test from `C:\Anomaly\bin_g<N>`, never `C:\Anomaly\bin`; coordinator does the final main-clone build + C:\Anomaly\bin smoke after the last goal.
- 18 time control: pause, step N frames, time_factor. 19 UI interaction (cursor move/click, top window; known: CUICursor follows the real Windows cursor when window >= screen size and SetUICursorPosition moves the user's real cursor — needs an MCP virtual cursor). 20 wait/sync (`wait_for`).
- 21 nav: AI-map feedback first (is position on navmesh, nearest node, path exists/length, optional overlay in screenshot), then maybe `move_to`.
After goal 21: planning session WITH the user (do not start goals) for story/mod-authoring support: dialogue read/choose, info portions + task/xr_logic state, triggers/restrictors/level changers, NPC/squad routing checks, scenario runner with assertions, gamedata deploy/reload loop; decide vanilla vs GAMMA target and engine tools vs Lua helper lib in gamedata. Try one real quest end to end with existing tools first to find the gaps.
Deferred indefinitely: PyInstaller single exe (user runs on own machine only).

Setup: `py -3.11 -m pip install "mcp>=2.3,<3"`; WER LocalDumps installed via `mcp/setup_wer.ps1` (admin, done 2026-11-15).
Caps 3 vanilla / 2 GAMMA. Vanilla ≈4.1 GB RAM / 1.6 GB VRAM. Template user.ltx has r__framelimit 30 (uncap for FPS tests).
Open issues: Lua errors inside engine→Lua callbacks are fatal by engine design (event sent before the crash; MCP-only non-fatal mode would be its own goal — ask user); mod-printed errors (`printe`) not captured; user's saves `1`/`bench_g1` are GAMMA saves (missing sections on vanilla; now a clear fatal + game_load reports missing_sections); one unreproduced startup hang before logs dir (launch timeout now auto-dumps); long level-load threshold untested on a multi-minute load.
Testing: new game via `game_launch(new_game=true)`; 180 s timeouts; kill leftover AnomalyDX11AVX after runs.
