#include "stdafx.h"
#include "r4_render_scale.h"
#include "../../xrEngine/XR_IOConsole.h"
#include "../../xrEngine/xr_ioc_cmd.h"

float ps_r4_render_scale = 1.f;

namespace
{
    struct render_scale_state
    {
        u32 render_w = 0, render_h = 0;
        u32 output_w = 0, output_h = 0;
        float scale = 1.f;
        bool active = false;

        bool in_scene = false;
        bool in_menu = false;
        u32 saved_w = 0, saved_h = 0;
        ID3DDepthStencilView *saved_zb = nullptr;
    };

    render_scale_state s;
    ref_rt rt_scene_zb;

    class CCC_RenderScale : public CCC_Float
    {
    public:
        CCC_RenderScale(LPCSTR N, float *V, float _min, float _max)
            : CCC_Float(N, V, _min, _max)
        {
        }

        void Execute(LPCSTR args) override
        {
            const float old = *value;
            CCC_Float::Execute(args);
            // Targets are rebuilt through CRender::reset_begin/reset_end
            if (!fsimilar(old, *value) && Device.b_is_Ready)
                Device.Reset();
        }
    };
} // namespace

namespace r4_render_scale
{
    u32 render_width()
    {
        return s.render_w;
    }
    u32 render_height()
    {
        return s.render_h;
    }
    u32 output_width()
    {
        return s.output_w;
    }
    u32 output_height()
    {
        return s.output_h;
    }
    float scale()
    {
        return s.scale;
    }
    bool active()
    {
        return s.active;
    }
    bool in_scene()
    {
        return s.in_scene;
    }
    const ref_rt &scene_depth()
    {
        return rt_scene_zb;
    }

    CRenderTarget *create_target()
    {
        VERIFY(!s.in_scene);
        s.output_w = Device.dwWidth;
        s.output_h = Device.dwHeight;
        // Stable copy for readers off the render thread (Lua device().width/height)
        Device.dwOutputWidth = s.output_w;
        Device.dwOutputHeight = s.output_h;

        float k = clampr(ps_r4_render_scale, 0.33f, 1.f);
        if (k < 1.f && RImplementation.o.dx10_msaa)
        {
            Msg("! r4_render_scale %.2f ignored: not supported with MSAA", k);
            k = 1.f;
        }
        s.scale = k;
        s.render_w = _max(1u, u32(iFloor(float(s.output_w) * k + 0.5f)));
        s.render_h = _max(1u, u32(iFloor(float(s.output_h) * k + 0.5f)));
        s.active = s.render_w != s.output_w || s.render_h != s.output_h;

        if (!s.active)
        {
            s.render_w = s.output_w;
            s.render_h = s.output_h;
            return xr_new<CRenderTarget>();
        }

        // Everything the target sizes from Device becomes render-size; output-size targets use output_width/height
        Device.dwWidth = s.render_w;
        Device.dwHeight = s.render_h;
        CRenderTarget *T = xr_new<CRenderTarget>();
        Device.dwWidth = s.output_w;
        Device.dwHeight = s.output_h;

        rt_scene_zb.create("$user$render_scale_zb", s.render_w, s.render_h, D3DFMT_D24S8);
        // Loading screen and UI draw before the first scene frame: start at output size
        T->u_setrt(s.output_w, s.output_h, HW.pBaseRT, nullptr, nullptr, HW.pBaseZB);

        Msg("* r4_render_scale %.2f: render %ux%u, output %ux%u", k, s.render_w, s.render_h, s.output_w, s.output_h);
        return T;
    }

    void destroy_target()
    {
        VERIFY(!s.in_scene);
        rt_scene_zb.destroy();
        s.active = false;
        s.in_menu = false;
    }

    void begin_scene()
    {
        if (!s.active || s.in_scene)
            return;

        s.in_scene = true;
        s.saved_w = Device.dwWidth;
        s.saved_h = Device.dwHeight;
        s.saved_zb = HW.pBaseZB;

        Device.dwWidth = s.render_w;
        Device.dwHeight = s.render_h;
        HW.pBaseZB = rt_scene_zb->pZRT;

        RImplementation.Target->u_setrt(s.render_w, s.render_h, nullptr, nullptr, nullptr, HW.pBaseZB);
        RImplementation.rmNormal();
    }

    void end_scene()
    {
        if (!s.in_scene)
            return;

        s.in_scene = false;
        Device.dwWidth = s.saved_w;
        Device.dwHeight = s.saved_h;
        HW.pBaseZB = s.saved_zb;

        // The scene image is render-size rt_Color ($user$albedo) and depth is rt_scene_zb.
        // phase_pp samples rt_Color bilinearly into the output-size backbuffer; an upscaler (DLSS) goes here.
        RImplementation.Target->u_setrt(Device.dwWidth, Device.dwHeight, HW.pBaseRT, nullptr, nullptr, HW.pBaseZB);
        RImplementation.rmNormal();
    }

    void set_menu_rt(const ref_rt &rt)
    {
        CRenderTarget *T = RImplementation.Target;
        if (!s.active)
        {
            T->u_setrt(rt, 0, 0, HW.pBaseZB);
            return;
        }

        // get_width() stays output-size so ui_core::pp_start lays out in output pixels like screen_res
        T->u_setrt(Device.dwWidth, Device.dwHeight, rt->pRT, nullptr, nullptr, rt_scene_zb->pZRT);
        T->set_viewport_size(HW.pContext, float(rt->dwWidth), float(rt->dwHeight));
        s.in_menu = true;
    }

    void end_menu()
    {
        s.in_menu = false;
        RImplementation.rmNormal();
    }

    Irect *menu_scissor(Irect *rect)
    {
        if (!s.in_menu || !rect)
            return rect;

        static Irect scaled;
        const float kx = float(s.render_w) / float(s.output_w);
        const float ky = float(s.render_h) / float(s.output_h);
        scaled.x1 = iFloor(float(rect->x1) * kx);
        scaled.y1 = iFloor(float(rect->y1) * ky);
        scaled.x2 = iCeil(float(rect->x2) * kx);
        scaled.y2 = iCeil(float(rect->y2) * ky);
        return &scaled;
    }

    void register_console_commands()
    {
        CMD4(CCC_RenderScale, "r4_render_scale", &ps_r4_render_scale, 0.33f, 1.f);
    }
} // namespace r4_render_scale
