# Build & packing
- Output: `$(SolutionDir)..\_build\_game\bin_dbg\` (Common.props:13,20) → `_build\_game\bin_dbg\AnomalyDX11AVX.exe`.
- `src\3rd party` is on the global IncludePath (Common.props:19): header-only libs need no vcxproj edits.
- No project links ws2_32.lib (only optick-git server via #pragma) — engine socket code must add `#pragma comment(lib, "ws2_32.lib")` or a linker entry.
- Pack: `compressor\! compress_gamedata.cmd` runs `xrCompress.exe gamedata -ltx build_patch.ltx -pack -strong -1024 -db`; output likely `gamedata.db0` (unverified) → rename `00_modded_exes_gamedata.db0` → `C:\Anomaly\db\mods\`.
- gamedata/: configs, levels, materials, meshes, scripts, shaders.
