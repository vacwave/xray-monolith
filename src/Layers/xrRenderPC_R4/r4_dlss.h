#pragma once

// NVIDIA DLSS for R4. The only code that talks to NGX (keeps a later switch to Streamline local).
namespace dlss
{
    // after the device exists: init NGX, log availability and the render size per quality mode
    void create(ID3D11Device *device, u32 out_width, u32 out_height);
    // output size changed (device reset): log the render sizes again
    void resize(u32 out_width, u32 out_height);
    // before the device is destroyed
    void destroy();
    bool available();
} // namespace dlss
