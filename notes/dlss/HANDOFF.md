# Handoff: overnight unattended run, 2 devs
Written 2026-11-18 by the planning session. The user approved `PLAN.md` (D1-D9, D9 soak = ~10 min) and asked for an unattended overnight run with 2 devs. Nothing is coded yet. Read `STATE.md`, this file, then `PLAN.md`; `pipeline.md` / `shaders.md` / `sdk.md` are the facts for the briefs (reader reports: verify a line ref before relying on it).

## Start
1. Repo = `D:\projects\xray-monolith-mcp\dev-mt-mcp-dlss`, branch `dev-mt-mcp-dlss` (single-commit history, local only). Check it is clean and builds are not running.
2. One worktree + branch per dev (`dlss-a`, `dlss-b`; Agent worktree isolation if the session's cwd is the clone, else `git worktree add`). Copy the untracked `src\3rd party\optick-git` into each (see `notes/STATE.md` "Parallel devs").
3. Brief both devs at once (goal, files/symbols from the notes, decisions, constraints, owned files, out of scope, the Test line of the goal).

## Tracks (disjoint files)
- Dev A (renderer): D1 → D2 → D3 → D4 → D5 → D6. Owns `src\Layers\xrRenderPC_R4\*`, `src\Layers\xrRender\rendertarget_phase_*`, `Blender_Recorder_StandartBinding.cpp`, `xrRender_console.*`, blenders, gamedata.
- Dev B (presentation): D7 → D8. Owns `src\Layers\xrRenderDX10\dx10HW.*`, `src\Layers\xrRender\dxRenderDeviceRender.*`, new bridge / frame-generation files, project files for them. Its cvars: one registration line each in `xrRender_console.cpp` (the only shared file; coordinator resolves that conflict at merge).
- D8 needs D1 + D2 merged (jitter, `rt_dlss_mv`, depth, reset flag). If D7 is done first: merge it, rebase B on main after D2 lands; while waiting B may only do D8 steps that do not need them (SDK add, swapchain swap, UI/HUD-less image).
- D9: after everything is merged, one dev, coordinator checks the result.
- Merge after every goal (not at the end): into `dev-mt-mcp-dlss`, one at a time, build, then the other dev rebases at its next goal boundary. One commit per goal. Do NOT push (user: not yet).

## Decisions already made (do not ask)
- Cvar names in `PLAN.md` are final: `r4_render_scale`, `r4_upscale_debug`, `r4_dlss`, `r4_dlss_preset`, `r4_dx12_present`, `r4_framegen`. All default off; off = today's frame and present path.
- DLSS on → SSS TAA and SMAA skipped, MSAA refused with a log line. HDR10 + DLSS or + bridge: if it does not work without extra design, disable the feature under HDR10 with a log line and note it.
- DLSS preset default K on RTX 20/30; M/L opt-in. Direct NGX on D3D11, no Streamline.
- Bridge may be borderless/windowed only if exclusive fullscreen is a problem: log it, note it.
- Shader fixes go into our gamedata, never into `C:\GAMMA\mods` (read-only for us).
- Testing: all substeps of a goal first, one build, ONE live session per goal (its Test line). No test code in the repo; scripts in the scratchpad. Short checks only; the user tests in real use.

## SDK dependencies (CLAUDE.md: ask first)
- The planning session asked the user for an explicit OK on downloading/adding the DLSS SDK (D3) and the AMD FSR SDK (D8). Answer: see `STATE.md` line "SDK OK". If that line does not say yes, do NOT add them: Dev A stops after D2 and does the SDK-free parts of D5/D6 (scope frames with the bilinear upscaler, menu entries for `r4_render_scale`), Dev B stops after D7, and STATE lists what waits.
- If yes: official sources only (github.com/NVIDIA/DLSS, github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK), pinned to the versions in `sdk.md`; headers/libs under `sdk\`, DLLs copied next to the exe by hand; do not modify or rename vendor DLLs. Licence fit is unchecked: fine locally, must be checked before any push.

## Unattended rules
- No questions to the user. Unclear point → take the smaller, reversible option, write the decision and why in `STATE.md`. Hard blocker on a goal → note it, move to the next goal that does not depend on it; never work around a deliberate block.
- Never: push, open PRs/issues, delete files outside the worktrees, touch `C:\GAMMA\mods`, change `dev-mt-mcp/`, edit settings or CLAUDE.md.
- Deploy for tests into `C:\Anomaly\bin_dlss_a` / `bin_dlss_b` (exe + pdb + SDK DLLs), not `C:\Anomaly\bin` (that exe belongs to the MCP branch and its testers). If gamedata changes: back up `C:\Anomaly\db\mods\00_modded_exes_gamedata.db0` once (`.bak_pre_dlss`) before overwriting it, and say so in STATE.
- Games: 8 GB VRAM. Only one GAMMA game at a time across both devs and never next to a vanilla one; check for running games first, wait instead of launching a second. Stop every game you launched; do not build into an exe that is running. Frame-time numbers only count when the other dev is not building or running a game (else mark them "noisy").
- A dev that fails the same step 3 times stops that goal and reports; the coordinator re-briefs once with a narrower scope, then parks the goal.
- Keep `notes/dlss/STATE.md` current after every merged goal (≤20 lines): done goals + commit, parked goals + reason, decisions taken, what the user must test or answer in the morning. Devs add facts/gotchas to `pipeline.md` / `shaders.md` / `sdk.md` (one line each).

## Done when
D1-D9 merged or parked with reasons, final DX11-AVX build clean at the branch head (`_build\_game\bin_dbg\AnomalyDX11AVX.exe`), worktrees and dev branches removed, STATE has the morning summary.
