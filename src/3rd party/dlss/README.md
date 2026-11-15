NVIDIA DLSS SDK 310.9.16 (https://github.com/NVIDIA/DLSS, tag v310.9.16), subset used by the R4 renderer:
- `include/`: NGX headers
- `lib/nvsdk_ngx_d.lib`: x64 NGX static lib, dynamic CRT (/MD), from `lib/Windows_x86_64/x64/`
- `bin/nvngx_dlss.dll`: release DLSS runtime, from `lib/Windows_x86_64/rel/`; copied next to the game exe by xrRender_R4.vcxproj
- `LICENSE.txt`: NVIDIA RTX SDKs license
