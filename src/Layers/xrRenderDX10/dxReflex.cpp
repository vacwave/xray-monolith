// dxReflex.cpp: NVIDIA Reflex (r4_reflex) through NvAPI sleep mode + latency markers.
#include "stdafx.h"
#pragma hdrstop

#ifdef USE_DX11
#include <d3d11.h>
#include <nvapi.h>
#include "dxReflex.h"
#include "dx12PresentBridge.h"

extern int ps_r4_reflex;

namespace
{
bool s_failed = false;     // unsupported or a call failed: off for the rest of the session
IUnknown* s_dev = nullptr; // device the sleep mode was applied to
int s_mode = 0;            // applied r4_reflex
int s_cap = 0;             // applied frame cap (fps)
NvU64 s_frame = 0;
bool s_in_frame = false;   // FrameStart slept for the current frame

void Fail(const char* what, NvAPI_Status st)
{
    NvAPI_ShortString text = {};
    NvAPI_GetErrorMessage(st, text);
    Msg("! r4_reflex: %s failed (%d, %s), Reflex off", what, st, text);
    s_failed = true;
    s_in_frame = false;
    s_mode = 0;
    s_dev = nullptr;
}

void Marker(NV_LATENCY_MARKER_TYPE type)
{
    if (!s_in_frame)
        return;
    NV_LATENCY_MARKER_PARAMS p = {};
    p.version = NV_LATENCY_MARKER_PARAMS_VER;
    p.frameID = s_frame;
    p.markerType = type;
    NvAPI_D3D_SetLatencyMarker(s_dev, &p);
}
} // namespace

bool Reflex::FrameStart(int cap)
{
    s_in_frame = false;
    const int mode = s_failed ? 0 : ps_r4_reflex;
    if (!mode && !s_mode)
        return false;

    IUnknown* dev = DX12Present_Device();
    const bool d3d12 = dev != nullptr;
    if (!dev)
        dev = HW.pDevice;
    if (mode != s_mode || cap != s_cap || dev != s_dev)
    {
        static bool initialised = false;
        if (!initialised)
        {
            const NvAPI_Status st = NvAPI_Initialize();
            if (st != NVAPI_OK)
            {
                Fail("NvAPI_Initialize (no NVIDIA GPU or driver)", st);
                return false;
            }
            initialised = true;
        }
        NV_SET_SLEEP_MODE_PARAMS p = {};
        p.version = NV_SET_SLEEP_MODE_PARAMS_VER;
        p.bLowLatencyMode = mode != 0;
        p.bLowLatencyBoost = mode == 2;
        p.minimumIntervalUs = (mode && cap > 0) ? 1000000u / u32(cap) : 0;
        const NvAPI_Status st = NvAPI_D3D_SetSleepMode(dev, &p);
        if (st != NVAPI_OK)
        {
            Fail("NvAPI_D3D_SetSleepMode", st);
            return false;
        }
        string64 cap_text = "none";
        if (cap > 0)
            xr_sprintf(cap_text, "%d fps by Reflex", cap);
        if (mode)
            Msg("* r4_reflex: low latency on%s, %s device, frame cap %s", mode == 2 ? " + boost" : "",
                d3d12 ? "D3D12 (bridge)" : "D3D11", cap_text);
        else
            Msg("* r4_reflex: off");
        s_mode = mode;
        s_cap = cap;
        s_dev = dev;
        if (!mode)
            return false;
    }

    const NvAPI_Status st = NvAPI_D3D_Sleep(dev);
    if (st != NVAPI_OK)
    {
        Fail("NvAPI_D3D_Sleep", st);
        return false;
    }
    ++s_frame;
    s_in_frame = true;
    Marker(SIMULATION_START);
    return cap > 0;
}

void Reflex::SimulationEnd() { Marker(SIMULATION_END); }
void Reflex::RenderSubmitStart() { Marker(RENDERSUBMIT_START); }

void Reflex::PresentStart()
{
    Marker(RENDERSUBMIT_END);
    Marker(PRESENT_START);
}

void Reflex::PresentEnd()
{
    Marker(PRESENT_END);
    s_in_frame = false;
}
#endif
