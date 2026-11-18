# Morning briefing (overnight DLSS run, started 2026-11-18)
Coordinator keeps this current; newest facts replace old ones. Details per goal: `bridge.md`, `pipeline.md`, `shaders.md`, `sdk.md`.

## Done (all merged into `dev-mt-mcp-dlss`, nothing pushed)
- D7 DX12 presentation bridge `0f3f3b45`: `r4_dx12_present 1` (read at start only). Vanilla + GAMMA OK, `vid_mode` resizes OK, cost within noise. Off under HDR10.
- D1 render-resolution split `28c655cb`: `r4_render_scale 0.5-1.0` + `vid_restart`; bilinear upscale after the SSS TAA slot; 1.0 = old path.
- D8 phase 1 `f0cc4f7a`: FSR SDK v2.4.0 (official tag zip, signed DLLs unmodified, licence in `sdk\include\FidelityFX\license.md`); `r4_framegen 1` creates the FSR FG swapchain on the bridge, pass-through only. ~0.2 ms extra per frame.
- D2 temporal inputs `52c804e2`: engine Halton jitter through `ssfx_jitter`, `rt_dlss_mv` (RG16F pixel vectors), `rt_dlss_depth`, history-reset flag, `r4_upscale_debug 0/1/2`. Debug views correct in GAMMA (still, turn, strafe, NPC, weapon sway, depth, scale 0.67, SSS high). New shaders in our gamedata; `C:\Anomaly\db\mods\00_modded_exes_gamedata.db0` overwritten, old one kept as `.bak_pre_dlss`.
- D8 phase 2 `115cf528`: FSR frame interpolation ON with `r4_dx12_present 1` + `r4_framegen 1` (DLSS off). Log counters: AnomalyMO2+SSS 47.8 → 95.7 fps, at scale 0.67 148 → 296, cap 30 → 60 presented; GAMMA Cordon 151 → 302, VRAM 5.3 of 8 GB (FG context 148 MB + swapchain 34 MB + inputs 16 MB). Held in main menu / loading; resize rebuilds; exit code 0. Without SSS: pass-through + log line.

## Running
- Dev A: D3 DLSS upscaling + DLAA (NGX SDK, `r4_dlss`, `r4_dlss_preset`).
- Dev B: free until D3 merges, then D6 (menu options) while A does D4.

## YOU must check (tools cannot see it)
- Generated frames were NOT seen by anyone: MCP screenshots read the DX11 back buffer = rendered frames only. HUD / crosshair warping, shake or artefacts on interpolated frames need your eyes on a visible window.

## Important findings
- DLSS and frame generation need the SSS motion-vector shader: plain vanilla Anomaly reports "unavailable" (vanilla shaders write no vectors and ignore the jitter; supporting it = shipping a full shader set). So they work in GAMMA / with SSS only, and the real tests need GAMMA (one at a time, so A's and B's tests queue).
- Particles, water, glass and other transparents write no motion vectors → possible ghosting under DLSS / FG (D4).
- While a scope is active the history resets every frame (D5).

## Not tested / limits
- Bridge: alt-tab, scope frames, exclusive fullscreen not exercised (MCP runs hidden + windowed). Overlays / ReShade hooking the DX11 swapchain will not see the D3D12 one.
- D1: vanilla night/rain checks weak; muzzle flash at 0.67 not caught on a screenshot. Render scale forced to 1.0 under MSAA or HDR10. HUD weapon + HUD UI (not 2D UI) are upscaled with the scene.
- FG: scope (SVP) path while aiming not proven (no held/reset log line seen with an SVD); with a 3D scope interpolation runs in reset mode every frame (D5). FG alone does not jitter the frame unless SSS TAA is on. A stats line (rendered / generated / presented fps) is logged every 10 s while interpolation is on.
- D2: swaying grass not confirmed with real grass (none where the tester stood).

## Decisions taken for you
- No push tonight (SDK licence note); you decide.
- Bridge on → exclusive fullscreen refused, forced borderless with a log line.
- Frame order kept: forward/HUD pass stays before the upscale slot.
- Accepted a fix that changes today's frame slightly even with everything off: `set_xform_world` (`R_Backend_Runtime.h`, all renderers) now sets the previous world matrix too, so rigid non-skinned dynamic objects get correct motion vectors (before: identity → wrong SSS TAA / motion blur on them). Say so if you want it gated.
- Vanilla without SSS = upscaler unavailable (log line), not a second vector path.

## For you to look at
- Pre-existing crash, not from DLSS work: `rs_screenmode windowed` in a hidden MCP run dies in `CHW::UpdateViews` (`dx10HW.cpp` ~1529). Cause: windowed mode sizes the swapchain from `GetClientRect`, a minimized MCP window gives 0x0. Left alone; wants its own goal on `dev-mt-mcp`?
- GAMMA `game_teleport` reported 8 script errors during the D7 launch; nobody looked at them yet.
