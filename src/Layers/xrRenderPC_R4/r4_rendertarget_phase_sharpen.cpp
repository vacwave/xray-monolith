#include "stdafx.h"

// RCAS sharpening of the upscaled scene image (gamedata shaders\r3\upscale_sharpen.ps)
class CBlender_sharpen : public IBlender
{
public:
	virtual LPCSTR getComment() { return "INTERNAL: sharpen"; }
	virtual BOOL canBeDetailed() { return FALSE; }
	virtual BOOL canBeLMAPped() { return FALSE; }

	virtual void Compile(CBlender_Compile& C)
	{
		IBlender::Compile(C);
		C.r_Pass("stub_notransform_t", "upscale_sharpen", FALSE, FALSE, FALSE);
		C.r_dx10Texture("s_sharpen_src", "$user$sharpen_src");
		C.r_End();
	}
};

// After the upscale slot (output size), before the combine tail and UI: generic0 -> copy -> RCAS -> generic0
void CRenderTarget::phase_sharpen()
{
	// HDR10 keeps generic0 above 1.0, RCAS assumes LDR 0..1; scope frames are left to D5
	if (ps_r4_upscale_sharpness <= 0.f || RImplementation.o.dx11_hdr10 || Device.m_SecondViewport.IsSVPFrame())
		return;

	PIX_EVENT(phase_sharpen);
	if (!s_sharpen)
	{
		// Same size and format as generic0 here, so CopyResource works with and without the output twins
		rt_sharpen_src.create("$user$sharpen_src", Device.dwWidth, Device.dwHeight, rt_Generic_0->fmt, 1);
		b_sharpen = xr_new<CBlender_sharpen>();
		s_sharpen.create(b_sharpen, "r2\\sharpen");
	}

	HW.pContext->CopyResource(rt_sharpen_src->pSurface, rt_Generic_0->pSurface);
	u_setrt(rt_Generic_0, nullptr, nullptr, nullptr);
	RCache.set_CullMode(CULL_NONE);
	RCache.set_Stencil(FALSE);
	RCache.set_Element(s_sharpen->E[0]);
	// FSR's 0..1 mapping: stops = 2 - 2 * sharpness, lobe scale = 2^-stops
	RCache.set_c("rcas_con", exp2f(2.f * ps_r4_upscale_sharpness - 2.f), 0.f, 0.f, 0.f);

	const float w = float(Device.dwWidth);
	const float h = float(Device.dwHeight);
	const u32 C = color_rgba(255, 255, 255, 255);
	u32 Offset;
	FVF::TL* pv = (FVF::TL*)RCache.Vertex.Lock(4, g_combine->vb_stride, Offset);
	pv->set(0, h, EPS_S, 1.f, C, 0.f, 1.f); pv++;
	pv->set(0, 0, EPS_S, 1.f, C, 0.f, 0.f); pv++;
	pv->set(w, h, EPS_S, 1.f, C, 1.f, 1.f); pv++;
	pv->set(w, 0, EPS_S, 1.f, C, 1.f, 0.f); pv++;
	RCache.Vertex.Unlock(4, g_combine->vb_stride);
	RCache.set_Geometry(g_combine);
	RCache.Render(D3DPT_TRIANGLELIST, Offset, 0, 4, 0, 2);
}
