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

	// Main thread only: runs f and returns the log lines printed meanwhile (any thread) as JSON members
	// "lines":[first 500 strings],"truncated":<lines dropped>
	ENGINE_API xr_string run_logged(const std::function<void()>& f);
	// JSON helpers for handlers outside xrEngine; engine strings are cp1251, JSON is UTF-8
	ENGINE_API bool get_string_param(LPCSTR params, LPCSTR key, xr_string& out);
	ENGINE_API bool get_int_param(LPCSTR params, LPCSTR key, s64& out);
	ENGINE_API xr_string to_json_string(LPCSTR s);

	// Start buffering engine log lines for log events / log.tail (call as early as possible)
	void capture_log();
	void start(u16 port, LPCSTR version);
	void stop();
}
