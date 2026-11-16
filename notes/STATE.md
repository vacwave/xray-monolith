# STATE
Scope: MT only (`dev-mt-mcp`; non-MT dropped). Agent ⇄ Python MCP supervisor (`mcp/anomaly_mcp.py`, server `anomaly` via `.mcp.json`) ⇄ TCP JSON-lines ⇄ engine (`-mcp_port`). Tool list: `mcp/README.md`.

Done (all pushed): goals 0-22 (IPC, supervisor, crash capture, all game_* tools, shutdown fixes; exe 698808a2 in `C:\Anomaly\bin`) · 23 quest probe a9b7d4f5 (vanilla + GAMMA quest end to end, notes only; facts in `gamma.md`, `gamestate.md`, `scenario.md`, `ui.md`, `world.md`) · supervisor tweaks 2026-11-16: shader cache cleared every launch (vanilla launch 6 s / new game 13 s, no visible cost), GAMMA cap 1, game_give equip slot fix (ini+1).
User decisions 2026-11-16: target BOTH vanilla and GAMMA; GAMMA uses the same template user.ltx, default keys; GAMMA reload needs MO2 mod "Reload Unjam Same Key Fix" (enabled, verified); kill our own MO2 as soon as the game PID exists (tested 3/3 OK).

Queue:
- 24 (IN PROGRESS, dev running, user OK 2026-11-16; single game at a time is the expected use, so no launch serialization / VRAM budget): `game_launch(game_root=C:\GAMMA)` via `ModOrganizer.exe -p "G.A.M.M.A" run <exe> -a "<args>"`, game = MO2's child, kill only our MO2, slot + job + restart, re-mute after every level load, measure GAMMA VRAM with the template; AND MCP runs must stop rewriting the user's `axr_options.ltx` (`C:\Anomaly\gamedata\configs\` and GAMMA's `mods\G.A.M.M.A. MCM values...\gamedata\configs\`; the GAMMA one was rewritten 2026-11-16 13:09 without a backup).
Approved by user 2026-11-16, in order after 24:
- 25 quest/task tool: stage, target, giver, description, completed/failed (also after hand-in), stage-change event.
- 26 dialogue: the agent must see the actual conversation options COMPLETELY (every option incl. scrolled-out ones, full untruncated text, speaker lines) plus dialog/phrase ids, and choose by id.
- 27 info portion list (actor's known infos; may fold into 25).
- 28 doors for game_move_to (user decision): emulate the use key on a door on the path / when stuck next to one (same path as a player press, no collision bypass, no opening doors up front), then report the door's state: opened, still closed, locked (with reason).
Maybe later (user: not now): scenario runner with assertions (agents can write their own test loops); gamedata deploy/reload loop (user doubts it will work). Small: game_object level field for off-level entities.
Minor: template user.ltx has 3 unknown commands (`lua_parallel_gc_pause`, `perf_spike_ms`, `perf_stats`); artefact pickup needs an active detector (document or tool); game_move_to stuck at closed doors.

Setup: `py -3.11 -m pip install "mcp>=2.3,<3"`; WER LocalDumps via `mcp/setup_wer.ps1` (done). GPU: RTX 2080 SUPER 8 GB. Caps 3 vanilla / 1 GAMMA (per profile, no shared VRAM budget). Vanilla ≈4.1 GB RAM / 1.6 GB VRAM. Template user.ltx: r__framelimit 30 (uncap for FPS tests), texture_lod 4, muted.
Open issues: Lua errors inside engine→Lua callbacks are fatal (ask user about an MCP-only non-fatal mode); `printe` errors not captured; no NPC-death event. One unreproduced double free in `CModelPool::Registry` at exit; one unreproduced startup hang. First screenshot after teleport can show the loading screen. `-showwindow` input ownership never tested live. User's saves `1`/`bench_g1` are GAMMA saves.
Testing: `game_teleport` for a new game; only stop game PIDs you launched. Harness pattern: scratch stdio MCP client (see supervisor.md). Parallel devs: worktrees, own `C:\Anomaly\bin_g<N>` copy, copy untracked `optick-git` in.
