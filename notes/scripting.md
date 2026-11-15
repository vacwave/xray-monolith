# Lua scripting
- `run_string` = `CCC_ScriptCommand` console_commands.cpp:1949: with level → `script_process(eScriptProcessorLevel)->add_script(...)` (runs on a later frame); no level → `luaL_loadbuffer`+`lua_pcall` on `ai().script_engine().lua()` :1969-1976 (synchronous).
- `run_script` console_commands.cpp:1911; `CScriptProcess::add_script` xrServerEntities/script_process.cpp:101.
- Errors: `print_output` / `on_error` script_engine.cpp:654.
- Engine→Lua calls: `script_engine().functor("module.func", f)` script_engine.h:155 (e.g. Level_input.cpp:188).
- LuaSocket exists (3rd party/lua-extensions/luasocket) but only opened with `lua_debug` (script_storage.cpp:449-464).
- Gotcha: script engine is in xrGame, not xrEngine — engine-side server needs an xrGame-registered handler for Lua.
