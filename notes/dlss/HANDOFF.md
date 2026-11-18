# Handoff for a fresh session (written 2026-11-19 ~01:00, run paused for the usage limit)
Read in this order: `STATE.md` → this file → `BRIEFING.md` (what the user is told) → `PLAN.md` (goals D1-D13). Facts for briefs: `pipeline.md`, `shaders.md`, `sdk.md`, `bridge.md` (dev reports: verify a line ref before relying on it).
The user wants the run to continue unattended from 02:25 Helsinki time; chat output minimal, everything important goes into `BRIEFING.md`.

## Where things stand
- Branch `dev-mt-mcp-dlss` (clone `dev-mt-mcp-dlss\`), local only, head = last "notes" commit after merge `4e933260`. Merged: D1 `28c655cb`, D2 `52c804e2`, D3 `02b2874f`, D7 `0f3f3b45`, D8 `f0cc4f7a` + `115cf528`, D10 `84c8950e`, D11 `e17b4262`, D4 partial `ceeeadff`.
- Worktrees `D:\projects\xray-monolith-mcp\wt-dlss-a` (branch `dlss-a`) and `wt-dlss-b` (`dlss-b`). BOTH HOLD UNCOMMITTED WORK plus a `notes\plan.md` written by the paused dev (state, half-done edits, exact next step). Do not reset, clean or re-create them.
- The old dev agents belong to the previous session and cannot be messaged from a new one: spawn a fresh `dev` per worktree and tell it to start from `notes\plan.md` + `git diff`.
- No game was running at the pause. Deployed test builds: `C:\Anomaly\bin_dlss_a`, `C:\Anomaly\bin_dlss_b`. Gamedata: `C:\Anomaly\db\mods\00_modded_exes_gamedata.db0` (Dev A's, backup `.bak_pre_dlss`), `01_dlss_b_test.db0` (Dev B's extra files: sharpen shader, menu script, text).

## Open work, in order
### Dev A (worktree a): finish D4, then D13, then D5
1. **Blocker: DLSS intermittently outputs a dark / white / magenta whole frame** (presets M and K). Known so far:
   - Not K-specific; exposure, IsHDR, output format, alpha, reset flag, driver override, NaN vectors are ruled out (`sdk.md`).
   - Uncommitted in the worktree: D3D `ClearState` + `RCache` invalidate before and after the NGX evaluate in `phase_dlss`, a guard that restores swapped output RTs at frame start, temporary debug views `r4_upscale_debug 3/4` (raw DLSS output; 4 adds a per-frame log). Remove 3/4 before the commit.
   - Result: after the FIRST DLSS start ~180 frames were clean (preset M, performance). After a SECOND `vid_restart` bad frames are back at once (16 of 21) and after closing the inventory / Esc menu. So the trigger is the DLSS feature being released and created again.
   - Next step: one short run, `vid_restart` twice, `r4_upscale_debug 4`: is the raw DLSS output already bad, and what does the per-frame log say. In parallel read the release / recreate order in `r4_dlss.cpp` against the render-target teardown on `vid_restart`.
   - Coordinator's suspects to hand the dev: (a) `create_feature` makes a new `exposure` texture every time and the old one is never released; `params` is one long-lived NGX parameter object that keeps resource pointers and hint values from the previous feature / from probing (stale pointers after the RTs were rebuilt) → allocate fresh parameters (or reset every resource entry) per feature and release everything in `release_feature`; (b) the feature is released while the context still has its resources bound, or created before the new RTs exist; (c) two features alive (old one not released) fighting over history; (d) `rt_dlss_mv` / `rt_dlss_depth` are created once in `begin_temporal_frame` (`if (!rt_dlss_mv)`) - check they are destroyed and re-created at the new size on `vid_restart`, and that `history_reset` is set for the first frame of the new feature.
   - Pass = zero bad frames over walk / turn / strafe, after launch, after 3 `vid_restart`, after inventory and Esc menu, with sharpening on and off. Then test K again: if clean, `default` → K on RTX 20/30 (original decision), M/L opt-in.
   - If it cannot be fixed in 2 more test sessions: park D4 sign-off, keep DLSS behind its cvar as experimental, write the evidence into `sdk.md` and `BRIEFING.md`, go on with what does not depend on it.
2. Effect walk (never done properly): AnomalyMO2 for water, SSR, AO/IL, sun, motion blur, DOF, anomalies, glass, night with flashlight, emission; short exclusive GAMMA part for night vision, grading, gasmask drops, thermal, Cordon burst. Record defects in `shaders.md`, fix only cheap engine-side causes. Decided: no DLSS bias mask (presets J/K/L/M ignore it); particle / water / glass vectors stay a known defect.
3. D13 (overlays after the upscale) and D5 (scopes): specs in `PLAN.md`. D5 inputs: every scope frame falls back to bilinear, history resets every frame while a scope is active, sharpening skips scope frames, frame generation runs in reset mode while aiming.

### Dev B (worktree b): finish D6, then D12
1. D6 options menu is implemented, uncommitted: status console commands `r4_dlss_status`, `r4_framegen_status`, `r4_reflex_status`; `gamedata\scripts\modxml_options_dlss.script`; `gamedata\configs\text\eng\ui_mm_dlss.xml`. Works: entries can be set, console values match, "AA ignored while DLSS is on" line shows. Still wrong: the "unavailable / reason" lines render as empty gaps; one launch reported Reflex as "not an NVIDIA GPU" on the RTX 2080 SUPER. Then: rebuild, repack `01_dlss_b_test.db0`, finish the test (plain `C:\Anomaly`, AnomalyMO2, one short GAMMA launch: menu opens, GAMMA's options intact, no script errors), one commit.
2. D12 separate UI texture for frame generation: the UI is already its own pass drawn after the upscale, so this should be mostly redirecting that pass to its own target and compositing; park with findings if it grows.

### After both tracks: D9
One ~10 min GAMMA soak with DLSS + frame generation + sharpening on (if the DLSS blocker is fixed; else frame generation at native res), fix what it finds. Then the wrap-up below.

## Wrap-up ("done when")
All goals merged or parked with reasons → final DX11-AVX build clean at the branch head → pack ALL gamedata into `00_modded_exes_gamedata.db0`, deploy, delete `C:\Anomaly\db\mods\01_dlss_b_test.db0` (ours) → copy exe + pdb + `nvngx_dlss.dll` + the two FSR DLLs to `C:\Anomaly\bin_dlss` → remove worktrees and dev branches (only after their work is merged) → `STATE.md` + `BRIEFING.md` final.

## Roles and rules (user's orders, still valid)
- Coordinator (main session): briefs, merges (git only; code conflicts go back to the dev; conflicts in `notes/` it may resolve itself, keeping both sides), keeps `STATE.md` / `BRIEFING.md`, investigates hard problems by reading code / logs / docs and hands the dev a solution. It does NOT edit engine code.
- Devs: agent type `dev`, one per goal, working only in its worktree with absolute paths. Playtesters are spawned by the dev: `general-purpose`, `model: sonnet`, `effort: low` (they have the `mcp__anomaly__*` tools, devs do not). One build, one playtest session per goal, max 2 extra sessions for failed checks, then stop and report; the coordinator re-briefs once, then parks.
- Tests: plain `C:\Anomaly` (no SSS → upscaler / interpolation "unavailable", used for the fallback checks) and the vanilla MO2 profile `C:\AnomalyMO2` (SSS + MCM, the default `game_launch` root) for most work; these may run in parallel. GAMMA `C:\GAMMA` only for GAMMA-only checks, short, one at a time and never next to any other game (tester checks `game_list` first and waits). Do not build into a running exe. Uncap fps (`r__framelimit 0`) before frame-time numbers; mark numbers "noisy" if anything else ran.
- Deploy: `C:\Anomaly\bin_dlss_a` / `bin_dlss_b`, never `C:\Anomaly\bin`. Dev A owns the `00_...db0`; Dev B packs only its own files into `01_dlss_b_test.db0`. Overwrite archives only when no game runs.
- Notes: `notes/dlss/*.md` only; never touch `notes/MAP.md`, `notes/STATE.md` or other area files on this branch. No test code in the repo; scratch under `C:\Users\vacwave\AppData\Local\Temp\claude`.
- Git: one commit per goal on the dev branch; coordinator merges into `dev-mt-mcp-dlss` after each goal and fast-forwards the worktree. `x64/` is gitignored (`git add -f` for SDK libs). Never commit `src\3rd party\optick-git\.git_from_copy`.
- Push: the user allowed push / PR on this branch on 2026-11-18, but the branch now contains vendor SDK files (DLSS, FidelityFX, NvAPI) and the user's earlier rule was "no push until the licences are sorted". Coordinator's decision: do not push; the user decides.
- Unattended: no questions to the user; unclear → smaller, reversible option + a line in `STATE.md`. Never: write to `C:\GAMMA\mods`, change `dev-mt-mcp\`, edit agents or CLAUDE.md, delete files we did not create. Usage limit close → favour track A, continue B after.
- Cvars (final names): `r4_render_scale`, `r4_upscale_debug`, `r4_dlss`, `r4_dlss_preset`, `r4_upscale_sharpness`, `r4_dx12_present`, `r4_framegen`, `r4_reflex`. All default off; off = today's frame and present path.

## For the user (kept in BRIEFING.md)
Generated frames were never seen by a tool (screenshots show rendered frames only); Reflex shows no measurable gain; preset choice and the Reflex-vs-engine frame limiter are open choices; a pre-existing `rs_screenmode windowed` crash in hidden MCP runs is documented, not fixed.
