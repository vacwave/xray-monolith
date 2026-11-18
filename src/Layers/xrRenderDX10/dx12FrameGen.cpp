// dx12FrameGen.cpp: AMD FSR frame generation swapchain for the DX12 presentation bridge (r4_framegen).
#include "stdafx.h"
#pragma hdrstop

#ifdef USE_DX11
#include "dx12FrameGen.h"
#include <FidelityFX/framegeneration/include/dx12/ffx_api_framegeneration_dx12.h>
#include <FidelityFX/framegeneration/include/ffx_framegeneration.h>

namespace
{
struct FfxApi
{
    PfnFfxCreateContext CreateContext = nullptr;
    PfnFfxDestroyContext DestroyContext = nullptr;
    PfnFfxConfigure Configure = nullptr;
    PfnFfxQuery Query = nullptr;
    PfnFfxDispatch Dispatch = nullptr;
};

// the loader stays loaded for the process: the proxy swapchain's code lives in the DLLs it loads
const FfxApi* LoadFfx()
{
    static FfxApi api;
    static bool tried = false;
    if (!tried)
    {
        tried = true;
        HMODULE dll = LoadLibraryW(L"amd_fidelityfx_loader_dx12.dll");
        if (!dll)
        {
            Msg("! r4_framegen: amd_fidelityfx_loader_dx12.dll not found next to the exe (error %u)", GetLastError());
            return nullptr;
        }
        api.CreateContext = (PfnFfxCreateContext)GetProcAddress(dll, "ffxCreateContext");
        api.DestroyContext = (PfnFfxDestroyContext)GetProcAddress(dll, "ffxDestroyContext");
        api.Configure = (PfnFfxConfigure)GetProcAddress(dll, "ffxConfigure");
        api.Query = (PfnFfxQuery)GetProcAddress(dll, "ffxQuery");
        api.Dispatch = (PfnFfxDispatch)GetProcAddress(dll, "ffxDispatch");
        if (!api.CreateContext || !api.DestroyContext || !api.Configure || !api.Query || !api.Dispatch)
        {
            Msg("! r4_framegen: amd_fidelityfx_loader_dx12.dll lacks the ffx API exports");
            api.CreateContext = nullptr;
        }
    }
    return api.CreateContext ? &api : nullptr;
}

const char* ProviderName(const FfxApi* api, void* ctx)
{
    ffxQueryGetProviderVersion q = {};
    q.header.type = FFX_API_QUERY_DESC_TYPE_GET_PROVIDER_VERSION;
    if (api->Query(&ctx, &q.header) != FFX_API_RETURN_OK || !q.versionName)
        return "?";
    return q.versionName;
}
} // namespace

CFrameGen::~CFrameGen()
{
    DestroyContext();
    DestroySwapChain();
}

IDXGISwapChain4* CFrameGen::CreateSwapChain(IDXGIFactory2* factory, ID3D12CommandQueue* queue, HWND hwnd,
                                            const DXGI_SWAP_CHAIN_DESC1& desc, const DXGI_SWAP_CHAIN_FULLSCREEN_DESC& desc_fullscreen)
{
    const FfxApi* api = LoadFfx();
    if (!api)
        return nullptr;

    DXGI_SWAP_CHAIN_DESC1 sc_desc = desc;
    DXGI_SWAP_CHAIN_FULLSCREEN_DESC sc_desc_fullscreen = desc_fullscreen;
    IDXGISwapChain4* sc = nullptr;

    ffxCreateContextDescFrameGenerationSwapChainVersionDX12 version = {};
    version.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_FRAMEGENERATIONSWAPCHAIN_VERSION_DX12;
    version.version = FFX_FRAMEGENERATION_SWAPCHAIN_DX12_VERSION;

    ffxCreateContextDescFrameGenerationSwapChainForHwndDX12 create = {};
    create.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_FRAMEGENERATIONSWAPCHAIN_FOR_HWND_DX12;
    create.header.pNext = &version.header;
    create.swapchain = &sc;
    create.hwnd = hwnd;
    create.desc = &sc_desc;
    create.fullscreenDesc = &sc_desc_fullscreen;
    create.dxgiFactory = factory;
    create.gameQueue = queue;

    const ffxReturnCode_t rc = api->CreateContext(&m_sc_ctx, &create.header, nullptr);
    if (rc != FFX_API_RETURN_OK || !sc)
    {
        Msg("! r4_framegen: FSR frame generation swapchain creation failed (ffx %u)", rc);
        if (m_sc_ctx)
            api->DestroyContext(&m_sc_ctx, nullptr);
        m_sc_ctx = nullptr;
        _RELEASE(sc);
        return nullptr;
    }
    m_sc = sc;
    Msg("* r4_framegen: FSR frame generation swapchain created (%s)", ProviderName(api, m_sc_ctx));
    return sc;
}

bool CFrameGen::CreateContext(ID3D12Device* device)
{
    const FfxApi* api = LoadFfx();
    if (!api || !m_sc_ctx || m_fg_ctx)
        return false;

    DXGI_SWAP_CHAIN_DESC1 desc;
    if (FAILED(m_sc->GetDesc1(&desc)))
        return false;

    ffxCreateBackendDX12Desc backend = {};
    backend.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_BACKEND_DX12;
    backend.device = device;

    ffxCreateContextDescFrameGenerationVersion version = {};
    version.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_FRAMEGENERATION_VERSION;
    version.header.pNext = &backend.header;
    version.version = FFX_FRAMEGENERATION_VERSION;

    // hudless colour is a copy of the back buffer, same format
    ffxCreateContextDescFrameGeneration create = {};
    create.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_FRAMEGENERATION;
    create.header.pNext = &version.header;
    create.flags = 0;
    create.displaySize = {desc.Width, desc.Height};
    create.maxRenderSize = {desc.Width, desc.Height};
    create.backBufferFormat = ffxApiGetSurfaceFormatDX12(desc.Format);

    const ffxReturnCode_t rc = api->CreateContext(&m_fg_ctx, &create.header, nullptr);
    if (rc != FFX_API_RETURN_OK)
    {
        Msg("! r4_framegen: FSR frame generation context creation failed (ffx %u, %ux%u)", rc, desc.Width, desc.Height);
        m_fg_ctx = nullptr;
        return false;
    }
    Configure(false, nullptr);
    Msg("* r4_framegen: frame generation context %ux%u (%s)", desc.Width, desc.Height, ProviderName(api, m_fg_ctx));
    return true;
}

void CFrameGen::Configure(bool enabled, ID3D12Resource* hudless)
{
    const FfxApi* api = LoadFfx();
    if (!api || !m_fg_ctx)
        return;
    ffxConfigureDescFrameGeneration cfg = {};
    cfg.header.type = FFX_API_CONFIGURE_DESC_TYPE_FRAMEGENERATION;
    cfg.swapChain = m_sc;
    cfg.frameGenerationEnabled = enabled;
    cfg.allowAsyncWorkloads = false;
    if (hudless)
        cfg.HUDLessColor = ffxApiGetResourceDX12(hudless, FFX_API_RESOURCE_STATE_COMMON);
    cfg.frameID = m_frame_id;
    if (enabled)
    {
        // interpolation runs inside the swapchain Present on the game queue
        cfg.frameGenerationCallback = [](ffxDispatchDescFrameGeneration* params, void* ctx) -> ffxReturnCode_t
        {
            return LoadFfx()->Dispatch(reinterpret_cast<ffxContext*>(ctx), &params->header);
        };
        cfg.frameGenerationCallbackUserContext = &m_fg_ctx;
    }
    DXGI_SWAP_CHAIN_DESC1 desc;
    if (SUCCEEDED(m_sc->GetDesc1(&desc)))
        cfg.generationRect = {0, 0, (int32_t)desc.Width, (int32_t)desc.Height};

    const ffxReturnCode_t rc = api->Configure(&m_fg_ctx, &cfg.header);
    if (rc != FFX_API_RETURN_OK)
        Msg("! r4_framegen: configure (interpolation %s) failed (ffx %u)", enabled ? "on" : "off", rc);
    m_enabled = enabled;
}

void CFrameGen::OnPresent(ID3D12GraphicsCommandList* list, ID3D12Resource* hudless, const FrameGenInputs& in)
{
    ++m_frame_id;
    if (!m_fg_ctx)
        return;
    // TODO(D8 phase 2): when in.valid, record ffxDispatchDescFrameGenerationPrepareV2 (depth, motion vectors, jitter,
    // camera, frame time, reset) into list and call Configure(true, hudless) every frame.
    (void)list;
    (void)in;
    if (m_enabled)
        Configure(false, hudless);
}

void CFrameGen::WaitForPresents()
{
    const FfxApi* api = LoadFfx();
    if (!api || !m_sc_ctx)
        return;
    ffxDispatchDescFrameGenerationSwapChainWaitForPresentsDX12 wait = {};
    wait.header.type = FFX_API_DISPATCH_DESC_TYPE_FRAMEGENERATIONSWAPCHAIN_WAIT_FOR_PRESENTS_DX12;
    api->Dispatch(&m_sc_ctx, &wait.header);
}

void CFrameGen::DestroyContext()
{
    const FfxApi* api = LoadFfx();
    if (!api || !m_fg_ctx)
        return;
    // disabling waits for the swapchain's interpolation and UI work that uses the context's resources
    Configure(false, nullptr);
    api->DestroyContext(&m_fg_ctx, nullptr);
    m_fg_ctx = nullptr;
}

void CFrameGen::DestroySwapChain()
{
    const FfxApi* api = LoadFfx();
    if (!api || !m_sc_ctx)
        return;
    api->DestroyContext(&m_sc_ctx, nullptr);
    m_sc_ctx = nullptr;
    m_sc = nullptr;
}
#endif
