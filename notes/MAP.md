# MAP
- MCP architecture/protocol → `mcp/` (supervisor, Python), engine IPC server `src/xrEngine/mcp_server.*` → `mcp.md`
- Main loop, threading, startup/shutdown, cmdline flags → `xrEngine/device.cpp`, `EngineThreading.cpp`, `x_ray.cpp`, `xrCore/xrParams.h` → `engine.md`
- Console, logging, crash handler/minidumps → `xrEngine/XR_IOConsole.cpp`, `xrCore/log.cpp`, `xrCore/xrDebugNew.cpp` → `logging.md`
- Lua scripting from C++ → `xrServerEntities/script_engine.*`, `script_process.cpp`, `xrGame/console_commands.cpp` (CCC_ScriptCommand) → `scripting.md`
- Input / actions → `xrEngine/Xr_input.cpp`, `xrGame/Level_input.cpp`, `xr_level_controller.cpp`, `level_script.cpp` → `input.md`
- Screenshots / render → `Layers/xrRender/r__screenshot.cpp`, `xrEngine/Render.h` → `render.md`
- Game state (actor, level, time) → `xrGame/Actor*.cpp`, `Level.h`, `level_script.cpp` → `gamestate.md`
- Build, output, 3rd party, gamedata packing → `src/Common.props`, `engine-vs2022.sln`, `compressor/` → `build.md`
