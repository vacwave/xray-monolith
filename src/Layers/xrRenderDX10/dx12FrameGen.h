#pragma once

#ifdef USE_DX11
#include <d3d12.h>
#include <dxgi1_5.h>

// r4_framegen: AMD FSR frame generation swapchain + context (FSR SDK 2.4.0, loaded at run time from
// amd_fidelityfx_loader_dx12.dll next to the exe). Used by the DX12 presentation bridge only.

// Per-frame inputs for frame interpolation. valid == false -> the swapchain presents rendered frames only.
struct FrameGenInputs
{
    bool valid = false;
    bool reset = false;                       // camera cut, level load, resolution change, first frame after an SVP frame
    ID3D12Resource* depth = nullptr;          // render-size depth, D3D12 side of a shared copy
    ID3D12Resource* motion_vectors = nullptr; // render-size RG16F, pixel space
    UINT render_width = 0;
    UINT render_height = 0;
    float jitter_x = 0.f;                     // pixels
    float jitter_y = 0.f;
    float camera_near = 0.f;
    float camera_far = 0.f;
    float camera_fov_y = 0.f;                 // radians
    float frame_time_ms = 0.f;
};

class CFrameGen
{
public:
    ~CFrameGen();

    // FG swapchain for hwnd presenting on queue; nullptr (logged) on failure
    IDXGISwapChain4* CreateSwapChain(IDXGIFactory2* factory, ID3D12CommandQueue* queue, HWND hwnd,
                                     const DXGI_SWAP_CHAIN_DESC1& desc, const DXGI_SWAP_CHAIN_FULLSCREEN_DESC& desc_fullscreen);
    // FG context for the swapchain's current size and format
    bool CreateContext(ID3D12Device* device);
    void DestroyContext();
    // after DestroyContext; the caller then releases its swapchain references
    void DestroySwapChain();
    void WaitForPresents();

    // before the swapchain Present; list is still open (prepare goes into it)
    void OnPresent(ID3D12GraphicsCommandList* list, ID3D12Resource* hudless, const FrameGenInputs& in);

private:
    void Configure(bool enabled, ID3D12Resource* hudless);

    void* m_sc_ctx = nullptr; // ffxContext of the swapchain
    void* m_fg_ctx = nullptr; // ffxContext of frame generation
    IDXGISwapChain4* m_sc = nullptr; // not owned
    bool m_enabled = false;
    UINT64 m_frame_id = 0;
};
#endif
