# Render / screenshots
- `screenshot` cmd = `CCC_Screenshot` Layers/xrRender/xrRender_console.cpp:731 → `Render->Screenshot()`.
- API xrEngine/Render.h:445-448: `Screenshot(mode,name)`, `Screenshot(mode,CMemoryWriter&)` (SM_FOR_MPSENDING only, small DDS, r__screenshot.cpp:419), `ScreenshotAsyncBegin/End(CMemoryWriter&)` → w,h + raw RGBA (:437-555); used by xrGame/screenshot_manager.cpp:201,252.
- Normal mode encodes JPG/PNG/BMP in memory via `D3DX11SaveTextureToMemory` :176 before writing.
