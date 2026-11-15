#pragma once

// MCP input injection (input.run / input.status / input.release_all): timed scripts of key, mouse and look ops
// run per frame at the end of CInput::OnFrame and delivered like real input (CurrentIR()->IR_On*).
namespace mcp_input
{
	struct game_hooks
	{
		// action (bind name or alias) or key name -> dik (MOUSE_1.. for mouse buttons); false sets err (with the valid names)
		bool (*resolve)(LPCSTR name, bool action, int& dik, xr_string& err);
		// dik -> key name for status output
		LPCSTR (*key_name)(int dik);
		// rotate the actor camera (radians, + = right / up); false when there is no actor
		bool (*look)(float yaw, float pitch);
	};

	// Called by xrGame at app start; registers the methods (no-op without -mcp_port)
	ENGINE_API void init(const game_hooks& hooks);
	// CInput::OnFrame hook
	void on_frame();
}
