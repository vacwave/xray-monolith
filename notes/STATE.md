# STATE
Scope: MT only (`dev-mt-mcp`; non-MT dropped). Agent ⇄ Python MCP supervisor (`mcp/anomaly_mcp.py`, server `anomaly` via `.mcp.json`) ⇄ TCP JSON-lines ⇄ engine (`-mcp_port`).

Done (all pushed): 0 notes · 1 IPC server 757d973c · 2 multi-instance 5ac6faea · 3 log streaming 6045689a · 4 silent runs 8795fb14 · 5 console/lua exec ca66329c · 6 supervisor dd980ff1 · 7 crash capture + hang detection + WER dumps 08b0cd0e · 8 game_teleport f74782d1 · 9 game_perf 9f0f7f34 · 11 game_events e08dd083 · 12 game_screenshot eb4a5914 · 13 game_input 90926fd9 · 14 game_state 1dc9bfc9 · 15 game_objects 1ba9778d.

Queue (planned 2026-11-15, running unattended since user go-ahead). User priority: analysing Lua (mods) choking the main thread.
- In flight (2 parallel devs, worktrees, unattended): 10 Lua profiler, 16 save/load. Devs test from `C:\Anomaly\bin_g<N>`, never `C:\Anomaly\bin`; coordinator does the final main-clone build + C:\Anomaly\bin smoke after the last goal.
- 16 save/load (`game_save`/`game_load`, returns when in control; covers the register_object crash). 17 spawn/debug: items, NPCs/enemies/squads, give item, weather, time, emission/psi-storm. 18 time control: pause, step N frames, time_factor. 19 UI interaction (cursor move/click, top window; known: CUICursor follows the real Windows cursor when window >= screen size and SetUICursorPosition moves the user's real cursor — needs an MCP virtual cursor). 20 wait/sync (`wait_for`).
- 21 nav: AI-map feedback first (is position on navmesh, nearest node, path exists/length, optional overlay in screenshot), then maybe `move_to`.
Deferred indefinitely: PyInstaller single exe (user runs on own machine only).

Setup: `py -3.11 -m pip install "mcp>=2.3,<3"`; WER LocalDumps installed via `mcp/setup_wer.ps1` (admin, done 2026-11-15).
Caps 3 vanilla / 2 GAMMA. Vanilla ≈4.1 GB RAM / 1.6 GB VRAM. Template user.ltx has r__framelimit 30 (uncap for FPS tests).
Open issues: Lua errors inside engine→Lua callbacks are fatal by engine design (event sent before the crash; MCP-only non-fatal mode would be its own goal — ask user); mod-printed errors (`printe`) not captured; loading saves `1`/`bench_g1` crashes in `CALifeSimulatorBase::register_object` (save/mod mismatch, also with -showwindow); one unreproduced startup hang before logs dir (launch timeout now auto-dumps); long level-load threshold untested on a multi-minute load.
Testing: new game via `game_launch(new_game=true)`; 180 s timeouts; kill leftover AnomalyDX11AVX after runs.
