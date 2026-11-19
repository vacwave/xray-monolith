#pragma once

#ifdef USE_DX11
// r4_reflex: NVIDIA Reflex low-latency mode through NvAPI on the presenting device (the D3D12 device of the
// DX12 presentation bridge, else the D3D11 device). Off (0) makes no NvAPI calls at all.
namespace Reflex
{
// Once per frame on the main thread, before input sampling and simulation: applies r4_reflex / frame cap changes,
// sleeps, sends SIMULATION_START. Returns true when Reflex enforces frame_cap_fps (the engine limiter must not wait).
bool FrameStart(int frame_cap_fps);
void SimulationEnd();
void RenderSubmitStart();
void PresentStart(); // RENDERSUBMIT_END + PRESENT_START
void PresentEnd();
bool Failed(); // unsupported or an NvAPI call failed: off for the session
}
#endif
