#include "pch_script.h"
#include "../xrEngine/mcp_perf.h"
#include "ai_space.h"
#include "script_engine.h"
#include <luabind/detail/pcall.hpp>

// xrGame side of the MCP perf window: Lua heap size and the luabind pcall/resume timing hook
void mcp_lua_profiler_hook(lua_State* L, int fn, bool enter);
void mcp_register_lua_profiler();

namespace
{
	s64 lua_heap_bytes()
	{
		if (!g_ai_space)
			return -1;
		lua_State* L = ai().script_engine().lua();
		if (!L)
			return -1;
		return s64(lua_gc(L, LUA_GCCOUNT, 0)) * 1024 + lua_gc(L, LUA_GCCOUNTB, 0);
	}

	void set_lua_hook(bool on)
	{
		luabind::detail::pcall_hook = on ? mcp_lua_profiler_hook : nullptr;
	}
}

void mcp_register_perf()
{
	mcp_perf::lua_heap_bytes = lua_heap_bytes;
	mcp_perf::set_lua_hook = set_lua_hook;
	mcp_register_lua_profiler();
}
