// dx12PresentBridge.cpp: DX11 renders, a D3D12 device presents (r4_dx12_present).
#include "stdafx.h"
#pragma hdrstop

#ifdef USE_DX11
#include <d3d11_4.h>
#include <dxgi1_4.h>
#include <d3d12.h>
#include "dx12PresentBridge.h"
#include "dx12FrameGen.h"
#include "../../xrEngine/igame_persistent.h"

#pragma comment(lib, "d3d12.lib")

namespace
{
const UINT FRAME_SLOTS = 3; // presents in flight on the D3D12 queue
const DWORD FENCE_TIMEOUT_MS = 10000;

class CDX12PresentSwapChain final : public IDXGISwapChain1
{
public:
    CDX12PresentSwapChain() { ZeroMemory(m_backbuf, sizeof(m_backbuf)); ZeroMemory(m_alloc, sizeof(m_alloc)); ZeroMemory(m_slot_value, sizeof(m_slot_value)); }
    ~CDX12PresentSwapChain();

    bool Init(ID3D11Device1* device, ID3D11DeviceContext1* context, IDXGIFactory2* factory, HWND hwnd,
              const DXGI_SWAP_CHAIN_DESC1& desc, const DXGI_SWAP_CHAIN_FULLSCREEN_DESC& desc_fullscreen, bool framegen);
    void CaptureHudless();
    bool FrameGenOn() const { return m_fg_on && m_fg.HasContext(); }
    ID3D12Device* Device12() const { return m_dev12; }
    void SetInputs(ID3D11Texture2D* depth, ID3D11Texture2D* mv, const FrameGenInputs& in);

    // IUnknown
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void** ppv) override;
    ULONG STDMETHODCALLTYPE AddRef() override { return InterlockedIncrement(&m_refs); }
    ULONG STDMETHODCALLTYPE Release() override
    {
        const ULONG refs = InterlockedDecrement(&m_refs);
        if (!refs)
            delete this;
        return refs;
    }

    // IDXGIObject
    HRESULT STDMETHODCALLTYPE SetPrivateData(REFGUID name, UINT size, const void* data) override { return m_sc->SetPrivateData(name, size, data); }
    HRESULT STDMETHODCALLTYPE SetPrivateDataInterface(REFGUID name, const IUnknown* unknown) override { return m_sc->SetPrivateDataInterface(name, unknown); }
    HRESULT STDMETHODCALLTYPE GetPrivateData(REFGUID name, UINT* size, void* data) override { return m_sc->GetPrivateData(name, size, data); }
    HRESULT STDMETHODCALLTYPE GetParent(REFIID riid, void** parent) override { return m_sc->GetParent(riid, parent); }

    // IDXGIDeviceSubObject: the device that renders into the buffers is the D3D11 one
    HRESULT STDMETHODCALLTYPE GetDevice(REFIID riid, void** device) override { return m_dev11->QueryInterface(riid, device); }

    // IDXGISwapChain
    HRESULT STDMETHODCALLTYPE Present(UINT sync_interval, UINT flags) override;
    HRESULT STDMETHODCALLTYPE GetBuffer(UINT buffer, REFIID riid, void** surface) override;
    HRESULT STDMETHODCALLTYPE SetFullscreenState(BOOL fullscreen, IDXGIOutput* target) override;
    HRESULT STDMETHODCALLTYPE GetFullscreenState(BOOL* fullscreen, IDXGIOutput** target) override { return m_sc->GetFullscreenState(fullscreen, target); }
    HRESULT STDMETHODCALLTYPE GetDesc(DXGI_SWAP_CHAIN_DESC* desc) override { return m_sc->GetDesc(desc); }
    HRESULT STDMETHODCALLTYPE ResizeBuffers(UINT count, UINT width, UINT height, DXGI_FORMAT format, UINT flags) override;
    HRESULT STDMETHODCALLTYPE ResizeTarget(const DXGI_MODE_DESC* params) override { return m_sc->ResizeTarget(params); }
    HRESULT STDMETHODCALLTYPE GetContainingOutput(IDXGIOutput** output) override;
    HRESULT STDMETHODCALLTYPE GetFrameStatistics(DXGI_FRAME_STATISTICS* stats) override { return m_sc->GetFrameStatistics(stats); }
    HRESULT STDMETHODCALLTYPE GetLastPresentCount(UINT* count) override { return m_sc->GetLastPresentCount(count); }

    // IDXGISwapChain1
    HRESULT STDMETHODCALLTYPE GetDesc1(DXGI_SWAP_CHAIN_DESC1* desc) override { return m_sc->GetDesc1(desc); }
    HRESULT STDMETHODCALLTYPE GetFullscreenDesc(DXGI_SWAP_CHAIN_FULLSCREEN_DESC* desc) override { return m_sc->GetFullscreenDesc(desc); }
    HRESULT STDMETHODCALLTYPE GetHwnd(HWND* hwnd) override { return m_sc->GetHwnd(hwnd); }
    HRESULT STDMETHODCALLTYPE GetCoreWindow(REFIID riid, void** unk) override { return m_sc->GetCoreWindow(riid, unk); }
    HRESULT STDMETHODCALLTYPE Present1(UINT sync_interval, UINT flags, const DXGI_PRESENT_PARAMETERS*) override { return Present(sync_interval, flags); }
    BOOL STDMETHODCALLTYPE IsTemporaryMonoSupported() override { return m_sc->IsTemporaryMonoSupported(); }
    HRESULT STDMETHODCALLTYPE GetRestrictToOutput(IDXGIOutput** output) override { return m_sc->GetRestrictToOutput(output); }
    HRESULT STDMETHODCALLTYPE SetBackgroundColor(const DXGI_RGBA* color) override { return m_sc->SetBackgroundColor(color); }
    HRESULT STDMETHODCALLTYPE GetBackgroundColor(DXGI_RGBA* color) override { return m_sc->GetBackgroundColor(color); }
    HRESULT STDMETHODCALLTYPE SetRotation(DXGI_MODE_ROTATION rotation) override { return m_sc->SetRotation(rotation); }
    HRESULT STDMETHODCALLTYPE GetRotation(DXGI_MODE_ROTATION* rotation) override { return m_sc->GetRotation(rotation); }

private:
    bool CreateShared(const D3D12_RESOURCE_DESC& tex, ID3D12Resource** res12, ID3D11Texture2D** res11);
    bool CreateBuffers();
    void ReleaseBuffers();
    void WaitIdle();
    void WaitFence(UINT64 value);
    HRESULT DeviceState();
    HRESULT WindowOutput(IDXGIOutput** output);
    void ReleaseToD3D11(UINT slot);
    void ReleaseInputs();
    void CountFrame(bool interpolated);

    LONG m_refs = 1;

    ID3D11Device5* m_dev11 = nullptr;
    ID3D11DeviceContext4* m_ctx11 = nullptr;
    ID3D11Fence* m_fence11 = nullptr;    // the D3D12 fence opened on D3D11
    ID3D11Texture2D* m_shared11 = nullptr; // what DX11 renders into (the engine's "back buffer")

    ID3D12Device* m_dev12 = nullptr;
    ID3D12CommandQueue* m_queue = nullptr;
    ID3D12CommandAllocator* m_alloc[FRAME_SLOTS];
    ID3D12GraphicsCommandList* m_list = nullptr;
    ID3D12Fence* m_fence12 = nullptr;
    HANDLE m_event = nullptr;
    UINT64 m_value = 0; // last value signalled on the shared fence by either device
    UINT64 m_slot_value[FRAME_SLOTS];
    UINT m_frame = 0;

    IDXGISwapChain3* m_sc = nullptr;
    ID3D12Resource* m_shared12 = nullptr;
    ID3D12Resource* m_backbuf[DXGI_MAX_SWAP_CHAIN_BUFFERS];
    bool m_lost_logged = false;
    HWND m_hwnd = nullptr;

    // r4_framegen: m_sc is the FSR frame generation swapchain
    CFrameGen m_fg;
    bool m_fg_on = false;
    ID3D12Resource* m_hudless12 = nullptr;  // scene without 2D UI, copied from the back buffer by CaptureHudless
    ID3D11Texture2D* m_hudless11 = nullptr;
    bool m_hudless_captured = false;
    FrameGenInputs m_fg_inputs; // valid only for the frame SetInputs was called in
    ID3D12Resource* m_depth12 = nullptr; // render-size copies of rt_dlss_depth / rt_dlss_mv
    ID3D11Texture2D* m_depth11 = nullptr;
    ID3D12Resource* m_mv12 = nullptr;
    ID3D11Texture2D* m_mv11 = nullptr;
    UINT m_inputs_w = 0, m_inputs_h = 0;
    // stats line every few seconds: rendered presents, of them interpolated, generated frames
    DWORD m_stats_start = 0;
    UINT m_stats_rendered = 0, m_stats_interpolated = 0;
    bool m_interpolating = false;
};

CDX12PresentSwapChain* s_active = nullptr; // the bridge in use (one window, one swapchain)
bool s_framegen_tried = false; // DX12Present_Create ran with framegen this session

#define BRIDGE_CHK(expr, what)                                                       \
    do {                                                                             \
        const HRESULT hr_ = (expr);                                                  \
        if (FAILED(hr_)) {                                                           \
            Msg("! r4_dx12_present: %s failed (0x%08x)", what, (u32)hr_);            \
            return false;                                                            \
        }                                                                            \
    } while (0)

bool CDX12PresentSwapChain::Init(ID3D11Device1* device, ID3D11DeviceContext1* context, IDXGIFactory2* factory,
                                 HWND hwnd, const DXGI_SWAP_CHAIN_DESC1& desc,
                                 const DXGI_SWAP_CHAIN_FULLSCREEN_DESC& desc_fullscreen, bool framegen)
{
    // shared fences need ID3D11Device5 / ID3D11DeviceContext4 (Windows 10 1703+)
    BRIDGE_CHK(device->QueryInterface(IID_PPV_ARGS(&m_dev11)), "ID3D11Device5");
    BRIDGE_CHK(context->QueryInterface(IID_PPV_ARGS(&m_ctx11)), "ID3D11DeviceContext4");

    // the D3D12 device must live on the adapter of the D3D11 device to share resources
    LUID luid;
    {
        IDXGIDevice* dxgi_device = nullptr;
        IDXGIAdapter* adapter = nullptr;
        BRIDGE_CHK(device->QueryInterface(IID_PPV_ARGS(&dxgi_device)), "IDXGIDevice");
        const HRESULT hr = dxgi_device->GetAdapter(&adapter);
        dxgi_device->Release();
        BRIDGE_CHK(hr, "GetAdapter");
        DXGI_ADAPTER_DESC adesc;
        const HRESULT hr2 = adapter->GetDesc(&adesc);
        adapter->Release();
        BRIDGE_CHK(hr2, "adapter GetDesc");
        luid = adesc.AdapterLuid;
    }

    IDXGIFactory4* factory4 = nullptr;
    BRIDGE_CHK(factory->QueryInterface(IID_PPV_ARGS(&factory4)), "IDXGIFactory4");
    IDXGIAdapter1* adapter12 = nullptr;
    HRESULT hr = factory4->EnumAdapterByLuid(luid, IID_PPV_ARGS(&adapter12));
    factory4->Release();
    BRIDGE_CHK(hr, "EnumAdapterByLuid");

    if (Core.ParamsData.test(ECoreParams::dxgi_dbg))
    {
        ID3D12Debug* debug = nullptr;
        if (SUCCEEDED(D3D12GetDebugInterface(IID_PPV_ARGS(&debug))))
        {
            debug->EnableDebugLayer();
            debug->Release();
        }
    }

    hr = D3D12CreateDevice(adapter12, D3D_FEATURE_LEVEL_11_0, IID_PPV_ARGS(&m_dev12));
    adapter12->Release();
    BRIDGE_CHK(hr, "D3D12CreateDevice");

    D3D12_COMMAND_QUEUE_DESC qdesc = {};
    qdesc.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    BRIDGE_CHK(m_dev12->CreateCommandQueue(&qdesc, IID_PPV_ARGS(&m_queue)), "CreateCommandQueue");
    for (UINT i = 0; i < FRAME_SLOTS; ++i)
        BRIDGE_CHK(m_dev12->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&m_alloc[i])), "CreateCommandAllocator");
    BRIDGE_CHK(m_dev12->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, m_alloc[0], nullptr, IID_PPV_ARGS(&m_list)), "CreateCommandList");
    BRIDGE_CHK(m_list->Close(), "CommandList Close");

    BRIDGE_CHK(m_dev12->CreateFence(0, D3D12_FENCE_FLAG_SHARED, IID_PPV_ARGS(&m_fence12)), "CreateFence");
    {
        HANDLE handle = nullptr;
        BRIDGE_CHK(m_dev12->CreateSharedHandle(m_fence12, nullptr, GENERIC_ALL, nullptr, &handle), "fence CreateSharedHandle");
        hr = m_dev11->OpenSharedFence(handle, IID_PPV_ARGS(&m_fence11));
        CloseHandle(handle);
        BRIDGE_CHK(hr, "OpenSharedFence");
    }
    m_hwnd = hwnd;
    m_event = CreateEvent(nullptr, FALSE, FALSE, nullptr);
    if (!m_event)
    {
        Msg("! r4_dx12_present: CreateEvent failed");
        return false;
    }

    if (framegen)
    {
        m_sc = m_fg.CreateSwapChain(factory, m_queue, hwnd, desc, desc_fullscreen);
        m_fg_on = m_sc != nullptr;
        if (m_fg_on && !(CreateBuffers() && m_fg.CreateContext(m_dev12)))
        {
            ReleaseBuffers();
            m_fg.DestroyContext();
            m_fg.DestroySwapChain();
            _RELEASE(m_sc);
            m_fg_on = false;
        }
        if (!m_fg_on)
            Msg("! r4_framegen: frame generation off, using the plain DX12 swapchain");
    }

    if (!m_sc)
    {
        IDXGISwapChain1* sc1 = nullptr;
        BRIDGE_CHK(factory->CreateSwapChainForHwnd(m_queue, hwnd, &desc, &desc_fullscreen, nullptr, &sc1), "D3D12 CreateSwapChainForHwnd");
        hr = sc1->QueryInterface(IID_PPV_ARGS(&m_sc));
        sc1->Release();
        BRIDGE_CHK(hr, "IDXGISwapChain3");

        if (!CreateBuffers())
            return false;
    }

    Msg("* r4_dx12_present: DX12 presentation bridge on (%ux%u, %u buffers%s)", desc.Width, desc.Height, desc.BufferCount,
        m_fg_on ? ", FSR frame generation swapchain" : "");
    s_active = this;
    return true;
}

CDX12PresentSwapChain::~CDX12PresentSwapChain()
{
    if (s_active == this)
        s_active = nullptr;
    if (m_fg_on)
        m_fg.WaitForPresents();
    if (m_sc && m_queue && m_fence12 && m_event)
        WaitIdle();
    m_fg.DestroyContext();
    ReleaseBuffers();
    ReleaseInputs();
    m_fg.DestroySwapChain();
    _RELEASE(m_sc);
    _RELEASE(m_list);
    for (UINT i = 0; i < FRAME_SLOTS; ++i)
        _RELEASE(m_alloc[i]);
    _RELEASE(m_queue);
    _RELEASE(m_fence11);
    _RELEASE(m_fence12);
    _RELEASE(m_dev12);
    _RELEASE(m_ctx11);
    _RELEASE(m_dev11);
    if (m_event)
        CloseHandle(m_event);
}

// D3D12-owned texture opened on D3D11
bool CDX12PresentSwapChain::CreateShared(const D3D12_RESOURCE_DESC& tex, ID3D12Resource** res12, ID3D11Texture2D** res11)
{
    D3D12_HEAP_PROPERTIES heap = {};
    heap.Type = D3D12_HEAP_TYPE_DEFAULT;
    BRIDGE_CHK(m_dev12->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_SHARED, &tex, D3D12_RESOURCE_STATE_COMMON,
        nullptr, IID_PPV_ARGS(res12)), "shared texture CreateCommittedResource");
    HANDLE handle = nullptr;
    BRIDGE_CHK(m_dev12->CreateSharedHandle(*res12, nullptr, GENERIC_ALL, nullptr, &handle), "texture CreateSharedHandle");
    const HRESULT hr = m_dev11->OpenSharedResource1(handle, IID_PPV_ARGS(res11));
    CloseHandle(handle);
    BRIDGE_CHK(hr, "OpenSharedResource1");
    return true;
}

// shared colour texture(s) + references to the swapchain buffers
bool CDX12PresentSwapChain::CreateBuffers()
{
    DXGI_SWAP_CHAIN_DESC1 desc;
    BRIDGE_CHK(m_sc->GetDesc1(&desc), "GetDesc1");

    D3D12_RESOURCE_DESC tex = {};
    tex.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    tex.Width = desc.Width;
    tex.Height = desc.Height;
    tex.DepthOrArraySize = 1;
    tex.MipLevels = 1;
    tex.Format = desc.Format;
    tex.SampleDesc.Count = 1;
    tex.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
    tex.Flags = D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET;

    if (!CreateShared(tex, &m_shared12, &m_shared11))
        return false;
    if (m_fg_on && !CreateShared(tex, &m_hudless12, &m_hudless11))
        return false;

    for (UINT i = 0; i < desc.BufferCount && i < DXGI_MAX_SWAP_CHAIN_BUFFERS; ++i)
        BRIDGE_CHK(m_sc->GetBuffer(i, IID_PPV_ARGS(&m_backbuf[i])), "swapchain GetBuffer");
    return true;
}

void CDX12PresentSwapChain::ReleaseBuffers()
{
    for (UINT i = 0; i < DXGI_MAX_SWAP_CHAIN_BUFFERS; ++i)
        _RELEASE(m_backbuf[i]);
    _RELEASE(m_shared11);
    _RELEASE(m_shared12);
    _RELEASE(m_hudless11);
    _RELEASE(m_hudless12);
    m_hudless_captured = false;
}

void CDX12PresentSwapChain::WaitFence(UINT64 value)
{
    if (m_fence12->GetCompletedValue() >= value)
        return;
    if (FAILED(m_fence12->SetEventOnCompletion(value, m_event)))
        return;
    if (WaitForSingleObject(m_event, FENCE_TIMEOUT_MS) != WAIT_OBJECT_0)
        Msg("! r4_dx12_present: fence wait timed out (value %llu)", value);
}

// both devices done with the shared texture and the swapchain buffers
void CDX12PresentSwapChain::WaitIdle()
{
    if (m_ctx11 && m_fence11)
    {
        m_ctx11->Signal(m_fence11, ++m_value);
        m_ctx11->Flush();
        m_queue->Wait(m_fence12, m_value);
    }
    m_queue->Signal(m_fence12, ++m_value);
    WaitFence(m_value);
}

HRESULT CDX12PresentSwapChain::DeviceState()
{
    HRESULT hr = m_dev11->GetDeviceRemovedReason();
    if (SUCCEEDED(hr))
        hr = m_dev12->GetDeviceRemovedReason();
    if (SUCCEEDED(hr))
        return S_OK;
    if (!m_lost_logged)
    {
        m_lost_logged = true;
        Msg("! r4_dx12_present: device removed (D3D11 0x%08x, D3D12 0x%08x)",
            (u32)m_dev11->GetDeviceRemovedReason(), (u32)m_dev12->GetDeviceRemovedReason());
    }
    // the engine maps only these two codes (dxRenderDeviceRender::GetDeviceState)
    return hr == DXGI_ERROR_DEVICE_RESET ? DXGI_ERROR_DEVICE_RESET : DXGI_ERROR_DEVICE_REMOVED;
}

HRESULT STDMETHODCALLTYPE CDX12PresentSwapChain::QueryInterface(REFIID riid, void** ppv)
{
    if (!ppv)
        return E_POINTER;
    if (riid == __uuidof(IUnknown) || riid == __uuidof(IDXGIObject) || riid == __uuidof(IDXGIDeviceSubObject) ||
        riid == __uuidof(IDXGISwapChain) || riid == __uuidof(IDXGISwapChain1))
    {
        *ppv = static_cast<IDXGISwapChain1*>(this);
        AddRef();
        return S_OK;
    }
    // newer interfaces (colour space, HDR metadata) go straight to the D3D12 swapchain
    return m_sc->QueryInterface(riid, ppv);
}

HRESULT STDMETHODCALLTYPE CDX12PresentSwapChain::GetContainingOutput(IDXGIOutput** output)
{
    if (!output)
        return E_POINTER;
    *output = nullptr;
    const HRESULT hr = m_sc->GetContainingOutput(output);
    if (SUCCEEDED(hr) && *output)
        return hr;
    // the FSR frame generation swapchain reports no output (callers such as xr_effgamma dereference it)
    return WindowOutput(output);
}

// output of the D3D11 adapter showing the window, else its first output
HRESULT CDX12PresentSwapChain::WindowOutput(IDXGIOutput** output)
{
    IDXGIDevice* dxgi_device = nullptr;
    IDXGIAdapter* adapter = nullptr;
    HRESULT hr = m_dev11->QueryInterface(IID_PPV_ARGS(&dxgi_device));
    if (FAILED(hr))
        return hr;
    hr = dxgi_device->GetAdapter(&adapter);
    dxgi_device->Release();
    if (FAILED(hr))
        return hr;
    const HMONITOR monitor = MonitorFromWindow(m_hwnd, MONITOR_DEFAULTTONEAREST);
    IDXGIOutput* out = nullptr;
    for (UINT i = 0; adapter->EnumOutputs(i, &out) != DXGI_ERROR_NOT_FOUND; ++i)
    {
        DXGI_OUTPUT_DESC desc;
        if (SUCCEEDED(out->GetDesc(&desc)) && desc.Monitor == monitor)
            break;
        _RELEASE(out);
    }
    if (!out)
        hr = adapter->EnumOutputs(0, &out);
    adapter->Release();
    *output = out;
    return out ? S_OK : (FAILED(hr) ? hr : DXGI_ERROR_NOT_FOUND);
}

HRESULT STDMETHODCALLTYPE CDX12PresentSwapChain::GetBuffer(UINT, REFIID riid, void** surface)
{
    if (!surface)
        return E_POINTER;
    if (!m_shared11)
        return DXGI_ERROR_INVALID_CALL;
    return m_shared11->QueryInterface(riid, surface);
}

HRESULT STDMETHODCALLTYPE CDX12PresentSwapChain::Present(UINT sync_interval, UINT flags)
{
    if (flags & DXGI_PRESENT_TEST)
        return DeviceState();
    if (!m_shared12)
        return DXGI_ERROR_INVALID_CALL;

    const UINT slot = m_frame++ % FRAME_SLOTS;
    WaitFence(m_slot_value[slot]);

    // D3D11 finished the frame -> D3D12 copies it into the current swapchain buffer
    m_ctx11->Signal(m_fence11, ++m_value);
    m_ctx11->Flush();
    m_queue->Wait(m_fence12, m_value);

    ID3D12Resource* backbuf = m_backbuf[m_sc->GetCurrentBackBufferIndex()];
    m_alloc[slot]->Reset();
    m_list->Reset(m_alloc[slot], nullptr);

    D3D12_RESOURCE_BARRIER barrier = {};
    barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    barrier.Transition.pResource = backbuf;
    barrier.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    barrier.Transition.StateBefore = D3D12_RESOURCE_STATE_PRESENT;
    barrier.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_DEST;
    m_list->ResourceBarrier(1, &barrier);
    // the shared texture is promoted COMMON -> COPY_SOURCE and decays back after the list
    m_list->CopyResource(backbuf, m_shared12);
    barrier.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_DEST;
    barrier.Transition.StateAfter = D3D12_RESOURCE_STATE_PRESENT;
    m_list->ResourceBarrier(1, &barrier);
    if (m_fg_on)
    {
        const bool interpolated = m_fg.OnPresent(m_list, m_hudless_captured ? m_hudless12 : nullptr, m_fg_inputs);
        CountFrame(interpolated);
    }
    m_hudless_captured = false;
    m_fg_inputs.valid = false;
    m_list->Close();

    ID3D12CommandList* lists[] = {m_list};
    m_queue->ExecuteCommandLists(1, lists);

    // FG: interpolation reads the shared textures inside Present on m_queue, so release them after it
    if (!m_fg_on)
        ReleaseToD3D11(slot);
    const HRESULT hr = m_sc->Present(sync_interval, flags);
    if (m_fg_on)
        ReleaseToD3D11(slot);
    if (hr == DXGI_ERROR_DEVICE_REMOVED || hr == DXGI_ERROR_DEVICE_RESET)
        DeviceState();
    return hr;
}

// D3D11 may render the next frame into the shared textures once the D3D12 work queued so far is done
void CDX12PresentSwapChain::ReleaseToD3D11(UINT slot)
{
    m_queue->Signal(m_fence12, ++m_value);
    m_slot_value[slot] = m_value;
    m_ctx11->Wait(m_fence11, m_value);
}

void CDX12PresentSwapChain::CaptureHudless()
{
    if (!m_hudless11 || !m_shared11)
        return;
    m_ctx11->CopyResource(m_hudless11, m_shared11);
    m_hudless_captured = true;
}

void CDX12PresentSwapChain::ReleaseInputs()
{
    _RELEASE(m_depth11);
    _RELEASE(m_depth12);
    _RELEASE(m_mv11);
    _RELEASE(m_mv12);
    m_inputs_w = m_inputs_h = 0;
}

// copies the D3D11 depth + motion vectors into textures shared with D3D12 (same fence as the back buffer)
void CDX12PresentSwapChain::SetInputs(ID3D11Texture2D* depth, ID3D11Texture2D* mv, const FrameGenInputs& in)
{
    if (in.render_width != m_inputs_w || in.render_height != m_inputs_h)
    {
        // render size changed (vid_restart, r4_render_scale): D3D12 must be done with the old copies
        m_fg.WaitForPresents();
        WaitIdle();
        ReleaseInputs();
        D3D12_RESOURCE_DESC tex = {};
        tex.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
        tex.Width = in.render_width;
        tex.Height = in.render_height;
        tex.DepthOrArraySize = 1;
        tex.MipLevels = 1;
        tex.SampleDesc.Count = 1;
        tex.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
        // R32F / RG16F open on D3D11 (OpenSharedResource1) only with simultaneous access, else E_INVALIDARG
        tex.Flags = D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET | D3D12_RESOURCE_FLAG_ALLOW_SIMULTANEOUS_ACCESS;
        tex.Format = DXGI_FORMAT_R32_FLOAT;
        bool ok = CreateShared(tex, &m_depth12, &m_depth11);
        tex.Format = DXGI_FORMAT_R16G16_FLOAT;
        ok = ok && CreateShared(tex, &m_mv12, &m_mv11);
        if (!ok)
        {
            Msg("! r4_framegen: no shared depth / motion vector textures (%ux%u), presenting rendered frames only", in.render_width, in.render_height);
            ReleaseInputs();
        }
        else
            Msg("* r4_framegen: frame generation inputs %ux%u (depth R32F + vectors RG16F, %.1f MB)", in.render_width, in.render_height,
                in.render_width * in.render_height * 8 / 1048576.0);
        m_inputs_w = in.render_width;
        m_inputs_h = in.render_height;
    }
    if (!m_depth11)
        return;
    m_ctx11->CopyResource(m_depth11, depth);
    m_ctx11->CopyResource(m_mv11, mv);
    m_fg_inputs = in;
    m_fg_inputs.depth = m_depth12;
    m_fg_inputs.motion_vectors = m_mv12;
    m_fg_inputs.valid = true;
}

void CDX12PresentSwapChain::CountFrame(bool interpolated)
{
    if (interpolated != m_interpolating)
    {
        Msg("* r4_framegen: interpolation %s", interpolated ? "on" : "held (no inputs this frame: menu, loading, scope frame gap or no vectors)");
        m_interpolating = interpolated;
    }
    const DWORD now = GetTickCount();
    if (!m_stats_start)
        m_stats_start = now;
    ++m_stats_rendered;
    m_stats_interpolated += interpolated;
    const DWORD elapsed = now - m_stats_start;
    if (elapsed < 10000)
        return;
    const UINT generated = m_fg.TakeGeneratedCount();
    if ((m_stats_interpolated || generated) && ps_r4_upscale_debug)
    {
        const float s = elapsed / 1000.f;
        Msg("* r4_framegen: %.1f s: %u rendered (%.1f fps), %u with interpolation, %u generated -> %.1f fps presented", s,
            m_stats_rendered, m_stats_rendered / s, m_stats_interpolated, generated, (m_stats_rendered + generated) / s);
    }
    m_stats_start = now;
    m_stats_rendered = m_stats_interpolated = 0;
}

HRESULT STDMETHODCALLTYPE CDX12PresentSwapChain::ResizeBuffers(UINT count, UINT width, UINT height, DXGI_FORMAT format, UINT flags)
{
    if (m_fg_on)
        m_fg.WaitForPresents();
    WaitIdle();
    // the FG context is sized for the display: rebuilt around the resize
    m_fg.DestroyContext();
    ReleaseBuffers();
    const HRESULT hr = m_sc->ResizeBuffers(count, width, height, format, flags);
    if (FAILED(hr))
        Msg("! r4_dx12_present: ResizeBuffers %ux%u failed (0x%08x)", width, height, (u32)hr);
    if (!CreateBuffers())
        Msg("! r4_dx12_present: buffers lost after resize");
    else if (m_fg_on && !m_fg.CreateContext(m_dev12))
        Msg("! r4_framegen: no frame generation context after resize, presenting rendered frames only");
    return hr;
}

HRESULT STDMETHODCALLTYPE CDX12PresentSwapChain::SetFullscreenState(BOOL fullscreen, IDXGIOutput* target)
{
    // exclusive fullscreen is not supported by the bridge (nor by FSR frame generation); CHW forces borderless
    if (fullscreen)
    {
        Msg("! r4_dx12_present: exclusive fullscreen refused, staying windowed");
        fullscreen = FALSE;
        target = nullptr;
    }
    if (m_fg_on)
        m_fg.WaitForPresents();
    WaitIdle();
    return m_sc->SetFullscreenState(fullscreen, target);
}
} // namespace

IDXGISwapChain1* DX12Present_Create(ID3D11Device1* device, ID3D11DeviceContext1* context, IDXGIFactory2* factory,
                                    HWND hwnd, const DXGI_SWAP_CHAIN_DESC1& desc,
                                    const DXGI_SWAP_CHAIN_FULLSCREEN_DESC& desc_fullscreen, bool framegen)
{
    s_framegen_tried |= framegen;
    CDX12PresentSwapChain* sc = new CDX12PresentSwapChain(); // COM refcount; Release() deletes
    if (sc->Init(device, context, factory, hwnd, desc, desc_fullscreen, framegen))
        return sc;
    Msg("! r4_dx12_present: bridge unavailable, using the DX11 swapchain");
    sc->Release();
    return nullptr;
}

void DX12Present_CaptureHudless()
{
    // SVP (scope) frames are not presented
    if (s_active && !Device.m_SecondViewport.IsSVPFrame())
        s_active->CaptureHudless();
}

IUnknown* DX12Present_Device()
{
    return s_active ? s_active->Device12() : nullptr;
}

bool DX12Present_FrameGenTried()
{
    return s_framegen_tried;
}

bool DX12Present_FrameGenWantsInputs()
{
    return s_active && s_active->FrameGenOn();
}

void DX12Present_SetFrameGenInputs()
{
    if (!s_active || !s_active->FrameGenOn())
        return;
    static bool unavailable_logged = false;
    if (!RImplementation.o.ssfx_motionvectors || RImplementation.o.dx10_msaa)
    {
        if (!unavailable_logged)
            Msg("! r4_framegen: interpolation unavailable (%s), presenting rendered frames only",
                RImplementation.o.dx10_msaa ? "MSAA is on" : "no SSS motion vectors");
        unavailable_logged = true;
        return;
    }
    const CRenderTarget* T = RImplementation.Target;
    if (!T || !T->temporal_active || !T->rt_dlss_mv || !T->rt_dlss_depth || Device.m_SecondViewport.IsSVPFrame())
        return;
    if (g_pGamePersistent && g_pGamePersistent->m_pMainMenu && g_pGamePersistent->m_pMainMenu->IsActive())
        return;

    // real time between interpolated frames (fTimeDelta follows the game time factor)
    static LARGE_INTEGER freq = {}, last = {};
    LARGE_INTEGER now;
    QueryPerformanceCounter(&now);
    if (!freq.QuadPart)
        QueryPerformanceFrequency(&freq);
    const float frame_ms = last.QuadPart ? float(double(now.QuadPart - last.QuadPart) * 1000.0 / double(freq.QuadPart)) : 16.7f;
    last = now;

    FrameGenInputs in;
    in.reset = T->history_reset;
    in.render_width = T->render_width;
    in.render_height = T->render_height;
    in.jitter_x = T->jitter_px.x;
    in.jitter_y = T->jitter_px.y;
    // rt_dlss_depth = _33 + _43 / z of Device.mProject (LH, not reversed): near = -_43/_33, far = _43/(1-_33)
    const Fmatrix& P = Device.mProject;
    in.camera_near = -P._43 / P._33;
    in.camera_far = P._43 / (1.f - P._33);
    in.camera_fov_y = 2.f * atanf(1.f / P._22);
    in.frame_time_ms = frame_ms;
    const Fvector* v[] = {&Device.vCameraPosition, &Device.vCameraTop, &Device.vCameraRight, &Device.vCameraDirection};
    float* dst[] = {in.camera_pos, in.camera_up, in.camera_right, in.camera_forward};
    for (int i = 0; i < 4; ++i)
    {
        dst[i][0] = v[i]->x;
        dst[i][1] = v[i]->y;
        dst[i][2] = v[i]->z;
    }
    s_active->SetInputs(T->rt_dlss_depth->pSurface, T->rt_dlss_mv->pSurface, in);
}
#endif
