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

## Swapchain / present / pacing
- `CHW::CreateDevice` Layers/xrRenderDX10/dx10HW.cpp:303; `CreateSwapChainForHwnd` :585, FLIP_DISCARD, 2 buffers, R8G8B8A8_UNORM or R10G10B10A2 (HDR10, :588-607); ResizeBuffers :752/:872/:879/:1121/:1167.
- Present: dxRenderDeviceRender.cpp:485 (interval from vsync, ALLOW_TEARING when windowed + VRR).
- Limiter `r__framelimit`: busy-wait in xrEngine/device.cpp:501-524 (ECO_RENDER). No Reflex / NVAPI latency calls (nvapi.lib is linked only).
- MCP screenshots copy the back buffer before Present (see `notes/render.md`): must keep working with the bridge.
