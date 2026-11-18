# Morning briefing (overnight DLSS run, started 2026-11-18)
Coordinator keeps this current; newest facts replace old ones. Details per goal: `bridge.md`, `pipeline.md`, `sdk.md`.

## Done
- D7 DX12 presentation bridge: commit `0f3f3b45`, merged. `r4_dx12_present 1` (read at start only). Works in vanilla + GAMMA, `vid_mode` resizes OK, cost within noise (~550 fps on vs ~455 off, one sample each). Turns itself off under HDR10.

## Running
- Dev A: D1 render-resolution split.
- Dev B: D8 phase 1 (FSR SDK, FG swapchain in pass-through, HUD-less image); phase 2 waits for D1 + D2.

## Not tested / limits
- Bridge: alt-tab, scope frames, exclusive fullscreen not exercised (MCP runs hidden + windowed). Overlays / ReShade hooking the DX11 swapchain will not see the D3D12 one.

## Decisions taken for you
- No push tonight (SDK licence note); you decide.
- Bridge on → exclusive fullscreen refused, forced borderless with a log line (untested path; FSR FG needs borderless anyway).

## For you to look at
- Pre-existing crash, not from DLSS work: `rs_screenmode windowed` in a hidden MCP run dies in `CHW::UpdateViews` (`dx10HW.cpp` ~1529, depth `CreateTexture2D`, "parameter is incorrect"), bridge on or off. Dump: `dev-mt-mcp\.mcp-run\vanilla-1\dumps\xray_player_11-18-26_23-01-34.mdmp`. Left alone; wants its own goal on `dev-mt-mcp`?
- GAMMA `game_teleport` reported 8 script errors during the D7 launch; nobody looked at them yet.
