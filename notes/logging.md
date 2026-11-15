# Console, logging, crashes
- Console exec: `CConsole::Execute(LPCSTR)` XR_IOConsole.cpp:851 → ExecuteCommand :681; ExecuteScript :876.
- Command registration: CMD1..CMD4 macros xrEngine/xr_ioc_cmd.h:4; game cmds in `CCC_RegisterCommands` xrGame/console_commands.cpp:2647.
- Log capture: `xrLogger::AddLogCallback/RemoveLogCallback` xrCore/log.h:23,36; signature `void(*)(const char*)`, called per line in `InternalPrintRecord` log.cpp:402 on the logger thread under `logCallbackGuard` — must not block.
- Log file: `<ApplicationName>_<UserName>.log` in `$logs$` (log.cpp:331-342); previous renamed to `.bkp`.
- Gotcha: `FlushLog` log.cpp:255 is async (only signals logger thread); real flush is `InternalFlushLog` :244.
- Build defines NO_BUG_TRAP + USE_OWN_MINI_DUMP (build_config_defines.h:25-26) → live filter is `UnhandledFilter` xrDebugNew.cpp:804 (installed :1195).
- Crash sequence: SetImmediateMode :806, format_message :808, crashhandler :814, LogStackTrace :820, FlushLog :857, save_mini_dump :859.
- Minidump: `save_mini_dump` xrDebugNew.cpp:664; name `<App>_<User>_<timestamp>.mdmp` in `$logs$` (fallback `logs/`, then cwd); flags MiniDumpNormal|FilterMemory|ScanMemory :737.
- Crash hook: `Debug.set_crashhandler(void(*)())` xrDebug.h:24 — single slot, already used by console (XR_IOConsole.cpp:238 DumpConsoleVariablesOnCrash); chain, don't replace.
