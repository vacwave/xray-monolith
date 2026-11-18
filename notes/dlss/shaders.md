# GAMMA shader contract (what mod shaders assume)
Source: reader report 2026-11-18 over enabled mods in `C:\GAMMA\profiles\G.A.M.M.A\modlist.txt`; load order (who wins a file) not analysed. DX11 shaders = `gamedata\shaders\r3`.

## Who ships r3 shaders (enabled)
- 190 Screen Space Shaders 23 (192 files) · 188 Enhanced Shaders (52) · 410 3DSS for GAMMA (~42) · 76 Boomsticks and Sharpsticks (21) · 290 Atmospherics (13) · 189 Beef's NVG (8) · 119 HD Glowsticks (4) · 409 Mark Switch (3) · 185 Shader Driven Scopes (2: fakescope.ps, night_vision.h) · a few single-file mods. Engine repo itself: ~33 files.

## Resolution assumptions
- `screen_res` (xy size, zw texel) is the main one: SSS 37 files / 109 uses (screenspace_common.h, ssfx_taa.ps, combine_1.ps, pp_blur.ps, ssfx_glass.ps), 188 combine_1/combine_volumetric, 410 models_scope_reticle.ps, 189 pp_blur.ps. Engine binder: `binder_screen_res` (Blender_Recorder_StandartBinding.cpp:1527).
- `pos_decompression_params` × pixel coords: ssfx_ao/ssr/sss/water/fog_scattering/motion_blur, gbuffer_stage.h.
- Fixed fractions of screen_res: pp_blur.ps (/2 DOF test, /4, /8 bokeh), ssfx_water_blur.ps (/2, SSR "always half res"), ssfx_rain.ps (/8), ssfx_bloom_combine.ps pixel offsets, combine_volumetric.ps (noise 512 vs screen_res), accum_volumetric_sun.ps (`ResScale`).

## Samplers bound by name
- s_position, s_accumulator, s_image, s_bloom, s_distort used across SSS / 188 / 189 / 185 / 410 / 76.
- Temporal: SSS `s_prev_pos` (ao, il, ssr, sss), `s_current`/`s_previous`/`s_mv` (ssfx_taa.ps); 410 `s_prev_frame` (scope reticle, transparent HUD).
- Scopes: 185 `s_scope`, `s_blur_2`; 410 `s_heat`, `s_tonemap`, `s_hud_rain`.

## SSS temporal code
- TAA: ssfx_taa.ps (unjitter via `ssfx_jitter`, neighbourhood clamp), ssfx_taa_prepare.ps, ssfx_taa_sharp.ps.
- Motion vectors: screenspace_mvectors.h (jitter applied there and in gbuffer_stage.h:141), included by ~60 .vs/.ps; ssfx_sky_mv; grass/tree prev bender arrays (deffer_grass.vs, deffer_tree_branch_bump-hq.vs).

## Motion-vector audit (D2, SSS 23 shaders, read only) [checked]
- Convention: SSS writes `ssfx_mv_calc` = (cur − prev) NDC × (0.5, −0.5) = uv delta, z = HUD flag, w = TAA mask; `vel_curr` is taken before `ssfx_taa_jitter`, so vectors are unjittered. Clear value 0 = "no motion".
- Static level geometry (deffer_base_*, terrain, lod): `m_wvp_prev` / `m_vp_prev` with identity world → camera motion correct.
- Skinned (NPCs, mutants, world weapons): deffer_model_* with `I.P_prev` from `sbones_array_prev` + `m_wvp_prev` from SkeletonX (prev object matrix) → correct, only when `o.ssfx_motionvectors`.
- HUD weapon/hands: deffer_model_hud_* (skinned) with HUD prev view/projection (CHudInitializer) → correct, IsHUD = 1.
- Rigid non-skinned dynamic visuals: `m_wvp_prev` used prev world = identity → wrong; fixed engine-side (prev world = current world, so correct while the object stands still, moving rigid objects show camera motion only).
- Grass (deffer_grass) and trees (deffer_tree_*): prev wind/bender data (`ssfx_wind_anim_prev`, `benders_prevpos`, `prev_wave`) → sway included, correct.
- Sky: `ssfx_sky_mv` with `mSky_prev` (drawn into the MV buffer before the G-buffer) → rotation only, correct.
- No vectors written (keep 0 or the surface below): particles, wallmarks (inherit the surface), water/glass/forward transparents, `deffer_detail_s_flat` (jittered, no velocity output), effects except `effects_rain`. Upscaler sees them as static: possible ghosting (D4).
- Other mods replacing `deffer_*` shaders (188, 410, 76) may drop the velocity output: load order not checked.

## SSS settings for tests (user 2026-11-18)
- SSS is driven by MCM keys `ssfx_module/...` in axr_options.ltx `[options]` (its scripts push them into the `ssfx_*` cvars at runtime; user.ltx values are not the truth).
- The user plays with everything at minimum: copy in `sss_mcm_user_min.ltx` (AO/IL/SSR/water/fog/bloom/motion blur intensities 0, SSS TAA `enabled_mcm = false`, jitter 0, volumetrics 0, interactive grass off). So in the user's setup `ssfx_jitter` is zero and SSS TAA never runs: the upscaler must bring its own jitter (D2).
- Tests: two profiles. "user min" = that file (must look right and not regress). "SSS high" = same keys raised (intensities > 0, TAA on, SSR/AO/IL quality up) for D4 coverage. The user allows changing them freely for tests: edit the slot's private axr_options copy (engine redirect under `-mcp_port`, see `notes/gamestate.md`), never the mod's file in `C:\GAMMA\mods`.
- User's real display: 3840x2160, `r__framelimit 120`, MSAA/SMAA/r2_aa off (C:\Anomaly\appdata\user.ltx). At 4K output DLSS costs ~3.5 ms (K) / 7.5 ms (M) on a 2080 Ti: do one perf check at 4K output, not only at the template's resolution.

## DLSS known defects (D4)
- Walk coverage so far (MO2 SSS high, Cordon, M unless noted): foliage still + strafe, rain, fog, quality strafe: pass. Water, SSR, AO/IL corners, sun/flare, motion blur, DOF/aim, anomalies, impacts, glass, emission, night, all GAMMA-only effects (Enhanced Shaders grading, Beef's NVG, gasmask drops, thermal), Cordon -> Garbage: NOT tested (testers ran out of budget after the defect below).
- Known defect: intermittent whole-frame dark + blue-blurred (sometimes white) DLSS output, both presets (K more often) / cause unknown, not NaN MVs (sanitised), not reset, not exposure flags / idea: log per frame while it happens (evaluate result, jitter, reset, `output_rts_swapped`, which texture is passed as colour), dump `rt_Generic_0` / `rt_dlss_mv` / `rt_dlss_depth` on a bad frame.
- Known defect (by design of the slot position): gasmask drops/dudv, night vision, heat vision, fakescope, DOF, LUT, motion blur and PP-UI run before `phase_upscale`, so DLSS reprojects screen-fixed overlays with scene vectors (smear / lost NV grain expected) / cause: frame order / idea: move the upscale slot before `phase_gasmask_dudv` (frame reorder = coordinator decision; those passes must then work at output size).
- Known defect: particles, smoke, fire, water surface, glass and forward transparents write no motion vectors (they keep the vectors of the surface below) / idea: write SSS-style vectors from their shaders (particle/water/glass shaders are replaced by mods, so our gamedata copies would be overridden). Bias-current-colour mask not built: ignored by the transformer presets (sdk.md).

## Scopes
- 3DSS (410): lens shaders models_scope_reticle(.ps/_precise.ps), scope_3dss_common.h; sample s_image / s_position in screen space (`I.hpos.xy * screen_res.zw`), pixelate uses screen_res.
- SDS (185) fakescope.ps: s_scope at `tc * (1, 0.5625)` (hard-coded 16:9), s_image at tc; no screen_res.
