#pragma once
#include <functional>

// Localhost IPC server for an external supervisor (enabled by -mcp_port N).
// Newline-delimited JSON: requests run on the main thread in a pureFrame.
namespace mcp
{
	// params is the request params as JSON text. Return true and set out to the result as JSON text,
	// or return false and set out to an error message.
	typedef std::function<bool(LPCSTR params, xr_string& out)> handler;

	// Main thread only
	ENGINE_API void register_method(LPCSTR name, const handler& h);
	// Thread-safe; data_json must be valid JSON text. Dropped when no client is connected
	ENGINE_API void send_event(LPCSTR name, LPCSTR data_json);

	// Start buffering engine log lines for log events / log.tail (call as early as possible)
	void capture_log();
	void start(u16 port, LPCSTR version);
	void stop();
}
