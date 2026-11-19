# DLSS + frame generation plan

## User decisions (2026-11-18)
- Target: DLSS upscaling ("DLSS 4.5") + frame generation in the DX11 engine, working with SSS, 3D scopes and the rest of GAMMA's shaders.
- Minimal engine changes; no DX12 renderer rewrite; do not break mods.
- Frame generation = AMD FSR FG (runs on the 2080 Super), DLSS does the upscaling. DLSS FG not planned (untestable here).
- A presentation-only DX12 bridge (DX11 renders, DX12 device owns the swapchain) is accepted.
- When DLSS (or any temporal upscaler) is on, the engine must turn off / ignore the other AA: SSS TAA, SMAA, MSAA (they conflict). Same for FSR upscaling if it is ever added.
- 3D shader scopes are expected to get blurry under DLSS: own goal later, not a blocker for the first DLSS goal.
- Branch stays local until the user says push.
- Testing: write all substeps of a goal, build once, ONE live test session per goal (no game launch per substep). No test code in the repo: checks are scratchpad scripts / MCP tool calls. The user tests in real use afterwards.

## Design direction (coordinator's inferences, to prove in D1/D2)
- Reuse the SSS temporal path (`ssfx_jitter`, `rt_ssfx_motion_vectors`, `rt_ssfx_hud`) instead of a second one; DLSS replaces `phase_ssfx_taa` in the same slot (after the SMAA slot, before `phase_pp`).
- Render/output split through the one `screen_res` constant (render res before the upscale, output res after) so mod shaders stay unmodified.
- Everything behind console variables, default off; off = today's frame.

## SDK consequences (see sdk.md)
- D3: direct NGX on D3D11 (no Streamline needed); preset cvar, default K on RTX 20/30, M/L ("4.5") opt-in; the SSS motion-vector buffer (RGBA16F, shares the HUD mask) needs a decode pass to RG16F pixel-space vectors.
- D7/D8: FSR FG 3.1.x swapchain on a D3D12 device, UI mode = HUD-less colour or separate UI texture; same shape as Skyrim Community Shaders. Expect borderless-only limits and overlay trouble; FG wants 60+ fps input.
- Budget: GAMMA already uses 7.0 of 8 GB VRAM in Cordon; DLSS (~0.1-0.5 GB) + a second device and FG history come on top, lower render res gives some back.

## Goals
Each goal = one `dev` brief, one commit, one build, one live test at the end (the "Test" line). Substeps are written back to back without launching the game. Cvar names are proposals.

### D1 Render-resolution split (no SDK)
1. Cvar `r4_render_scale` (0.5-1.0, default 1.0, applied on device reset / vid_restart); render size = output size × scale, kept in one place in CRenderTarget.
2. Size the pre-upscale RTs from render size (G-buffer, accum, generic, all `rt_ssfx_*`, the /2 /4 /8 chains, half depth); keep post-upscale RTs at output size (final colour, `rt_secondVP`, UI/PDA, screenshot staging). Depth buffer for the scene at render size.
3. `screen_res`, `pos_decompression_params` and viewports report render size before the upscale slot and output size after it.
4. Upscale pass at the `phase_ssfx_taa` slot: plain bilinear copy render → output; everything after it (combine tail, HUD UI, phase_pp, UI) runs at output size.
5. Scale 1.0 must take the old code path unchanged.
Test (1 session, vanilla then GAMMA): screenshots at scale 1.0 vs 0.67 in Cordon day + night + rain, HUD weapon, inventory, PDA; look for wrong-sized effects, offset reflections/AO, UI at wrong size; `log_tail` for shader/RT errors.

### D2 Temporal inputs
1. Engine-owned jitter: Halton sequence in pixels, fed through `ssfx_jitter`, active when the upscaler is on even if SSS TAA is off; expose current/previous jitter for later use.
2. Motion-vector audit from the shaders (read only): list which geometry writes correct vectors (static, skinned, HUD weapon, grass/trees, particles, sky) and fix only the missing engine-side previous matrices.
3. Resolve pass: `rt_ssfx_motion_vectors` → RG16F pixel-space vectors at render size (`rt_dlss_mv`), plus a depth copy usable as input.
4. History-reset flag: level load, teleport/camera cut, resolution change, first frame after an SVP (scope) frame.
5. Debug view cvar (`r4_upscale_debug` 0/1/2: off / motion vectors / depth) for the test only.
Test (1 session, GAMMA): debug view screenshots while standing still (vectors ≈ 0), strafing, with a moving NPC, swaying grass, weapon sway. Without SSS installed (vanilla): jitter + vectors still present or the upscaler reports "unavailable".

### D3 DLSS upscaling + DLAA (needs user OK: DLSS SDK)
1. Add the SDK (headers + lib in `sdk/`, `nvngx_dlss.dll` next to the exe, not in gamedata); init/shutdown with the device, release/recreate on reset; log why when unavailable (non-RTX, driver, DLL missing).
2. Cvars: `r4_dlss` (off, DLAA, quality, balanced, performance, ultra performance) sets the render scale from the SDK's optimal settings; `r4_dlss_preset` (default, K, M, L; default → K on RTX 20/30).
3. Evaluate at the upscale slot with colour, depth, `rt_dlss_mv`, jitter, reset flag, auto-exposure; restore the D3D11 state the engine caches (RCache) afterwards.
4. While DLSS is on: skip `phase_ssfx_taa` and `phase_smaa`, treat MSAA as off (refuse to enable with a log line), apply the texture mip bias.
5. Unavailable or evaluate failure → fall back to D1's bilinear path, no crash.
Test (1 session, vanilla then GAMMA): each mode once + DLAA, preset K and M; screenshots still and moving (ghosting, shimmer, HUD weapon smear); `game_perf` frame time per mode at uncapped fps; toggle on/off and vid_restart ×3 without leaks or crash.

### D4 GAMMA compatibility pass
1. Walk the effect list with DLSS quality + performance and note defects: SSR, AO, IL, water, rain/puddles, fog, volumetrics, bloom + lens, motion blur, DOF, glass, night vision (Beef's), Enhanced Shaders grading, gasmask drops, particles/anomalies, emission, heat/thermal.
2. Fix engine-side causes (wrong size constant, wrong RT bound, missing vectors for transparents); shader edits only if unavoidable and shipped in our gamedata db, never in the mods.
3. HDR10 path on: check or disable DLSS with a log line.
Test: the walk in step 1 IS the test, done once before the fixes and once after (burst screenshots along a fixed Cordon → Garbage route, day/night/rain/emission via `game_weather` / `game_time`).

### D5 Scopes under DLSS
1. SVP frames: decide per the D3 result whether the scope view renders at render or output size and whether it runs DLSS; keep its history separate from the main view (reset or second DLSS instance).
2. 3DSS lens sharpness: lens samples of `s_image` / `s_position` at the right resolution and mip bias; fix `screen_res` use inside the lens pass.
3. SDS `fakescope` (hard-coded 16:9 factor) and 2D scopes with the split.
4. Scope in/out transitions: reset flag instead of ghost trails.
Test (1 session, GAMMA): 3DSS scope, SDS scope, 2D scope, thermal/NV scope: screenshots hip → aiming → moving while aiming → un-aim; compare lens sharpness to DLSS off.

### D6 Options
1. Settings menu entries (upscaler mode, preset, sharpness if used) wired to the cvars, saved in user.ltx; AA options greyed out or ignored while DLSS is on.
2. Shown as unavailable with the reason on unsupported hardware.
Test: change each option in the menu, restart the game, values persist; vanilla + GAMMA menu both show it.

### D7 DX12 presentation bridge (no frame generation yet)
1. Cvar `r4_dx12_present` (default off, read at start): create a D3D12 device + flip swapchain for the window; the DX11 device keeps rendering into a shared texture instead of the swapchain back buffer.
2. Per frame: sync (shared fence), copy the shared final image to the D3D12 back buffer, Present there; SVP frames still skip Present.
3. Keep working: resize / vid_restart / alt-tab, vsync on/off, tearing, borderless + fullscreen (or document borderless-only), HDR10, MCP screenshots (they read the back buffer), device-lost handling.
4. Off = today's present path untouched.
Test (1 session, vanilla): launch with the bridge, `game_perf` frame time vs bridge off (cost of the copy + sync), resize and alt-tab ×3, screenshot tool still returns the frame, clean exit; then one GAMMA launch for VRAM headroom.

### D8 FSR frame generation (needs user OK: AMD FSR SDK)
1. Add the SDK (D3D12 backend DLLs next to the exe); replace the bridge's swapchain with the FSR frame-generation swapchain.
2. Share depth + `rt_dlss_mv` to D3D12; call FG prepare with jitter, camera near/far/FOV, frame time, render size; dispatch per presented frame.
3. UI: HUD-less colour = the image before `render_forward` HUD UI / 2D UI, final image with UI as the presented frame.
4. Cvar `r4_framegen` (off/on, default off); pacing vs `r__framelimit`; skip or hold interpolation on SVP frames, level loads, menus and when the reset flag is set.
5. Works with DLSS on and with DLSS off (native res).
Test (1 session, GAMMA): `game_perf` presented vs rendered fps, burst screenshots of a strafe (interpolated frames: HUD/crosshair/text not warped), scope aiming, menu and inventory open, loading screen; VRAM against the 8 GB limit.

### D9 Soak + performance
1. One ~10 min GAMMA session (hunt / changelevel loop) with DLSS + FG on: crashes, leaks (VRAM + RAM over time), script errors.
2. Fix what it finds; final DX11-AVX build; update `notes/dlss/`.
Test: the soak itself; then hand over to the user for real play.

## Added by the user 2026-11-19 ("add all to the plan")
D4 gets two extra steps: (a) motion vectors for particles / water / glass / other transparents where the engine can supply them; (b) a DLSS bias (reactive) mask for the pixels that still have none, so history is trusted less there.
D6 also covers: sharpness slider next to the DLSS mode, frame generation on/off, the DX12 bridge (restart needed), low-latency mode.

### D10 Sharpening
1. Cvar `r4_upscale_sharpness` (0-1, default 0 = off): one RCAS pass right after the upscale slot at output size (AMD's RCAS shader from the already approved FidelityFX SDK, in our gamedata). Works with DLSS, DLAA and plain `r4_render_scale`; also offered with frame generation on at native res ("FSR too": coordinator's reading = same slider applies whenever our upscale / FG path is active).
2. Off = no pass. Not applied on SVP frames unless the scope view is upscaled too (D5 decides).
Test: screenshots at 0 / 0.5 / 1 with DLSS quality and at native; no halos on the HUD/UI (UI is drawn after it).

### D11 Low latency (NVIDIA Reflex via NvAPI)
1. NvAPI is already in the repo (`sdk\include\nvapi`, `sdk\libraries\x64\nvapi.lib`, used by `HWCaps.cpp`): cvar `r4_reflex` (off / on / on+boost, default off) → `NvAPI_D3D_SetSleepMode` on the DX11 device, `NvAPI_D3D_Sleep` once per frame before input sampling / simulation. If the bundled NvAPI is too old for these calls, update it from the official NVIDIA NvAPI repo (covered by the user's "add all").
2. Unsupported (non-NVIDIA, old driver) → log line, off. With `r__framelimit` set, pass the cap as the sleep-mode minimum interval instead of fighting it.
3. Optional if cheap: latency markers (simulation / render submit / present).
Test: `game_perf` frame time unchanged or better with it on, no hitching, toggle at runtime, works with the bridge + frame generation on; real latency is for the user to feel.

### D12 Separate UI texture for frame generation
1. Render the 2D UI / HUD UI into its own RGBA target (premultiplied alpha) instead of straight onto the final image when frame generation is on, composite it for the presented frame, and pass it to FSR as the UI texture (instead of the HUD-less colour pair).
2. Only if D8's HUD-less mode shows warping or the change stays small; otherwise park with the findings. Off (framegen off) = today's UI path.
Test: strafe + text on screen with frame generation on; the user judges generated frames.

### D13 Screen-fixed overlays after the upscale (coordinator, from the D4 findings)
1. Gasmask drops, night vision, heat vision, fakescope, DOF, LUT, motion blur and PP-UI run before the upscale slot today, so DLSS treats them as moving scene (smear, lost NV grain). Move the upscale slot in front of them (or them behind it) so they run at output size; list of phases and what each reads is in `pipeline.md` (written by the D4 dev).
2. DLSS / render scale off = today's order and frame. Sharpening stays directly after the upscale.
Test: GAMMA night vision, gasmask, thermal, DOF and motion blur with DLSS quality vs off; D1's scale 0.67 checks again.

Order: Dev A D3 → D4 → D13 → D5 · Dev B D11 → D10 → D6 → D12 · then D9.

## Added by the user 2026-11-19 (real-use feedback, GAMMA)
User ran preset K: works for them (couple of seconds to start, no black frames); HUD and crosshair looked fine with their setup.
### D14 Motion blur on the HUD weapon
SSS per-object motion blur (e.g. only the magazine blurs on reload) looks broken: drawing the weapon and walking forward, the blur smears / moves around the weapon; maybe motion blur in general. First find whether it is ours (A/B against the `dev-mt-mcp` exe and DLSS / FG / scale off), then fix.
### D15 Transparent iron sights shift while aiming
GAMMA's ADS feature that makes the iron sight transparent: the transparent part shifts around a bit. Not only the transparent part: the separate iron-sight model itself shifts too, worst on the SKS. A/B first, then fix.
Order: D14 → D15. Both DONE by `70673ce1` (one cause), confirmed by the user 2026-11-19.

## Open questions
- Dependencies: DLSS SDK and AMD FSR SDK approved by the user 2026-11-18 (local/private; licences get sorted by the user before any public release).
- MSAA with the upscaler: plan says refuse with a log line; confirm.
- HDR10 with DLSS and with the bridge.
