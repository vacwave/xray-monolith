# DLSS branch STATE
Scope: branch `dev-mt-mcp-dlss` (clone `dev-mt-mcp-dlss/`). All DLSS/FSR notes live in `notes/dlss/` only; do not touch `notes/STATE.md` / `MAP.md` (kept identical to `dev-mt-mcp`).
Files: `STATE.md` (this, ≤20 lines, overwrite) · `BRIEFING.md` (summary for the user) · `PLAN.md` (goals D1-D13) · `pipeline.md` · `shaders.md` · `sdk.md` · `bridge.md` · `options.md` · `HANDOFF.md` (rules of the overnight run; its "open work" part is outdated).

DONE 2026-11-19 05:55: all goals D1-D13 merged, plus the user's orders (startup crash fix, sight check). Nothing running, nothing unmerged, worktrees and dev branches removed. Waiting for the user's real-use test and decisions.
Head: code as of merge `aeeb4f8a`. Final DX11-AVX build clean → `_build\_game\bin_dbg\AnomalyDX11AVX.exe`. NOT pushed.
Deployed: `C:\Anomaly\bin_dlss` (soak-tested exe + pdb built from the same code in the removed worktree, `nvngx_dlss.dll`, 2 FSR DLLs) · `C:\Anomaly\db\mods\00_modded_exes_gamedata.db0` (all gamedata; backup `.bak_pre_dlss`). Also left: `C:\Anomaly\bin_dlss_a` / `bin_dlss_b` (test builds + test `.ltx` files, removable).
Merged: D1 `28c655cb` · D2 `52c804e2` · D3 `02b2874f` · D7 `0f3f3b45` · D8 `f0cc4f7a` + `115cf528` · D10 `84c8950e` · D11 `e17b4262` · D4 `ceeeadff` + `1cc5ff7b` · D6 `ccabc56d` · D12 `c8a39779` · D13 `c0f62d67` · D5 `2dccad3b` · empty-DLSS-output recovery `33b6a82b` · startup fix `f203d4a1` + `c50dceb9` · 3DSS lens vectors `1938ad7a` + `b6f4878c` · D9 soak notes `728adbfa`.
Open (user decides / checks by eye):
- Push: branch holds vendor SDK files (DLSS, FidelityFX, NvAPI).
- D12: UI on presented frames with frame generation was never seen by a tool; `git revert c8a39779` = old HUD-less mode.
- Empty-DLSS-output recovery never fired live (the bug did not recur after the fix); cause inside NGX unknown.
- Rare one-off red / white / magenta frames after `vid_restart` or equip (self-recovering); one GAMMA hang in `vid_restart` during NGX load (seen once); one 0xC0000374 exit code after a start without any config (seen once).
- Preset K broken on RTX 2080 SUPER + D3D11 (default = M). Reflex vs engine limiter when a cap is set.
- Not tested: water, SSR puddles, sun / lens flare, gas mask drops, full-screen heat vision, 2D texture scopes, Aimpoint, frame generation while aiming an SVP scope, headlamp at night.
- Not from this branch: crash in `CCustomZone::shedule_Update` (`CustomZone.cpp:647`) on MCP `game_teleport` during load (for `dev-mt-mcp`); `wpn_ak12_m1_eot` has no reticle; black sky in GAMMA `w_foggy2`.
Tester gotchas: pass `instance` explicitly · GAMMA thermal sights need battery charge (handler line in `shaders.md`) · bare `-ltx` names are not found in an MCP slot, use absolute paths · the GAMMA MCP slot uses its own template user.ltx (real one: `C:\Anomaly\appdata\user.ltx`).
