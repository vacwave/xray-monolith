# DX11 (R4) render facts for DLSS / frame generation
Source: reader reports 2026-11-18 (not line-verified unless marked [checked]). Paths relative to `src\`.

## Frame order
- `CRender::Render` Layers/xrRenderPC_R4/r4_R_render.cpp:72 → phase_scene_prepare :121 → HUD mask into `rt_ssfx_hud` :129-134 → scene G-buffer (phase_scene_begin :169, HUD weapon :199-201) → wallmarks → ssfx sss :250/:266 → lights/accumulator :287-327 → volumetric blur :339 → `Target->phase_combine()` :347.
- `phase_combine` (r4_rendertarget_phase_combine.cpp:41): SSAO :70-87 → ssfx ao/il/ssr/water/rain/fog/motion blur/bloom (:120-550) → phase_bloom → fakescope :587 → `phase_smaa` :593-598 → `phase_ssfx_taa` :600-603 [checked] → combine into rt_Color :624 → flares → `render_forward` :431 (HUD UI + sorted HUD into `HW.pBaseRT`, r4_R_render.cpp:427-461) → `OnRenderPPUI_main` :432 → `phase_pp` :757 (final quad to `HW.pBaseRT`, r4_rendertarget_phase_PP.cpp:114-162).
- 3D UI (`RenderUI3DDirect` r4_R_render.cpp:356) draws into `HW.pBaseRT` after phase_pp. 2D UI path and its exact place: not traced yet.

## Render targets
- All created in `CRenderTarget::CRenderTarget` (r4_rendertarget.cpp:350, creates from :489) from `Device.dwWidth/dwHeight` only; no render-scale variable anywhere on DX11. Viewport comes from the first RT in `u_setrt` (:46-87).
- Full size: `$user$position` (A16B16G16R16F, eye-space xyz, what shaders sample as s_position), albedo, accum, generic 0/1/2, `rt_secondVP`, `rt_fakescope`, all main `rt_ssfx_*` (taa, prev_frame, motion_vectors, accum, ssr, water, ao, il, sss).
- Fractions of screen size: blur /2 /4 /8, HDR10 half res, ssfx bloom chain /2…/64, ssfx volumetric + rain /8, half depth. Fixed: water waves 512², LUM 64/8, shadow maps, UI icon atlas.
- Depth: `HW.pBaseZB` D24S8 (dx10HW.cpp:1502), not reversed-Z (near 0, far 1).

## AA / scaling today
- MSAA `r3_msaa` (RTs get SampleCount), SMAA `r2_smaa` (rendertarget_phase_smaa.cpp:4), SSS TAA `ssfx_taa` (x enable, y jitter, z sharpness; rendertarget_phase_smaa.cpp:105). No FXAA, no FSR/CAS/render scale (`r__supersample` is R1 only).
- SSS TAA only exists when `r3\ssfx_taa.ps` is installed (`o.ssfx_taa`, r4.cpp:490) [checked].

## Jitter / motion vectors
- Jitter is NOT in the projection matrix: constant `ssfx_jitter` (Blender_Recorder_StandartBinding.cpp:1169-1195, zero unless SSS TAA is on) is applied by the SSS shaders themselves (gbuffer_stage.h, screenspace_mvectors.h) [checked].
- `rt_ssfx_motion_vectors` (`$user$ssfx_motion_vectors`, A16B16G16R16F) = 4th MRT of the scene pass (r4_rendertarget_phase_scene.cpp:48-49); shaders in gamedata write it.
- Previous matrices: R_Backend_xform.h:20-21 (`m_vp_prev`, `m_wvp_prev`, ...), constants at StandartBinding.cpp:1475-1479; skinned meshes set `m_wvp_prev` (SkeletonX.cpp:90-102); `m_previous` (r__dsgraph_render.cpp:405) is camera reprojection only. Per-object motion for static/HUD/grass/trees/particles: not found engine-side (grass/trees have prev bender arrays in SSS shaders).
- Frame matrices saved at r4_R_render.cpp:230-241, skipped on SVP frames.

## Scope second viewport (SVP)
- `Device.m_SecondViewport` (xrEngine/device.h:156-171); `IsSVPFrame` = active && dwFrame % frameDelay(2) == 0 (device.cpp:998): every 2nd frame is a scope frame.
- SVP frame renders at full res, `CRender::RenderToTarget` (r4_R_render.cpp:477-498) CopyResource's swapchain buffer 0 into `rt_secondVP` (`$user$viewport2`), and Present is skipped (dxRenderDeviceRender.cpp:484-486).
- Skipped on SVP frames: motion matrix save, SSS, SSR/water/blur, point/spot accumulation. Shader flag `ssfx_issvp`.
- 3D shader scopes also use `$user$scopeRT` and `$user$temp_zb`; reticle via `render_Reticle` r4_R_render.cpp:466-475.

## Render resolution split (D1) [checked]
- `r4_render_scale` (0.5-1, xrRender_console) is read in the `CRenderTarget` ctor (vid_restart); MSAA or HDR10 force 1.0 with a `! r4_render_scale ... ignored` line; scaled start logs `* render size WxH, output size WxH`.
- `CRenderTarget::render_width/height` = render size; `scr_width()/scr_height()` = render size while `scene_size_active` (`begin_scene_size` before `phase_scene_prepare` until `phase_upscale`), output size otherwise. All phase code, `screen_res`, `pos_decompression_params(2)`, `ssfx_jitter`, lights_render volumetric viewport and fluid cbuffers use it; R2/R3 have Device-size stubs because the `xrRender\rendertarget_phase_*` files compile into R2/R3/R4.
- Scene depth: `rt_scene_zb` (`$user$scene_zb`, D24S8) is put into `HW.pBaseZB` from `begin_scene_size` to `phase_upscale` (every scene pass binds `HW.pBaseZB`); `end_output_size` (end of `CRender::Render`, end of `render_menu`) restores. D3D11 drops `OMSetRenderTargets` when RTV and DSV sizes differ, so `phase_occq` binds no colour RT when scaled.
- Upscale slot `phase_upscale` (phase_combine, after `phase_ssfx_taa` + prevPos copy): `CBlender_upscale` (base `stub_notransform_t` + `copy_nomsaa`, its `smp_nofilter` stage forced bilinear) copies generic0 -> `rt_Generic_0_out`; then `swap_output_rts` swaps `rt_Color`/`rt_Generic_0` with output twins (ref_rt + their CTexture surfaces, so `$user$albedo` / `$user$generic0` bound by name in combine_2 and `postprocess.s` see output size) and unbinds cached SRVs with an empty texture list. generic1 (distortion mask) stays render size there; `render_menu` swaps generic1 too.
- Output size: `rt_secondVP`, `rt_ui_pda` (CopyResource from the swapchain), async screenshot staging, the `_out` twins, 3D icon atlas; every other RT at render size. `render_forward` (sorted, HUD UI, attached UI) and PP-UI run before the slot, so they are upscaled; 2D UI after `Render` is at output size.
- Gotcha: `CBackend::Invalidate` is private; mid-frame SRV cache reset = `RCache.set_Textures(&emptyList)`. Base shader sources (copy*.ps, postprocess.s, stub_*.vs) are unpacked in `C:\Anomaly\tools\_unpacked\shaders\r3`.

## Temporal inputs (D2) [checked]
- Interface on `CRenderTarget` (r4_rendertarget.h, R4 only): `upscaler_enabled` (D3 sets it), `temporal_active` (this main-view frame), `jitter_px` / `jitter_px_prev`, `history_reset`, `request_history_reset()`, `rt_dlss_mv` (`$user$dlss_mv`), `rt_dlss_depth` (`$user$dlss_depth`).
- "Upscaler on" = `upscaler_enabled || r4_upscale_debug != 0`, and only with `o.ssfx_motionvectors` (SSS `r3\screenspace_mvectors.h` installed) and no MSAA; `r4_render_scale` alone does not turn it on (bilinear D1 path stays unjittered).
- Vanilla (no SSS): no motion vectors and no shader applies `ssfx_jitter`, so the upscaler is unavailable: one log line `! temporal upscaler inputs unavailable: needs SSS motion vectors (...)`, frame unchanged.
- `begin_temporal_frame()` (start of `begin_scene_size`) decides `temporal_active`, creates the RTs + `s_temporal` on first use (render size, so a vid_restart recreates them), advances jitter, sets `history_reset`.
- SVP (scope) frames: `temporal_active` false, no jitter, no inputs; the next main frame has `history_reset` (prev matrices on it are the scope camera's, see "Scope second viewport").
- Jitter: Halton(2,3) − 0.5 in render pixels, phase count 8 × (output/render width)² clamped 8..64, advances once per active frame; `jitter_px` = screen offset of the geometry (+x right, +y down), so the point sampled at pixel centre c is c − jitter_px. Not in the projection matrix.
- `ssfx_jitter` binder (StandartBinding.cpp): while `temporal_active && scene_size_active` it sends (2·jx/render_w, −2·jy/render_h, ssfx_taa.x, ssfx_taa.w) = the clip-space offset the SSS vertex shaders add (`hpos.xy += ssfx_jitter.xy * hpos.w`); otherwise the old SSS TAA pattern. After the upscale slot nothing is jittered.
- History reset (one flag, `history_reset`, valid while `temporal_active`): new target (vid_restart / resolution / render scale change), frame gap (`Device.dwFrame` not previous+1: loading, menu, SVP frame, first frame after reset), other `g_pGameLevel`, camera jump > 5 m in a frame (teleport / cut), upscaler just turned on, `request_history_reset()`.
- Resolve `phase_temporal_inputs()` in phase_combine right before `phase_upscale` (render size, after SSS TAA + prevPos copy), shader `upscale_inputs.ps` (our gamedata): MRT `rt_dlss_mv` + `rt_dlss_depth` from `rt_ssfx_motion_vectors` and `$user$position`.
- `rt_dlss_mv` (RG16F, render size): previous − current pixel position in render pixels (+x right, +y down), i.e. prev = cur + mv; jitter NOT included (SSS computes both positions unjittered); = −SSS mv.xy × render size. DLSS: MVScale (1,1), no jittered-MV flag. HUD mask stays in `rt_ssfx_motion_vectors.z` (1 = HUD), not copied.
- `rt_dlss_depth` (R32F, render size): hardware depth of the main projection, 0 near .. 1 far, NOT reversed, = `_33 + _43 / z_view` from `$user$position.z`; nothing drawn (sky) = 1.0. HUD weapon pixels carry the HUD view depth through the main projection. Not the D24S8 buffer (that has no SRV at scale 1).
- Debug `r4_upscale_debug` 1/2 (`phase_upscale_debug`, after `phase_pp`, over `HW.pBaseRT` at output size, shader `upscale_debug.ps`): 1 = mv as rg = 0.5 + 0.5·mv/(|mv|+8) (grey = still), 2 = view distance z/(z+20) (black near, white sky).
- `set_xform_world` now also sets the previous world matrix to the same value (R_Backend_Runtime.h): rigid non-skinned dynamic objects used to get prev world = identity (garbage vectors); skinned (SkeletonX) and sky set their own.

## DLSS (D3)
- `CDlss Dlss` (Layers/xrRenderPC_R4/r4_dlss.*): NGX init lazily on the first target that wants DLSS, `Dlss.shutdown()` in `CRender::destroy`; the feature is created on the first evaluate and released in `~CRenderTarget` (so vid_restart recreates it). Unavailable reasons are logged once as `! DLSS unavailable: ...` (not NVIDIA, DLL missing, NGX init, driver, MSAA, HDR10, no SSS motion vectors).
- `r4_dlss` (off/dlaa/quality/balanced/performance/ultra_performance) and `r4_dlss_preset` (default/k/m/l; default = M below device id 0x2600 = RTX 20/30 (K broken there, sdk.md), NGX default above) are read in the `CRenderTarget` ctor = applied on vid_restart. DLSS on: render size = NGX optimal size (overrides `r4_render_scale`), `r3_msaa` forced off in `CRender::create` with a log line, HDR10 = DLSS off.
- `dlss_target` (this target) / `dlss_active` (this frame = `dlss_target && temporal_active`); `begin_scene_size` sets `upscaler_enabled = dlss_target` and the sampler mip bias `r__tf_mipbias + log2(render/output) - 1` (every frame through `SSManager.SetMipLODBias`, no-op when unchanged).
- DLSS on → output twins exist even at DLAA (render = output size); `rt_Generic_0_out` gets a UAV. `phase_upscale` → `phase_dlss` (unbinds all RTs through RCache, evaluates generic0 → generic0_out with `rt_dlss_depth`, `rt_dlss_mv`, `jitter_px`, `history_reset`, flag MVLowRes, exposure texture 1.0) → `swap_output_rts`; evaluate false (SVP frame, failure) → D1 bilinear copy. 30 failures in a row or a failed create = bilinear until vid_restart. SMAA and SSS TAA skipped while `dlss_active`.

## Swapchain / present / pacing
- `CHW::CreateDevice` Layers/xrRenderDX10/dx10HW.cpp:303; `CreateSwapChainForHwnd` :585, FLIP_DISCARD, 2 buffers, R8G8B8A8_UNORM or R10G10B10A2 (HDR10, :588-607); ResizeBuffers :752/:872/:879/:1121/:1167.
- Present: dxRenderDeviceRender.cpp:485 (interval from vsync, ALLOW_TEARING when windowed + VRR).
- Limiter `r__framelimit`: busy-wait in xrEngine/device.cpp:501-524 (ECO_RENDER). No Reflex / NVAPI latency calls (nvapi.lib is linked only).
- MCP screenshots copy the back buffer before Present (see `notes/render.md`): must keep working with the bridge.
- `r4_dx12_present 1`: `HW.m_pSwapChain` is a proxy over a D3D12 swapchain, back buffer = shared texture (see `bridge.md`). Note `CHW::CreateDevice` makes the D3D11 device on the default adapter (`D3D11CreateDevice(nullptr, ...)`), not on `m_pAdapter` [checked].
- Gotcha: NGX's D3D11 evaluate restores the previous viewport, which is the render-size one from `phase_temporal_inputs`. After a DLSS evaluate, `phase_upscale` must set the output-size viewport again, otherwise every later pass draws into the top-left corner.

## Sharpening (D10)
- `r4_upscale_sharpness` (float 0-1, default 0 = no pass, runtime): `CRenderTarget::phase_sharpen` (r4_rendertarget_phase_sharpen.cpp, blender class inside the file) runs right after `phase_upscale` in phase_combine, at output size, before combine_2 / flares / `phase_pp` / 2D UI.
- Runs on main-view frames whenever sharpness > 0: native without upscaler, `r4_render_scale` bilinear, DLSS/DLAA. Skipped on SVP (scope) frames (D5 decides) and under HDR10 (generic0 is FP16 above 1.0 there; RCAS limits assume LDR 0..1).
- Source != destination: lazily created `rt_sharpen_src` (`$user$sharpen_src`, output size, generic0's format) = `CopyResource` of generic0; RCAS reads it and writes back into `rt_Generic_0`. Same code with and without the output twins (at native res generic0 is already output size).
- Shader `r3\upscale_sharpen.ps` = AMD FidelityFX SDK 2.4.0 `FsrRcasF` (fsr1/ffx_fsr1.h, 32-bit, with the newer `lowerLimiterMultiplier`, passthrough alpha, no denoise), MIT header kept. Loads by pixel (SV_Position); off-screen taps read 0 = no sharpening on the border pixels.
- Mapping as FSR2/3 (`sharpenessRemapped = 2 - 2s`): `rcas_con.x = 2^(2s - 2)`, so any value > 0 starts at ~0.25 of max (0 = pass off).
- Frame generation: the HUD-less capture (`DX12Present_CaptureHudless`, r4_R_render.cpp right after `phase_combine`) already contains the sharpened image.
- The 3D in-hand PDA screen is part of the scene (drawn before the slot), so it is sharpened with the world; 2D HUD / inventory are not (D10 test). Pass cost at 1080p native: not measurable (~0 ms, noisy).
