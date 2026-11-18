#pragma once

#ifdef USE_DX11
// r4_dx12_present: a D3D12 device owns the flip swapchain of the window; the returned object stands in for the
// DX11 swapchain (GetBuffer = a D3D11 texture shared with D3D12, Present = fence sync + copy + D3D12 Present).
// Returns nullptr (and logs why) when the bridge can't be created; the caller then makes the DX11 swapchain.
IDXGISwapChain1* DX12Present_Create(ID3D11Device1* device, ID3D11DeviceContext1* context, IDXGIFactory2* factory,
                                    HWND hwnd, const DXGI_SWAP_CHAIN_DESC1& desc,
                                    const DXGI_SWAP_CHAIN_FULLSCREEN_DESC& desc_fullscreen);
#endif
