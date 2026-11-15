# Engine loop, threading, startup
- Frame: `CRenderDevice::on_idle` device.cpp:331 → `FrameMove` :662 runs `seqFrame.Process` on main thread :717; then secondary_tasks, `seqRender` :526, `secondary_tasks.wait()` :543.
- Main-thread per-frame hook: `Device.seqFrame.Add(this)` (see xrGame/GamePersistent.cpp:95) or `Device.AddSeqFrame(f,false)` device.cpp:869 (`true` = MT list — don't use for game work).
- Gotcha: `GameThread` EngineThreading.cpp:124 is a background task running scheduler, seqParallel, seqFrameMT and parallel Lua GC — Lua is not main-thread-only.
- Task groups are MSVC PPL `concurrency::task_group` (xrCore/_thread_types.h:18), not TBB.
- Startup: WinMain x_ray.cpp:1271 → WinMain_impl :971 → Core._initialize :1107, InitEngine :173, InitConsole :239, Startup :589.
- In Startup: `Device.Create()` :648, `Init_Discord` :662 (model for starting a service thread), `Device.Run()` :675; shutdown after Run returns :677-715, `destroyEngine` :310.
- Flags: raw `strstr(Core.Params, "-start ")` x_ray.cpp:639; typed `ECoreParams` xrCore/xrParams.h:3 filled in xrParams.cpp:26, test via `Core.ParamsData.test(...)`.
