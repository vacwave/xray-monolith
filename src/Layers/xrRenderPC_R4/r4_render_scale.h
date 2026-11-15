#pragma once

// R4 render resolution. The scene (G-buffer .. phase_combine) runs at render size, phase_pp and the 2D UI at
// output (swap-chain) size. Inactive when render size == output size: every function then keeps the old behaviour.
// During the scene Device.dwWidth/dwHeight and HW.pBaseZB are redirected to render size, so the existing
// screen-size code, `screen_res` and shader packs see one consistent resolution.

class CRenderTarget;

extern float ps_r4_render_scale;

namespace r4_render_scale
{
    // Sizes are fixed when the main target is created (CRender::create / reset_end).
    u32 render_width();
    u32 render_height();
    u32 output_width();
    u32 output_height();
    float scale();
    bool active();
    bool in_scene();

    // Render-size depth-stencil used as HW.pBaseZB during the scene (null when inactive). Has an SRV (R24G8).
    const ref_rt &scene_depth();

    // Creates the main target at render size plus the scene depth; replaces xr_new<CRenderTarget>().
    CRenderTarget *create_target();
    // Releases the scene depth; call before deleting the main target.
    void destroy_target();

    // Redirect Device size and HW.pBaseZB to render size / restore them. end_scene is the upscale seam before phase_pp.
    void begin_scene();
    void end_scene();

    // render_menu: binds a render-size target while UI layout keeps output size (viewport = render size).
    void set_menu_rt(const ref_rt &rt);
    // render_menu: back to the output viewport after the menu targets.
    void end_menu();
    // UI scissor rects are in output pixels; between set_menu_rt and end_menu they are scaled to the render-size target.
    Irect *menu_scissor(Irect *rect);

    void register_console_commands();
} // namespace r4_render_scale
