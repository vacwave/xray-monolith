#pragma once

#ifdef USE_DX11
// r4_dx12_present: a D3D12 device owns the flip swapchain of the window; the returned object stands in for the
// DX11 swapchain (GetBuffer = a D3D11 texture shared with D3D12, Present = fence sync + copy + D3D12 Present).
// Returns nullptr (and logs why) when the bridge can't be created; the caller then makes the DX11 swapchain.
// framegen (r4_framegen): FSR frame generation swapchain instead of the plain one, falls back to it on failure.
IDXGISwapChain1* DX12Present_Create(ID3D11Device1* device, ID3D11DeviceContext1* context, IDXGIFactory2* factory,
                                    HWND hwnd, const DXGI_SWAP_CHAIN_DESC1& desc,
                                    const DXGI_SWAP_CHAIN_FULLSCREEN_DESC& desc_fullscreen, bool framegen);

// r4_framegen: copies the back buffer as the HUD-less colour; call once per frame when the 3D scene (incl. HUD
// weapon and post-process) is final, before the 2D UI is drawn. No-op without the FG swapchain.
void DX12Present_CaptureHudless();

// the D3D12 device that presents (r4_reflex targets it), nullptr without the bridge
IUnknown* DX12Present_Device();

// r4_framegen: true while the FG swapchain wants depth + motion vectors (turns the R4 temporal inputs on)
bool DX12Present_FrameGenWantsInputs();
// r4_framegen: hands this frame's rt_dlss_depth / rt_dlss_mv, jitter and camera to frame generation; call after
// phase_combine. Frames without the call (menu, loading screen, SVP) present without interpolation.
void DX12Present_SetFrameGenInputs();
#endif
