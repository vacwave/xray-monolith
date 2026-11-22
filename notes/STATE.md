# STATE
Workflow: agents coder / developer / reader / investigator / playtest; see root CLAUDE.md. Push / PR only when decided with the user (stutter fixes: PR as soon as a re-measurement confirms the spike is gone, user 2026-11-22).

## Queue
1. Stutter program (GAMMA, walking / camera): session 3 (2026-11-22) done so far. Method, tools, facts: notes/perfwork.md ("Stutter session"). Reports: D:\projects\xray-monolith-mcp\perf-baseline\hitch_*.txt, focus_*.txt.
   - Kept, committed on dev-mt-mcp, PRs open on vacwave/xray-monolith: ST1 axr_main callback order #128, ST2 actor-only CActor_OnChangedSlot #129, ST3 hand-cycle map #130, ST4 GC udata walk prefetch #131, ST5 tutorial XML preload #132, ST6 CLevelGraph::vertex search #133 (#114 closed in its favour).
   - Gamedata pack perf-baseline\stutter_gamedata_00_modded_exes_gamedata.db0 (axr_main.script change; NOT deployed: C:\Anomaly\db\mods was restored).
   - Open stutters: stalker net_Spawn 20-90 ms (script: sound_theme init_npc, zc_core init_npc / sound_exists), shader compile on the prefetch thread blocking main (40-70 ms), UI texture apply_load at first draw (93 ms), 17-28 ms frames with the main thread descheduled (needs context-switch tracing = admin).
   - Needs the user's decision: bump_get / bump_exist wait removal (LD1 = PR #127; same change came up again for HUD model loads, patch perf-baseline\patches\ST-bump-wait-dev.patch); GC pacing (the parallel LuaGC task forces ~5 full cycles per second).
2. Performance program leftovers (session 2): perf-baseline\todo.md (LD2, bundle branch cherry-picks, PRs #114-#127 open).

## Goal rules (user)
- Surgical fixes only. No threading changes. Behaviour identical (NPC/object counts checked).
- Perf commits touch engine code only (no MCP code). GAMMA: 1 instance, never next to another game. Benchmarks only on an idle machine.

## Open
- 2026-11-22: upstream/dev-mt (b58698d0) merged as `Sync to upstream 2026-11-22`; default exe = `C:\Anomaly\bin_mcp\AnomalyDX11AVX.exe` (never `C:\Anomaly\bin`). Open PRs stay open untouched (reference only).
- mcp/templates/user.ltx has uncommitted higher graphics settings; notes/ are uncommitted.
