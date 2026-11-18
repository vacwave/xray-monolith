# Handoff: overnight unattended run (final, 2026-11-18)
The user approved `PLAN.md` (D1-D9, D9 soak ~10 min) and both SDKs, and wants an unattended overnight run. Nothing is coded yet. Read `STATE.md`, this file, then `PLAN.md`; `pipeline.md` / `shaders.md` / `sdk.md` are the facts for briefs (reader reports: verify a line ref before relying on it).

## Roles (user's orders)
- Coordinator (main session) = master coordinator only. It does NOT write or edit engine code, not even trivial fixes. It briefs, answers dev questions, merges, keeps `STATE.md`, and investigates hard problems (read code, logs, crash dumps, docs; use `reader`s / web research) to hand the dev a solution.
- Devs = 2 × agent type `dev` (Opus 5.5, medium effort: already set in `.claude/agents/dev.md`; do not override). A dev that is confused or missing a decision stops and asks the coordinator (its report); the coordinator answers the SAME dev with SendMessage. Devs do not guess on design questions.
- Coders = as in `dev.md` (dev's choice for mechanical steps).
- Playtesters = spawned by the dev, one per goal test: agent type `general-purpose` with `model: sonnet` and `effort: low` (user's order: Sonnet 5.5 low; it has the `mcp__anomaly__*` game tools, devs do not). Brief: exe path, game root (vanilla / `C:\GAMMA`), settings profile, the goal's Test line as a checklist, cvars to toggle, what "broken" looks like. It returns pass/fail per check with screenshot file paths and log lines, changes no code, stops every game it launched. The dev reads the report, fixes, and re-tests only the failed checks (max 2 extra sessions per goal, then ask the coordinator).

## Start
1. Start the session in `D:\projects\xray-monolith-mcp` (the `.claude` agents and settings live there). Repo = `dev-mt-mcp-dlss\`, branch `dev-mt-mcp-dlss` (local only). Check it is clean.
2. One worktree + branch per dev: `git -C dev-mt-mcp-dlss worktree add ..\wt-dlss-a -b dlss-a` (and `wt-dlss-b` / `dlss-b`). Copy the untracked `src\3rd party\optick-git` into each (see `notes/STATE.md` "Parallel devs").
3. Brief both devs at once: goal, files/symbols from the notes, decisions, constraints, owned files, out of scope, the goal's Test line, and the overrides below.

## Overrides of `dev.md` / CLAUDE.md for this branch (put them in every brief)
- Notes: facts and gotchas go into `notes/dlss/*.md` only; do not touch `notes/MAP.md`, `notes/STATE.md` or other area files. `notes/plan.md` as scratch is fine (delete before the commit).
- Verify: DX11-AVX build in the worktree, deploy exe + pdb (+ SDK DLLs) to `C:\Anomaly\bin_dlss_a` / `bin_dlss_b`, NOT `C:\Anomaly\bin` (that exe belongs to the MCP branch and its testers). Gamedata changed → back up `C:\Anomaly\db\mods\00_modded_exes_gamedata.db0` once as `.bak_pre_dlss` before overwriting, and report it.
- Testing: all substeps of the goal first, one build, ONE playtester session for the goal's Test line. No test code in the repo; scripts in the scratchpad.
- No push. One commit per goal on the dev's branch.

## Tracks (disjoint files)
- Dev A (renderer): D1 → D2 → D3 → D4 → D5 → D6. Owns `src\Layers\xrRenderPC_R4\*`, `src\Layers\xrRender\rendertarget_phase_*`, `Blender_Recorder_StandartBinding.cpp`, `xrRender_console.*`, blenders, gamedata.
- Dev B (presentation): D7 → D8. Owns `src\Layers\xrRenderDX10\dx10HW.*`, `src\Layers\xrRender\dxRenderDeviceRender.*`, new bridge / frame-generation files, project files for them. Its cvars: one registration line each in `xrRender_console.cpp` (the only shared file; a dev fixes that conflict when rebasing).
- D8 needs D1 + D2 merged (jitter, `rt_dlss_mv`, depth, reset flag). If D7 finishes first: merge it; B rebases after D2 lands and meanwhile does only the D8 steps that do not need them (SDK add, swapchain swap, HUD-less image).
- D9: after everything is merged, one dev + one playtester (~10 min GAMMA session).
- Merge after every goal: coordinator merges the dev branch into `dev-mt-mcp-dlss` (git only; a conflict goes back to the dev), the next goal's dev starts from the new head. A fresh `dev` per goal, same worktree.

## Decisions already made (do not ask the user)
- Cvar names in `PLAN.md` are final: `r4_render_scale`, `r4_upscale_debug`, `r4_dlss`, `r4_dlss_preset`, `r4_dx12_present`, `r4_framegen`. All default off; off = today's frame and present path.
- DLSS on → SSS TAA and SMAA skipped, MSAA refused with a log line. HDR10 + DLSS or + bridge: if it needs extra design, disable the feature under HDR10 with a log line and note it.
- DLSS preset default K on RTX 20/30; M/L opt-in. Direct NGX on D3D11, no Streamline.
- Bridge may be borderless/windowed only if exclusive fullscreen is a problem: log it, note it.
- Shader fixes go into our gamedata, never into `C:\GAMMA\mods` (read-only for us).
- SSS test settings: the user's GAMMA has every SSS option at minimum (`sss_mcm_user_min.ltx`); test with that profile AND an "SSS high" one (`shaders.md` "SSS settings for tests"). Changing them in the test slot is allowed.
- SDKs: both approved (STATE "SDK OK"). Official sources only (github.com/NVIDIA/DLSS, github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK), versions from `sdk.md`; headers/libs under `sdk\`, DLLs copied next to the exe; never modify or rename vendor DLLs. Local/private use: nothing is pushed or published.

## Unattended rules
- No questions to the user. Unclear point → the coordinator takes the smaller, reversible option and writes the decision and why into `STATE.md`. Hard blocker on a goal → park it with the reason, continue with the next goal that does not depend on it; never work around a deliberate block (denied permission, protected file).
- Never: push, open PRs/issues, delete files outside the worktrees, touch `C:\GAMMA\mods`, change `dev-mt-mcp\`, edit settings, agents or CLAUDE.md.
- Games: 8 GB VRAM. Only one GAMMA game at a time across everything and never next to a vanilla one: a playtester checks `game_list` first and waits instead of launching a second. Do not build into an exe that is running. Frame-time numbers only count when nobody else is building or running a game (else mark them "noisy").
- A dev that fails the same step 3 times, or has used its 2 extra test sessions, stops and reports. The coordinator investigates and re-briefs once with the solution or a narrower scope; if that fails too, park the goal.
- After every merged goal update `notes/dlss/STATE.md` (≤20 lines): done goals + commit, parked goals + reason, decisions taken, what the user must test or answer in the morning.

## Done when
D1-D9 merged or parked with reasons, final DX11-AVX build clean at the branch head (`_build\_game\bin_dbg\AnomalyDX11AVX.exe`, also copied with the SDK DLLs to `C:\Anomaly\bin_dlss`), worktrees and dev branches removed, STATE has the morning summary.
