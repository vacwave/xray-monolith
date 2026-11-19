# Morning briefing (overnight DLSS run, started 2026-11-18)
Coordinator keeps this current; newest facts replace old ones. Details per goal: `bridge.md`, `pipeline.md`, `shaders.md`, `sdk.md`.

## Done (all merged into `dev-mt-mcp-dlss`, nothing pushed)
- D7 DX12 presentation bridge `0f3f3b45`: `r4_dx12_present 1` (read at start only). Vanilla + GAMMA OK, `vid_mode` resizes OK, cost within noise. Off under HDR10.
- D1 render-resolution split `28c655cb`: `r4_render_scale 0.5-1.0` + `vid_restart`; bilinear upscale after the SSS TAA slot; 1.0 = old path.
- D8 phase 1 `f0cc4f7a`: FSR SDK v2.4.0 (official tag zip, signed DLLs unmodified, licence in `sdk\include\FidelityFX\license.md`); `r4_framegen 1` creates the FSR FG swapchain on the bridge, pass-through only. ~0.2 ms extra per frame.
- D2 temporal inputs `52c804e2`: engine Halton jitter through `ssfx_jitter`, `rt_dlss_mv` (RG16F pixel vectors), `rt_dlss_depth`, history-reset flag, `r4_upscale_debug 0/1/2`. Debug views correct in GAMMA (still, turn, strafe, NPC, weapon sway, depth, scale 0.67, SSS high). New shaders in our gamedata; `C:\Anomaly\db\mods\00_modded_exes_gamedata.db0` overwritten, old one kept as `.bak_pre_dlss`.
- D8 phase 2 `115cf528`: FSR frame interpolation ON with `r4_dx12_present 1` + `r4_framegen 1` (DLSS off). Log counters: AnomalyMO2+SSS 47.8 → 95.7 fps, at scale 0.67 148 → 296, cap 30 → 60 presented; GAMMA Cordon 151 → 302, VRAM 5.3 of 8 GB (FG context 148 MB + swapchain 34 MB + inputs 16 MB). Held in main menu / loading; resize rebuilds; exit code 0. Without SSS: pass-through + log line.
- D3 DLSS `02b2874f` (merged, head `58bba5bd`; build clean after the merge, no game test after the merge): DLSS SDK v310.9.16 (official tag zip, DLL unmodified, `LICENSE.txt` kept). `r4_dlss off/dlaa/quality/balanced/performance/ultra_performance` + `r4_dlss_preset default/k/m/l`, applied on `vid_restart`. **Preset M works in every mode** (AnomalyMO2+SSS, GAMMA quality still shot): full frame, sharp, no hand/weapon ghosting, some dark smear in foliage when moving. Frame time 1080p preset M (noisy): off 6.35 ms · dlaa 13.5 · quality 6.8 · balanced 5.6 · performance 4.5 · ultra perf 3.4. DLSS VRAM: M 170-345 MB, K 85 MB.
- **Preset K is broken** (whole frame white / dark-blurred / magenta), also in GAMMA; three fixes did not help. Decision: `default` → M for now, K stays selectable; K is investigated at the start of D4 (ideas: NaN/inf in the motion vectors, HDR flag with a float colour copy, driver preset override, SDK dev DLL debug view). Note: at 1080p, M quality is no faster than DLSS off on the 2080S, so K matters for speed.
- D11 low latency `e17b4262` (merged): `r4_reflex 0/1/2` (off / on / on+boost), runtime toggle, NVIDIA Reflex sleep at the start of the frame + latency markers. NvAPI in `sdk\include\nvapi` updated from an R80-era copy to R620 (official NVIDIA repo, `License.txt` kept; `nvapi.lib` replaced, all render projects still build). **No measurable frame-time gain; pacing slightly looser with it on** (p99 uncapped 6.9 → 9.5 ms, cap 30: 34.5 → ~40 ms, cap 60: 17.3 → 22.2 ms). With a frame cap set and Reflex on, Reflex does the capping instead of the engine limiter. With the bridge on it targets the D3D12 device; the FSR interpolation thread is outside what Reflex sees. Whether input lag feels better is for you to judge.
- D10 sharpening `84c8950e` (merged): `r4_upscale_sharpness 0-1` (runtime, 0 = no pass), AMD RCAS right after the upscale; works at native res, with DLSS and with plain render scale; crosshair, HUD text, inventory not sharpened. Cost not measurable at 1080p. Off under HDR10 and on scope frames. The 3D in-hand PDA screen IS sharpened (it is part of the scene) - left as is.

## Running
- Dev A: D4 completion. **Open blocker: DLSS intermittently outputs a dark / white whole frame, with preset M too** (about 12 of 21 frames in a 6 s burst right after the first DLSS use, gone later in the run; K shows it for tens of seconds while walking). So "K is broken" is most likely one pipeline bug that K hits more often. My diagnosis from the code: after the NGX call the engine's render-state cache is stale (only targets, viewport and textures are reset), so the next full-screen pass can draw with NGX's leftover state. Fix being tested: full D3D state clear + cache invalidate after the call. Until that passes, treat DLSS as not ready.
- D4 so far (`ceeeadff`, merged): NaN/inf-safe motion vectors and depth; DLSS / render scale now really go off when HDR10 is toggled on; NGX preset log lines. Frame time K vs M at 1080p (K rows may include its broken state): quality 4.3 vs 6.7 ms, balanced 3.8 vs 5.4, performance 3.3 vs 4.3, DLSS off 6.0.
- D4 findings: no DLSS bias mask (the SDK says presets J/K/L/M ignore it); particles / water / glass still without vectors (needs shader edits that GAMMA mods override); screen-fixed overlays (gasmask drops, night vision, thermal, DOF, LUT, motion blur) run before the upscale, so DLSS smears them → new goal D13 moves them after it. Effect walk mostly not done yet (testers stopped at the blocker).
- D4 `1cc5ff7b` (2026-11-19 ~03:00, on `dlss-a`, merge pending): the dark / white DLSS frames are fixed for preset M: 0 bad frames of 109 (after launch, after 3 `vid_restart`, after inventory, after Esc menu, sharpening 0.5; performance mode 960x540 → 1080p). Cause (inferred from before / after, not proven inside NGX): after `vid_restart` the new DLSS feature was created on the old NGX instance whose parameter map still pointed at destroyed render targets; NGX is now shut down and started again with every render target.
- Preset K is still broken on the RTX 2080 SUPER (bad frames in every condition, raw DLSS output black while every call reports success): separate problem, so `default` stays M and K is opt-in. Your choice later: try a newer driver, or run DLSS on the D3D12 device through the bridge.
- Effect walk (preset M): fine = AO/IL, motion blur, DOF, anomalies, emission, GAMMA grading. NOT tested (nothing found in the test saves): water, SSR puddles, sun / lens flare, night vision, gasmask drops, thermal; glass and night flashlight inconclusive. Night vision / gasmask / thermal are picked up in the D13 test; the rest needs your real-use check.
- Dev A now: D13 (overlays after the upscale), then D5 (scopes).
- D6 `ccabc56d` (merged 2026-11-19 ~02:45): options menu "Upscaling" section (DLSS, sharpness, frame generation, bridge, Reflex) with status / reason lines. Passed on plain Anomaly (reads "unavailable: needs Screen Space Shaders motion vectors"), AnomalyMO2 (entries set, console matches) and GAMMA (its own rows intact, no script errors). The false "Reflex: not an NVIDIA GPU" was only an early log line written before the render device existed; it now reads "unknown: render device not created yet".
- Dev B now: D12 separate UI texture for frame generation (parks if it grows).
- Open choice for you (Reflex + frame cap): keep Reflex as the limiter when a cap is set (current), or let the engine's limiter keep the cap (evener pacing) and use Reflex only for latency?
- Plan extended on your order (PLAN.md "Added by the user 2026-11-19"): particle / transparent vectors + DLSS bias mask in D4; sharpness slider, frame generation, bridge and low-latency entries in D6; new goals D10-D12. My reading of "FSR too": the one sharpness slider (RCAS pass) applies with DLSS, plain render scale and frame generation at native res; FSR upscaling itself is NOT added. Correct me if you meant FSR upscaling as a mode.

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
