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

## SSS settings for tests (user 2026-11-18)
- SSS is driven by MCM keys `ssfx_module/...` in axr_options.ltx `[options]` (its scripts push them into the `ssfx_*` cvars at runtime; user.ltx values are not the truth).
- The user plays with everything at minimum: copy in `sss_mcm_user_min.ltx` (AO/IL/SSR/water/fog/bloom/motion blur intensities 0, SSS TAA `enabled_mcm = false`, jitter 0, volumetrics 0, interactive grass off). So in the user's setup `ssfx_jitter` is zero and SSS TAA never runs: the upscaler must bring its own jitter (D2).
- Tests: two profiles. "user min" = that file (must look right and not regress). "SSS high" = same keys raised (intensities > 0, TAA on, SSR/AO/IL quality up) for D4 coverage. The user allows changing them freely for tests: edit the slot's private axr_options copy (engine redirect under `-mcp_port`, see `notes/gamestate.md`), never the mod's file in `C:\GAMMA\mods`.
- User's real display: 3840x2160, `r__framelimit 120`, MSAA/SMAA/r2_aa off (C:\Anomaly\appdata\user.ltx). At 4K output DLSS costs ~3.5 ms (K) / 7.5 ms (M) on a 2080 Ti: do one perf check at 4K output, not only at the template's resolution.

## Scopes
- 3DSS (410): lens shaders models_scope_reticle(.ps/_precise.ps), scope_3dss_common.h; sample s_image / s_position in screen space (`I.hpos.xy * screen_res.zw`), pixelate uses screen_res.
- SDS (185) fakescope.ps: s_scope at `tc * (1, 0.5625)` (hard-coded 16:9), s_image at tc; no screen_res.
