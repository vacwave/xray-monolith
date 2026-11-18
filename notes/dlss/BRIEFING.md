# Morning briefing (overnight DLSS run, started 2026-11-18)
Coordinator keeps this current; newest facts replace old ones. Details per goal: `bridge.md`, `pipeline.md`, `sdk.md`.

## Done
- D7 DX12 presentation bridge: commit `0f3f3b45`, merged. `r4_dx12_present 1` (read at start only). Works in vanilla + GAMMA, `vid_mode` resizes OK, cost within noise (~550 fps on vs ~455 off, one sample each). Turns itself off under HDR10.
- D1 render-resolution split: commit `28c655cb`, merged (`1339888a`). `r4_render_scale 0.5-1.0` + `vid_restart`; bilinear upscale after the SSS TAA slot; 1.0 = old path. Vanilla + GAMMA (SSS min) day/rain/menu/inventory/PDA/HUD clean, ~12 `vid_restart` toggles without crash. No gamedata change.
- D8 phase 1: commit `f0cc4f7a`, merged (`fd15e060`). FSR SDK v2.4.0 (official tag zip, signed DLLs unmodified, licence in `sdk\include\FidelityFX\license.md`); `r4_framegen 1` creates the FSR FG swapchain (3.1.7 / FG 3.1.6 on the 2080S) on the bridge in pass-through, no interpolation yet. ~0.2 ms extra per frame vs the plain bridge; resize, graceful exit, fallback log lines pass (vanilla).

## Running
- Dev A: D2 temporal inputs (jitter, motion-vector resolve, reset flag, debug view).
- Dev B: waiting for D2, then D8 phase 2 (feed depth / vectors / jitter, HUD-less capture hook after `phase_combine`, turn interpolation on).

## Not tested / limits
- Bridge: alt-tab, scope frames, exclusive fullscreen not exercised (MCP runs hidden + windowed). Overlays / ReShade hooking the DX11 swapchain will not see the D3D12 one.
- D1: GAMMA night and the "SSS high" profile skipped (moved into the D2 test); vanilla night/rain checks weak (dark scene, no puddles); muzzle flash at 0.67 not caught on a screenshot; scopes untested (D5).
- D1 known: render scale forced to 1.0 under MSAA or HDR10 (log line). HUD weapon + HUD UI (not 2D UI) are drawn before the upscale, so they are upscaled too. combine_2 AA neighbour offsets are in output pixels.

## Decisions taken for you
- No push tonight (SDK licence note); you decide.
- Bridge on → exclusive fullscreen refused, forced borderless with a log line (untested path; FSR FG needs borderless anyway).
- Frame order kept: forward/HUD pass stays before the upscale slot (moving it is a bigger change; revisit if HUD UI looks soft under DLSS).

## For you to look at
- Pre-existing crash, not from DLSS work: `rs_screenmode windowed` in a hidden MCP run dies in `CHW::UpdateViews` (`dx10HW.cpp` ~1529, depth `CreateTexture2D`, "parameter is incorrect"), bridge on or off. Dump: `dev-mt-mcp\.mcp-run\vanilla-1\dumps\xray_player_11-18-26_23-01-34.mdmp`. Cause found: windowed mode sizes the swapchain from `GetClientRect`, a minimized MCP window gives 0x0. Left alone; wants its own goal on `dev-mt-mcp`?
- GAMMA `game_teleport` reported 8 script errors during the D7 launch; nobody looked at them yet.
