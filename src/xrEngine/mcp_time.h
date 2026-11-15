#pragma once

// MCP time control (time.* methods): a pause separate from the game's own pauses, fixed-dt frame stepping and the
// simulation time factor. All state resets when the MCP client changes. Main thread only.
namespace mcp_time
{
	void register_methods();
	// CRenderDevice::FrameMove right after the frame timers are read: applies pause/resume, sets up step frames
	void on_frame_time();
	// Injected input timing (mcp_input): false = frozen this frame (MCP-paused, not a step frame), else ms to advance:
	// wall ms when MCP time control is not in use, in menus or the console, else simulation ms (Device.dwTimeDelta)
	bool input_delta(u32 wall_ms, u32& out_ms);
	// The MCP client asked for a pause (it may wait for a menu to close or the next frame to take effect)
	ENGINE_API bool paused();
}
