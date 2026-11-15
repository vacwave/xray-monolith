# STATE
Scope: MT only (`dev-mt-mcp`; non-MT dropped). Agent ⇄ Python MCP supervisor (`mcp/anomaly_mcp.py`, server `anomaly` via `.mcp.json`) ⇄ TCP JSON-lines ⇄ engine (`-mcp_port`). Tool list: `mcp/README.md`.

Done (all pushed): 0 notes · 1 IPC server 757d973c · 2 multi-instance 5ac6faea · 3 log streaming 6045689a · 4 silent runs 8795fb14 · 5 console/lua exec ca66329c · 6 supervisor dd980ff1 · 7 crash capture + hang detection + WER dumps 08b0cd0e · 8 game_teleport f74782d1 · 9 game_perf 9f0f7f34 · 10 game_lua_profile e860cd26 · 11 game_events e08dd083 · 12 game_screenshot eb4a5914 · 13 game_input 90926fd9 · 14 game_state 1dc9bfc9 · 15 game_objects 1ba9778d · 16 game_save/load/saves a60c8aeb · 17 scenario tools (spawn/give/actor/kill/release/weather/emission) 93cd110e · 18 game_time/game_step 8ec065b0 · 19 game_ui/game_ui_click + virtual cursor b51defdd · 20 game_wait 59826e8b · 21 game_nav/game_nav_map/game_move_to d4828be5 · 22 shutdown fixes (precache light, logger pop race) 698808a2.
Final build 2026-11-16: DX11-AVX clean at 698808a2, exe+pdb copied to `C:\Anomaly\bin`; smoke passed (launch, teleport new game, wait, state, screenshot, objects, input, perf, lua profile, nav, spawn, pause/step, inventory UI, save/load, release, events: 0 script errors, graceful quit exit 0, no real-cursor jumps).

Queue: empty.
NEXT STEP: planning session WITH the user (do not start goals) for story/mod-authoring support: dialogue read/choose, info portions + task/xr_logic state, triggers/restrictors/level changers, NPC/squad routing checks, scenario runner with assertions, gamedata deploy/reload loop; decide vanilla vs GAMMA target and engine tools vs Lua helper lib in gamedata. Try one real quest end to end with existing tools first to find the gaps.
Deferred indefinitely: PyInstaller single exe (user runs on own machine only).
Parallel-dev practice that worked: 2 devs in worktrees, each testing from its own `C:\Anomaly\bin_g<N>` copy and its own scratch subdir, new code in new files + one contiguous block in `anomaly_mcp.py`, dev rebases before hand-back, coordinator ff-merges/cherry-picks. Worktrees need the untracked `optick-git` sources copied in.

Setup: `py -3.11 -m pip install "mcp>=2.3,<3"`; WER LocalDumps installed via `mcp/setup_wer.ps1` (admin, done 2026-11-15).
Caps 3 vanilla / 2 GAMMA. Vanilla ≈4.1 GB RAM / 1.6 GB VRAM. Template user.ltx has r__framelimit 30 (uncap for FPS tests).
Open issues (candidates for the planning session):
- Ask user: MCP-only non-fatal mode for Lua errors inside engine→Lua callbacks (fatal by engine design; event is sent before the crash). Mod-printed errors (`printe`) are not captured as events; no NPC-death event type.
- Shutdown: one unreproduced double free in `CModelPool::Registry` at exit (dump 18664; suspected unlocked race, upstream MT code; 0 in 70 quits since). One unreproduced startup hang before the logs dir (launch timeout auto-dumps).
- First screenshot right after `game_teleport`/`in_control` can still show the loading screen (precache frames); `game_move_to` gets stuck at closed doors (Sidorovich bunker); `-showwindow` + focused-window input/cursor ownership never tested live; GAMMA untested for all tools; user's saves `1`/`bench_g1` are GAMMA saves (need GAMMA data).
- Cleanup pending user OK: `C:\Anomaly\bin_g8`…`bin_g22`, `bin_g22_base` test copies; leftover dir `.claude/worktrees/agent-a0f8787c3e74f35f6` (locked file, branch already merged).
Testing: `game_teleport` for a new game on a level; only stop game PIDs you launched (user may run their own game).
