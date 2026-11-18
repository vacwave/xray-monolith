# DLSS / FSR SDK facts
Source: web research agent 2026-11-18 (URLs = where it read them; "inference" = not sourced). Guide = DLSS Programming Guide 31 Mar 2026, github.com/NVIDIA/DLSS/blob/main/doc/DLSS_Programming_Guide_Release.pdf.

## DLSS Super Resolution
- Latest: DLSS SDK v310.9.16 (github.com/NVIDIA/DLSS/releases), Streamline v2.14.16.
- "DLSS 4.5" = 2nd-gen transformer SR model = presets M (default for Performance) and L (Ultra Performance); K (DLSS 4 transformer) stays default for DLAA/Quality/Balanced. Presets L/M added in 310.5.0 (Guide §3.12).
- D3D11 is supported by direct NGX: `NVSDK_NGX_D3D11_Init_with_ProjectID`, `NGX_D3D11_EVALUATE_DLSS_EXT`; it preserves immediate-context state (Guide §5.2). No NVIDIA application ID needed (project-ID GUID, CUSTOM engine type).
- RTX 20: SR/DLAA supported, all presets allowed; L/M are tuned for RTX 40+ (no FP8 on 20/30). Cost on a 2080 Ti, Performance mode, 1080p/1440p/4K output: K 1.16/1.80/3.50 ms, M 1.95/3.41/7.51 ms, L 3.51/5.45/10.60 ms. VRAM K ~86/144/307 MB, M ~119/207/459 MB. Inference: default K on the 2080 Super, M opt-in.
- Inputs: colour at render res (any format); depth any depth format (D24S8 ok, flag if inverted); motion vectors RG16F/RG32F in screen-space pixels at render res (`MVLowRes`), other encodings must be decoded first; jitter in pixels every evaluate (Halton recommended); exposure optional (auto-exposure flag otherwise; L always auto).
- Mip bias: native + log2(render/display) - 1.0.
- Licence (github.com/NVIDIA/DLSS/blob/main/LICENSE.txt): redistribute the unmodified signed `rel` DLL inside an application, with NVIDIA notices; must not make the SDK subject to an open-source licence. Fit with this engine's licence: NOT verified.

## Frame generation
- DLSS FG: D3D12/Vulkan only via Streamline swapchain + Reflex; RTX 40/50 only. Not usable here.
- AMD FSR SDK 2.4.0 "Redstone" (github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK): FG 3.1.6 (analytical) / 4.0.2 (ML, RX 9000 only), FG swapchain 3.1.7 / 4.0.0. D3D12 or Vulkan only, no D3D11 backend.
- FG swapchain = an `IDXGISwapChain4` on a D3D12 device with its own pacing + present threads (gpuopen.com/manuals/fsr_sdk/techniques/frame-interpolation-swap-chain/).
- FG 3.1.x needs only typed UAV load + R16G16B16A16_UNORM: inference it runs on the 2080 Super (no official table found).
- Works with any upscaler: "Frame Generation Prepare" takes pre-upscale motion vectors + depth (so DLSS upscaling + FSR FG is a supported shape).
- UI: three modes (callback, separate UI texture, HUD-less colour). Tearing/VRR only in fullscreen. Designed for 60+ fps input, works down to 30.
- Licence: AMD custom, binary redistribution with notices.

## DX11 renderer + DX12 frame generation (precedent)
- Skyrim SE Community Shaders: creates a D3D12 device + swapchain, copies the D3D11 frame into a shared D3D12 resource, runs FSR FG (PR community-shaders/skyrim-community-shaders#2430); reported windowed/borderless only.
- OptiScaler: D3D11on12 path costs "up to 10-15%"; its FG breaks Steam/RTSS overlays.
- Not found: measured latency / fence cost of shared-handle interop.
- Driver FG (NVIDIA Smooth Motion RTX 40/50, AMD AFMF Radeon only): not usable on the 2080 Super.

## FSR SDK in the repo (D8)
- FSR SDK v2.4.0 (tag v2.4.0, 2026-10-28) from github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK `archive/refs/tags/v2.4.0.zip` (the release has no separate assets; the zip holds the signed DLLs in `Kits/FidelityFX/signedbin`).
- In repo: headers `sdk\include\FidelityFX\{api,framegeneration}\include\` (SDK layout kept, the headers include each other relatively), DLLs `sdk\binaries\FidelityFX\amd_fidelityfx_loader_dx12.dll` + `amd_fidelityfx_framegeneration_dx12.dll` (Authenticode valid, FG DLL file version 4.0.2.4555, unmodified), licence `docs/license.md` copied to both folders as `license.md`. Upscaler/denoiser/VK DLLs and the loader .lib not taken (runtime loading).
- Header versions: FG swapchain DX12 4.0.0, FG 4.0.2; runtime picks the provider per GPU (non-RDNA4 -> FG swapchain 3.1.7, FG 3.1.6). Requires typed UAV load + R16G16B16A16_UNORM.

## DLSS SDK in the repo (D3)
- DLSS SDK v310.9.16 from github.com/NVIDIA/DLSS `archive/refs/tags/v310.9.16.zip` (the repo zip holds headers, libs and signed DLLs; the release assets are only demos).
- In repo: headers `sdk\include\DLSS\` (included as `<DLSS/nvsdk_ngx.h>`), import lib `sdk\libraries\x64\DLSS\nvsdk_ngx_d.lib` (/MD, linked by `#pragma comment` in r4_dlss.cpp; `x64/` is gitignored, added with -f), runtime `sdk\binaries\DLSS\nvngx_dlss.dll` (rel, Authenticode valid, 310.9.16.0, unmodified, ~59 MB); `LICENSE.txt` copied into all three folders. Deploy: the DLL next to the exe (NGX searches the exe folder); never in gamedata.
- App id: `NVSDK_NGX_D3D11_Init_with_ProjectID` with a fixed project-id GUID (`dlss_project_id` in r4_dlss.cpp), `NVSDK_NGX_ENGINE_TYPE_CUSTOM`, engine version "1.0"; NGX logs go to `$logs$`.
- Guide facts used: D3D11 evaluate preserves the immediate-context state (§5.2.5); jitter in render pixels, same axes as the motion vectors (§3.7.3); mip bias = native + log2(render/display) − 1 (§3.5).
- D3 test (RTX 2080 Super, 1080p output, LDR R8G8B8A8 colour): preset M (hint 13) is correct in every mode. Preset K (hint 11, "default" on RTX 20/30) breaks the whole frame. With AutoExposure it flickers white/dark/magenta; without an exposure input it does the same; with an explicit 1x1 R32F exposure of 1.0 it stays dark and blurred. The cause is not found yet. K reports 85 MB VRAM, M 170-345 MB.
- Perf at 1920x1080, r__framelimit 0, MO2 profile with SSS, preset M (noisy): off 6.35 ms; dlaa 13.5; quality 6.8; balanced 5.6; performance 4.5; ultra_performance 3.4.
- Decision (coordinator, D3): `r4_dlss_preset default` = M on RTX 20/30; superseded in D4 (below).
- D4 preset K probe (one session, temporary `r4_dlss_dbg` bitmask, MO2 Cordon, quality 1280x720 -> 1920x1080, one fixed view for ~5 min): with the release DLL K looked like M at dbg 0 and with IsHDR, no exposure texture, auto-exposure, 16F output, hint 0, frame time, forced reset, zero MVs, DepthInverted. `history_reset` was 0 every frame; colour/output RGBA8 (fmt 28), output UAV ok.
- But the next session (K as default, walking around Rookie village, quality and performance) showed the D3 symptom again, intermittently: whole-frame dark-blurred "blobs" for tens of seconds, single white frames, normal frames in between; often normal right after vid_restart. NVIDIA's dev DLL (`lib/Windows_x86_64/dev`) gave a white 3D view with K at once (M normal). So K on the 2080 SUPER + D3D11 stays broken; cause not found. Ruled out: exposure setup, IsHDR, output format, alpha, reset flag, driver/app preset override (NGX log says "Using App hint Preset K"), NaN/inf/huge MVs (sanitised in `upscale_inputs.ps`, kept). Left: an NGX/driver issue with K on Turing D3D11 (try a newer driver or the D3D12 path via the bridge later), or input content that only K reacts to (try zeroing depth/MV per region with a debug path while it is broken).
- Dev DLL: watermark only, no preset text without the HKLM `ShowDlssIndicator` key; never ship it.
- NGX log (callback at level ON, filtered to `* NGX:` lines with the snippet path/version and "Using App hint Preset X"): no driver / NVIDIA-app override seen; snippet = our `nvngx_dlss.dll` 310.9.16. Harmless NGX lines: NGXCore 126 load errors from the exe folder, cache misses.
- Perf K vs M (1080p output, uncapped, MO2 SSS defaults, GPU-bound, K possibly in its broken state): quality 4.3 vs 6.7 ms, balanced 3.8 vs 5.4, performance 3.3 vs 4.3; off 6.0 ms. VRAM K 85 MB.
- Decision (D4, time box spent): `default` stays M on RTX 20/30; `k` selectable.
- D4 session 3: the SAME whole-frame dark + blue-blurred frames appeared with preset M (log hint 13), performance mode, first DLSS use after launch (new game, teleports), ~12 of 21 frames in a 6 s still burst, good and bad frames alternating within 0.3 s; gone later in the same run (quality, then performance again clean). So the "K is broken" symptom is probably a preset-independent intermittent pipeline/input bug that K hits more often. Not solved; screenshots `scratchpad\s3\montP.jpg` (D4 dev scratchpad).
- Bias current colour mask: Guide §3.15 (310.4+) says the transformer presets (J/K/L/M) do not use it, only the removed preset F, "should not be used". Not implemented.
