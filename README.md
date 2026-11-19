# Anomaly DLSS + FSR Frame Generation

Stalker Anomaly 1.5.3 engine (DX11) with NVIDIA DLSS and AMD FSR frame generation. Built on the MT version of [themrdemonized's Modded Exes](https://github.com/themrdemonized/xray-monolith) (`dev-mt`), which has the full list of base engine features.

**Requires [Screen Space Shaders](https://www.moddb.com/mods/stalker-anomaly/addons/screen-space-shaders) (SSS) by Ascii1457.** DLSS and frame generation need its motion vectors. Without SSS the game runs normally and the options show "unavailable".

## Install
1. Download `Anomaly-DLSS_<version>.zip` from [Releases](../../releases). It has `bin\` (DX11 and DX11-AVX exes, the DLSS and FSR DLLs) and `db\mods\00_modded_exes_gamedata.db0`.
2. Extract the zip into the Anomaly root folder and overwrite files if asked.
3. Delete the shader cache in the launcher once, then start the game with the DX11 renderer.

Debug symbols for crash reports are a separate download, `Anomaly-DLSS_<version>_pdb.zip`; extract it into the Anomaly root too.

Settings: Options → Video → Advanced, "Upscaling" section.

## What it adds
- **DLSS** super resolution and DLAA (NVIDIA RTX GPUs), presets K / L / M.
- **FSR 3.1 frame generation** (any DX12-capable GPU). The game still renders in DX11 and presents through a D3D12 swapchain. The HUD and menus are passed to FSR as a separate UI layer.
- **Render scale** without DLSS (plain bilinear upscale).
- **Sharpening** (AMD RCAS) for DLSS, render scale or native resolution. The UI is not sharpened.
- **NVIDIA Reflex** low latency mode.

## Fixes and changes
- Engine-side jitter, motion vectors and depth for the upscaler, with NaN / inf safe inputs.
- Gas mask, night vision, heat vision and fake scope overlays run after the upscale, so DLSS does not smear them.
- Second-viewport scopes keep the DLSS history while aiming; 3DSS scope lenses get correct motion vectors.
- Correct motion vectors for rigid moving objects and single-bone weapon parts (fixes SSS motion blur and iron sights shifting on HUD weapons).
- Recovery when DLSS returns an empty frame after `vid_restart`.
- Fixed a startup crash with SSS when `user.ltx` has no `renderer` line.

## Console commands
| Command | Values | Applied |
|---|---|---|
| `r4_dlss` | `off`, `dlaa`, `quality`, `balanced`, `performance`, `ultra_performance` | `vid_restart` |
| `r4_dlss_preset` | `default`, `k`, `m`, `l` (`default` = M on RTX 20/30, NVIDIA's default on newer cards) | `vid_restart` |
| `r4_render_scale` | `0.5` - `1.0` (ignored while DLSS is on) | `vid_restart` |
| `r4_upscale_sharpness` | `0` (off) - `1` | at once |
| `r4_dx12_present` | `0` / `1`: D3D12 presentation, needed for frame generation | game restart |
| `r4_framegen` | `0` / `1`: FSR frame generation | game restart |
| `r4_reflex` | `0` off, `1` on, `2` on + boost | at once |
| `r4_dlss_status`, `r4_framegen_status`, `r4_reflex_status` | print the current state and why a feature is off | - |
| `r4_upscale_debug` | `0`, `1` motion vectors, `2` depth (debug view) | at once |

Not supported: MSAA and HDR10 turn DLSS and render scale off. The D3D12 presentation forces borderless instead of exclusive fullscreen.

## Building
Visual Studio 2022 with MFC and ATL. Run `git submodule update --init`, open `src\engine-vs2022.sln` and build `DX11-AVX` or `DX11`; the output is in `_build\_game\bin_dbg`. Copy the DLLs from `sdk\binaries\DLSS` and `sdk\binaries\FidelityFX` next to the exe. The GitHub workflow `dlss-release.yml` builds both configurations, packs the gamedata and publishes the two release zips.

## Credits and licence
GSC Game World (X-Ray engine, S.T.A.L.K.E.R.), the Anomaly team, themrdemonized and contributors (Modded Exes / xray-monolith), OpenXRay and IX-Ray, Ascii1457 (Screen Space Shaders). Uses NVIDIA DLSS (NVIDIA, DLSS and RTX are trademarks of NVIDIA Corporation), NVIDIA NvAPI and AMD FidelityFX (AMD and FidelityFX are trademarks of Advanced Micro Devices, Inc.). Non-commercial use only. See `License.txt` for the terms of each part.
