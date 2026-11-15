# STATE
Scope: MT only (`dev-mt-mcp`; non-MT dropped). Agent ⇄ Python MCP supervisor (`mcp/anomaly_mcp.py`, server `anomaly` via `.mcp.json`) ⇄ TCP JSON-lines ⇄ engine (`-mcp_port`).

Done (all pushed): 0 notes · 1 IPC server 757d973c · 2 multi-instance 5ac6faea · 3 log streaming 6045689a · 4 silent runs 8795fb14 · 5 console/lua exec ca66329c · 6 supervisor dd980ff1 · 7 crash capture + hang detection + WER dumps 08b0cd0e · 8 game_teleport f74782d1 · 9 game_perf 9f0f7f34 · 11 game_events e08dd083 · 12 game_screenshot eb4a5914.

Queue (planned 2026-11-15, running unattended since user go-ahead). User priority: analysing Lua (mods) choking the main thread.
- In flight (2 parallel devs, worktrees, unattended): 10 Lua profiler, 13 input. Devs test from `C:\Anomaly\bin_g<N>`, never `C:\Anomaly\bin`; coordinator does the final main-clone build + C:\Anomaly\bin smoke after the last goal.
- 13 input: actions press/hold/release (`level.press_action`), raw keys, mouse-look in C++, no focus. 14 `game_state` (actor, level, time, UI state). 15 world queries: objects/NPCs/items within radius param (id, section, pos, dist, faction, alive/hostile) + look-at target.
- 16 save/load (`game_save`/`game_load`, returns when in control; covers the register_object crash). 17 spawn/debug: items, NPCs/enemies/squads, give item, weather, time, emission/psi-storm. 18 time control: pause, step N frames, time_factor. 19 UI interaction (cursor move/click, top window). 20 wait/sync (`wait_for`).
- 21 nav: AI-map feedback first (is position on navmesh, nearest node, path exists/length, optional overlay in screenshot), then maybe `move_to`.
Deferred indefinitely: PyInstaller single exe (user runs on own machine only).

Setup: `py -3.11 -m pip install "mcp>=2.3,<3"`; WER LocalDumps installed via `mcp/setup_wer.ps1` (admin, done 2026-11-15).
Caps 3 vanilla / 2 GAMMA. Vanilla ≈4.1 GB RAM / 1.6 GB VRAM. Template user.ltx has r__framelimit 30 (uncap for FPS tests).
Open issues: Lua errors inside engine→Lua callbacks are fatal by engine design (event sent before the crash; MCP-only non-fatal mode would be its own goal — ask user); mod-printed errors (`printe`) not captured; loading saves `1`/`bench_g1` crashes in `CALifeSimulatorBase::register_object` (save/mod mismatch, also with -showwindow); one unreproduced startup hang before logs dir (launch timeout now auto-dumps); long level-load threshold untested on a multi-minute load.
Testing: new game via `game_launch(new_game=true)`; 180 s timeouts; kill leftover AnomalyDX11AVX after runs.
